"""Misty POS - SQLite 接続・スキーマ・トランザクション

設計上の判断:
- ファイル1つで完結する SQLite を使う。ノートPC 1台で動き、PyInstaller で
  exe 化しても追加のサービスが要らないため。書き込みはレジ数台分（毎分数件〜数十件）
  なので、単一ライターの SQLite で十分に捌ける。
- WAL モードにして、バーテン画面の2秒周期の読み取りがレジの書き込みを待たないようにする。
- synchronous=FULL を明示する。WAL + NORMAL の方が速いが、電源断で直前の
  コミットを失いうる。ノートPCの電池切れはまさに想定している障害なので、速度より耐久性を取る。
- Python の sqlite3 は既定で DML の直前に暗黙の BEGIN を発行し、どこで
  トランザクションが始まったかがコードから読めなくなる。isolation_level=None で
  暗黙のトランザクションを切り、書き込みは必ず transaction() を通す。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from flask import g

from . import runtime

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS menu_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL UNIQUE,
    category    TEXT    NOT NULL DEFAULT '',
    price       INTEGER NOT NULL CHECK (price >= 0),
    -- 在庫はマイナスにならないことを DB で保証する。アプリ側のチェックをすり抜けた
    -- 過剰販売は、黙って 0 に丸めるのではなく制約違反として失敗させる
    stock_qty   INTEGER NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    -- クライアントが会計ごとに生成する冪等キー。タイムアウト後の再送で二重計上しない
    request_id    TEXT    UNIQUE,
    seat_no       INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'done', 'void')),
    created_at    TEXT    NOT NULL,
    completed_at  TEXT,
    voided_at     TEXT,
    void_reason   TEXT,
    device_name   TEXT,
    total_amount  INTEGER NOT NULL DEFAULT 0 CHECK (total_amount >= 0)
);

-- 1枚の整理番号札が「準備中」の注文を2件以上持たないことを部分ユニークインデックスで保証する。
-- アプリ側で SELECT してから INSERT する方式だと、レジ2台が同時に同じ札で会計したときに
-- 両方通ってしまう（check-then-act 競合）。制約にしておけば判定は DB が原子的に行う。
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_seat_pending
    ON orders(seat_no) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS order_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id        INTEGER NOT NULL REFERENCES orders(id),
    menu_item_id    INTEGER NOT NULL,
    -- 会計後にメニュー名や価格を変えても売上記録が変わらないよう、会計時点の値を複製して持つ
    menu_item_name  TEXT    NOT NULL,
    qty             INTEGER NOT NULL CHECK (qty > 0),
    unit_price      INTEGER NOT NULL CHECK (unit_price >= 0)
);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);

-- 追記専用の監査ログ。状態変更と同じトランザクションで書くので、
-- 「注文はあるのにログがない」状態が発生しない
CREATE TABLE IF NOT EXISTS operation_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,
    device_name  TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    order_id     INTEGER,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);

-- sender は端末名ではなく役割ラベル（「レジ」「バーテン」）。同じ役割の端末が
-- 何台あっても、受け手が知りたいのは「どの持ち場からか」なので
CREATE TABLE IF NOT EXISTS chat_messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT    NOT NULL,
    sender  TEXT    NOT NULL,
    text    TEXT    NOT NULL
);

-- 書き込みトランザクションのたびに +1 される世代番号。スタンバイの複製で
-- ETag として使い、変化がない周期はスナップショットの転送と再書き込みを丸ごと省く
INSERT OR IGNORE INTO meta (key, value) VALUES ('data_version', '0');
INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', '1');
"""


def connect(path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # timeout は busy_timeout として効く。別接続が書き込み中でも、10秒までは待ってから失敗する
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(runtime.settings().db_path)
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@contextmanager
def transaction(db: sqlite3.Connection | None = None, *, bump_version: bool = True):
    """書き込み用トランザクション。

    BEGIN IMMEDIATE で開始時に書き込みロックを取る。DEFERRED だと読んだ後に
    書こうとした時点でロック昇格に失敗しうる（SQLITE_BUSY）ため、
    読んで判定してから書く処理（在庫チェック→減算）では IMMEDIATE が必須。
    """
    db = db or get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        yield db
        if bump_version:
            db.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'data_version'"
            )
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


@contextmanager
def snapshot(db: sqlite3.Connection | None = None):
    """読み取り専用の一貫したスナップショット。

    WAL では、トランザクション外の SELECT は文ごとに最新のコミットを見る。
    複数テーブルを順に読むと、orders と order_items の間に別の会計が挟まり
    「明細のない注文」を複製してしまう。BEGIN 内で読めば全 SELECT が同じ時点を見る。
    """
    db = db or get_db()
    db.execute("BEGIN")
    try:
        yield db
    finally:
        db.execute("COMMIT")


def init_db(db: sqlite3.Connection):
    db.executescript(SCHEMA)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def get_meta(key, default=None, db=None):
    db = db or get_db()
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key, value, db=None):
    db = db or get_db()
    db.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def data_version(db=None) -> int:
    return int(get_meta("data_version", "0", db=db))
