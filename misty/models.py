"""Misty POS - ドメインロジックとデータアクセス

すべての書き込みは db.transaction() の中で「検証 → 状態変更 → 操作ログ」を
1トランザクションで行う。状態遷移（準備中→完了→取消）は
`UPDATE ... WHERE status = <遷移元>` の条件付き更新で表現し、影響行数で成否を判定する。
先に SELECT で状態を確かめてから UPDATE する書き方だと、バーテン2人が同じ注文を
同時に完了にしたり、管理画面とレジが同時に取消したりしたときに、在庫の二重戻しが起きる。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .db import data_version, get_db, get_meta, set_meta, snapshot, transaction
from .errors import Conflict, NotFound, ValidationError

MAX_QTY_PER_LINE = 99
MAX_LINES_PER_ORDER = 50
MAX_REQUEST_ID_LEN = 64


def now_iso() -> str:
    # 端末のローカルタイムゾーン付きで保存する。CSV を開いた人がそのまま読めることを優先した
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------- 入力検証

def _int(value, field, lo=None, hi=None) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} は整数で指定してください")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} は整数で指定してください") from None
    if isinstance(value, float) and value != n:
        raise ValidationError(f"{field} は整数で指定してください")
    if lo is not None and n < lo:
        raise ValidationError(f"{field} は {lo} 以上で指定してください")
    if hi is not None and n > hi:
        raise ValidationError(f"{field} は {hi} 以下で指定してください")
    return n


def _text(value, field, *, max_len, required=False) -> str:
    s = "" if value is None else str(value).strip()
    if required and not s:
        raise ValidationError(f"{field} を入力してください")
    if len(s) > max_len:
        raise ValidationError(f"{field} は {max_len} 文字以内にしてください")
    return s


_MENU_FIELDS = {
    "name": lambda v: _text(v, "商品名", max_len=60, required=True),
    "category": lambda v: _text(v, "カテゴリー", max_len=40),
    "price": lambda v: _int(v, "価格", 0, 1_000_000),
    "stock_qty": lambda v: _int(v, "在庫数", 0, 100_000),
    "is_active": lambda v: 1 if v in (True, 1, "1", "true") else 0,
    "sort_order": lambda v: _int(v, "並び順"),
}


# ---------------------------------------------------------------- 操作ログ

def _log(db, device_name, action, order_id=None, detail=None):
    """呼び出し側のトランザクション内で書く（単独でコミットしない）。"""
    db.execute(
        "INSERT INTO operation_log (ts, device_name, action, order_id, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (now_iso(), device_name, action, order_id,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None),
    )


def log_action(device_name, action, order_id=None, detail=None):
    with transaction() as db:
        _log(db, device_name, action, order_id, detail)


def list_operation_log(limit=200):
    rows = get_db().execute(
        "SELECT * FROM operation_log ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- メニュー

def list_menu(active_only=False):
    q = "SELECT * FROM menu_items"
    if active_only:
        q += " WHERE is_active = 1"
    q += " ORDER BY sort_order, id"
    return [dict(r) for r in get_db().execute(q).fetchall()]


def get_menu_item(item_id):
    row = get_db().execute("SELECT * FROM menu_items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def _insert_menu_item(db, values):
    try:
        cur = db.execute(
            "INSERT INTO menu_items (name, category, price, stock_qty, is_active, sort_order) "
            "VALUES (:name, :category, :price, :stock_qty, :is_active, "
            "(SELECT COALESCE(MAX(sort_order), 0) + 1 FROM menu_items))",
            values,
        )
    except sqlite3.IntegrityError:
        raise Conflict(f"「{values['name']}」は既に登録されています") from None
    return cur.lastrowid


def create_menu_item(device_name, name, category="", price=0, stock_qty=0, is_active=True):
    values = {
        "name": _MENU_FIELDS["name"](name),
        "category": _MENU_FIELDS["category"](category),
        "price": _MENU_FIELDS["price"](price),
        "stock_qty": _MENU_FIELDS["stock_qty"](stock_qty),
        "is_active": _MENU_FIELDS["is_active"](is_active),
    }
    with transaction() as db:
        item_id = _insert_menu_item(db, values)
        _log(db, device_name, "menu_create", detail={"item_id": item_id, **values})
    return get_menu_item(item_id)


def update_menu_item(device_name, item_id, fields: dict):
    if not isinstance(fields, dict) or not fields:
        raise ValidationError("更新する項目がありません")
    unknown = set(fields) - set(_MENU_FIELDS)
    if unknown:
        # 黙って無視すると、フロントのタイポ（stock → stock_qty）に誰も気付けない
        raise ValidationError(f"更新できない項目です: {', '.join(sorted(unknown))}")
    clean = {k: _MENU_FIELDS[k](v) for k, v in fields.items()}
    sets = ", ".join(f"{k} = :{k}" for k in clean)  # キーは _MENU_FIELDS のホワイトリスト由来
    with transaction() as db:
        try:
            cur = db.execute(f"UPDATE menu_items SET {sets} WHERE id = :id", {**clean, "id": item_id})
        except sqlite3.IntegrityError:
            raise Conflict("同じ商品名が既にあります") from None
        if cur.rowcount == 0:
            raise NotFound("商品が見つかりません")
        _log(db, device_name, "menu_update", detail={"item_id": item_id, "fields": clean})
    return get_menu_item(item_id)


def import_menu_rows(rows, device_name):
    """スプレッドシートの行をメニューに反映する（全件成功か全件ロールバック）。

    - 名前で既存商品と照合し、価格・カテゴリーは常に上書きする。
    - 在庫数は「取消されていない会計に一度も出ていない商品」だけ上書きする。
      営業中に取込をやり直しても、売れた分の在庫が開店時の数に巻き戻らないようにするため。
    - 不正な行はスキップし、行番号と理由を返す（どの行を直せばよいか画面に出せるように）。
    """
    parsed, skipped = [], []
    for idx, row in enumerate(rows, start=2):  # 1行目は見出し
        try:
            parsed.append({
                "name": _MENU_FIELDS["name"](row.get("name")),
                "category": _MENU_FIELDS["category"](row.get("category")),
                "price": _MENU_FIELDS["price"](row.get("price")),
                "stock_qty": _MENU_FIELDS["stock_qty"](row.get("stock_qty") or 0),
                "is_active": 1,
            })
        except ValidationError as exc:
            skipped.append({"row": idx, "name": row.get("name"), "reason": exc.message})

    created = updated = 0
    with transaction() as db:
        existing = {r["name"]: r["id"] for r in db.execute("SELECT id, name FROM menu_items")}
        sold_ids = {
            r[0] for r in db.execute(
                "SELECT DISTINCT oi.menu_item_id FROM order_items oi "
                "JOIN orders o ON o.id = oi.order_id WHERE o.status != 'void'"
            )
        }
        for values in parsed:
            item_id = existing.get(values["name"])
            if item_id is None:
                existing[values["name"]] = _insert_menu_item(db, values)
                created += 1
                continue
            if item_id in sold_ids:
                db.execute("UPDATE menu_items SET category = ?, price = ? WHERE id = ?",
                           (values["category"], values["price"], item_id))
            else:
                db.execute("UPDATE menu_items SET category = ?, price = ?, stock_qty = ? WHERE id = ?",
                           (values["category"], values["price"], values["stock_qty"], item_id))
            updated += 1
        _log(db, device_name, "menu_import",
             detail={"created": created, "updated": updated, "skipped": len(skipped)})
    return {"created": created, "updated": updated, "skipped": skipped}


# ---------------------------------------------------------------- 注文

def _normalize_cart(items) -> dict[int, int]:
    """カートを {menu_item_id: 合計数量} に畳む。

    同じ商品が複数行で来たとき（連打・フロントのバグ・手書きのAPI呼び出し）に
    行ごとに在庫を判定すると、各行は在庫以内でも合計で在庫を超えてしまう。
    """
    if not isinstance(items, list) or not items:
        raise ValidationError("カートが空です")
    if len(items) > MAX_LINES_PER_ORDER:
        raise ValidationError("明細が多すぎます")
    qty_by_id: dict[int, int] = {}
    for it in items:
        if not isinstance(it, dict):
            raise ValidationError("明細の形式が不正です")
        item_id = _int(it.get("menu_item_id"), "menu_item_id", 1)
        qty = _int(it.get("qty"), "数量", 1, MAX_QTY_PER_LINE)
        qty_by_id[item_id] = qty_by_id.get(item_id, 0) + qty
    return qty_by_id


def place_order(seat_no, items, device_name, request_id=None, *, seat_range=(1, 20)):
    """会計する。戻り値は (order, created)。

    request_id が既存の注文と一致した場合は新規作成せずその注文を返す（created=False）。
    ホットスポット越しの通信は「サーバーはコミットしたがレスポンスが届かない」ことがあり、
    クライアントの再送を安全にするための冪等キー。
    """
    seat_no = _int(seat_no, "整理番号", *seat_range)
    qty_by_id = _normalize_cart(items)
    if request_id is not None:
        request_id = _text(request_id, "request_id", max_len=MAX_REQUEST_ID_LEN) or None

    with transaction() as db:
        if request_id:
            row = db.execute("SELECT id FROM orders WHERE request_id = ?", (request_id,)).fetchone()
            if row:
                return _get_order(db, row["id"]), False

        ids = list(qty_by_id)
        placeholders = ",".join("?" * len(ids))
        menu = {r["id"]: r for r in db.execute(
            f"SELECT * FROM menu_items WHERE id IN ({placeholders})", ids)}

        lines, total = [], 0
        for item_id, qty in qty_by_id.items():
            m = menu.get(item_id)
            if m is None:
                raise ValidationError("存在しない商品が含まれています")
            if not m["is_active"]:
                raise Conflict(f"{m['name']} は現在販売停止中です", code="inactive_item")
            if m["stock_qty"] < qty:
                raise Conflict(f"{m['name']} の在庫が不足しています（残り{m['stock_qty']}）",
                               code="out_of_stock")
            lines.append((item_id, m["name"], qty, m["price"]))
            total += m["price"] * qty

        try:
            cur = db.execute(
                "INSERT INTO orders (request_id, seat_no, status, created_at, device_name, total_amount) "
                "VALUES (?, ?, 'pending', ?, ?, ?)",
                (request_id, seat_no, now_iso(), device_name, total),
            )
        except sqlite3.IntegrityError as exc:
            # 制約名で判別する。IntegrityError を一律「整理番号の重複」と扱うと、
            # 別の制約違反（CHECK 等）を誤ったメッセージで隠してしまう
            if "orders.seat_no" in str(exc):
                raise Conflict(f"整理番号 {seat_no} は既に準備中の注文があります",
                               code="seat_occupied") from None
            raise
        order_id = cur.lastrowid
        db.executemany(
            "INSERT INTO order_items (order_id, menu_item_id, menu_item_name, qty, unit_price) "
            "VALUES (?, ?, ?, ?, ?)",
            [(order_id, *line) for line in lines],
        )
        db.executemany(
            "UPDATE menu_items SET stock_qty = stock_qty - ? WHERE id = ?",
            [(qty, item_id) for item_id, _, qty, _ in lines],
        )
        _log(db, device_name, "order_create", order_id=order_id,
             detail={"seat_no": seat_no, "total": total, "lines": len(lines)})
        return _get_order(db, order_id), True


def _attach_items(db, orders: list[dict]) -> list[dict]:
    """注文一覧に明細を付ける。注文ごとに SELECT する N+1 を避け、1クエリで取る。"""
    if not orders:
        return orders
    ids = [o["id"] for o in orders]
    by_order: dict[int, list] = {i: [] for i in ids}
    rows = db.execute(
        f"SELECT * FROM order_items WHERE order_id IN ({','.join('?' * len(ids))}) ORDER BY id",
        ids,
    )
    for r in rows:
        by_order[r["order_id"]].append(dict(r))
    for o in orders:
        o["items"] = by_order[o["id"]]
    return orders


def _get_order(db, order_id):
    row = db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        return None
    return _attach_items(db, [dict(row)])[0]


def get_order(order_id):
    return _get_order(get_db(), order_id)


def list_pending_orders():
    db = get_db()
    rows = db.execute("SELECT * FROM orders WHERE status = 'pending' ORDER BY id").fetchall()
    return _attach_items(db, [dict(r) for r in rows])


def list_recent_orders(limit=50, status=None):
    db = get_db()
    q, params = "SELECT * FROM orders", []
    if status:
        q += " WHERE status = ?"
        params.append(status)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return _attach_items(db, [dict(r) for r in db.execute(q, params).fetchall()])


_STATUS_LABEL = {"pending": "準備中", "done": "完了", "void": "取消済み"}


def _transition_failed(db, order_id, expected):
    row = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row is None:
        raise NotFound("注文が見つかりません")
    raise Conflict(
        f"この注文は「{_STATUS_LABEL.get(row['status'], row['status'])}」のため操作できません"
        f"（{expected}の注文のみ）",
        code="invalid_transition",
    )


def complete_order(order_id, device_name):
    with transaction() as db:
        cur = db.execute(
            "UPDATE orders SET status = 'done', completed_at = ? WHERE id = ? AND status = 'pending'",
            (now_iso(), order_id),
        )
        if cur.rowcount == 0:
            _transition_failed(db, order_id, "準備中")
        order = _get_order(db, order_id)
        _log(db, device_name, "order_complete", order_id=order_id, detail={"seat_no": order["seat_no"]})
    return order


def uncomplete_order(order_id, device_name):
    """完了→準備中への差し戻し。

    その整理番号で既に次の注文が準備中なら、部分ユニークインデックスが拒否する。
    """
    with transaction() as db:
        try:
            cur = db.execute(
                "UPDATE orders SET status = 'pending', completed_at = NULL "
                "WHERE id = ? AND status = 'done'",
                (order_id,),
            )
        except sqlite3.IntegrityError:
            seat = db.execute("SELECT seat_no FROM orders WHERE id = ?", (order_id,)).fetchone()
            raise Conflict(f"整理番号 {seat['seat_no']} は既に別の注文が準備中のため、戻せません",
                           code="seat_occupied") from None
        if cur.rowcount == 0:
            _transition_failed(db, order_id, "完了")
        order = _get_order(db, order_id)
        _log(db, device_name, "order_uncomplete", order_id=order_id, detail={"seat_no": order["seat_no"]})
    return order


def void_order(order_id, device_name, reason=""):
    """取消。在庫の戻しは、状態を void に変えられた1回だけ行う。"""
    reason = _text(reason, "取消理由", max_len=200)
    with transaction() as db:
        cur = db.execute(
            "UPDATE orders SET status = 'void', voided_at = ?, void_reason = ? "
            "WHERE id = ? AND status IN ('pending', 'done')",
            (now_iso(), reason, order_id),
        )
        if cur.rowcount == 0:
            _transition_failed(db, order_id, "準備中・完了")
        order = _get_order(db, order_id)
        db.executemany(
            "UPDATE menu_items SET stock_qty = stock_qty + ? WHERE id = ?",
            [(i["qty"], i["menu_item_id"]) for i in order["items"]],
        )
        _log(db, device_name, "order_void", order_id=order_id,
             detail={"seat_no": order["seat_no"], "reason": reason})
    return order


# ---------------------------------------------------------------- 集計

def sales_summary():
    db = get_db()
    with snapshot(db):
        totals = db.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(total_amount), 0) AS total "
            "FROM orders WHERE status != 'void'"
        ).fetchone()
        ranking = db.execute(
            "SELECT oi.menu_item_name AS name, SUM(oi.qty) AS qty, "
            "SUM(oi.qty * oi.unit_price) AS amount "
            "FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.status != 'void' "
            "GROUP BY oi.menu_item_name ORDER BY qty DESC, amount DESC"
        ).fetchall()
    return {
        "order_count": totals["cnt"],
        "total_amount": totals["total"],
        "ranking": [dict(r) for r in ranking],
    }


# ---------------------------------------------------------------- チャット

def send_chat_message(sender, text):
    sender = _text(sender, "送信者", max_len=20) or "端末"
    text = _text(text, "メッセージ", max_len=500, required=True)
    with transaction() as db:
        cur = db.execute("INSERT INTO chat_messages (ts, sender, text) VALUES (?, ?, ?)",
                         (now_iso(), sender, text))
        row = db.execute("SELECT * FROM chat_messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def list_chat_messages(after_id=0, limit=200):
    """after_id より新しいメッセージだけ返す。id は単調増加なので、ポーリングの差分取得に使える。"""
    rows = get_db().execute(
        "SELECT * FROM chat_messages WHERE id > ? ORDER BY id LIMIT ?", (after_id, limit)
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- 複製（host → standby）

_REPLICATED_TABLES = {
    # テーブル名: 列（apply 時の INSERT に使う。SELECT * の列順に依存しない）
    "menu_items": ("id", "name", "category", "price", "stock_qty", "is_active", "sort_order"),
    "orders": ("id", "request_id", "seat_no", "status", "created_at", "completed_at",
               "voided_at", "void_reason", "device_name", "total_amount"),
    "order_items": ("id", "order_id", "menu_item_id", "menu_item_name", "qty", "unit_price"),
    "chat_messages": ("id", "ts", "sender", "text"),
    # 操作ログも複製する。昇格後に旧ホストの操作履歴が参照できないと、障害後の照合ができない
    "operation_log": ("id", "ts", "device_name", "action", "order_id", "detail"),
}
# 外部キーの向きに合わせた削除順（子→親）と挿入順（親→子）
_DELETE_ORDER = ("order_items", "orders", "menu_items", "chat_messages", "operation_log")
_INSERT_ORDER = ("menu_items", "orders", "order_items", "chat_messages", "operation_log")


def export_full_state():
    db = get_db()
    with snapshot(db):
        state = {t: [dict(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY id")]
                 for t in _REPLICATED_TABLES}
        state["data_version"] = data_version(db)
        state["epoch"] = int(get_meta("epoch", "1", db=db))
    return state


def apply_full_state(state: dict):
    """host のスナップショットで自分のテーブルを丸ごと置き換える（standby 側）。

    差分複製ではなく全量置換にしている理由: 注文は status が後から変わる（完了・取消）ため、
    id の増分だけでは変更を拾えない。変更ログを別に持つより、学園祭1日分
    （数百〜数千件）なら全量でも数十 ms で終わる（scripts/bench_sync.py）。
    さらに data_version の ETag で、変化がない周期は転送自体を省いている。
    """
    try:
        payload = {t: state[t] for t in _REPLICATED_TABLES}
        version = int(state["data_version"])
        epoch = int(state["epoch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError(f"複製データの形式が不正です: {exc}") from None

    with transaction(bump_version=False) as db:
        for t in _DELETE_ORDER:
            db.execute(f"DELETE FROM {t}")
        for t in _INSERT_ORDER:
            cols = _REPLICATED_TABLES[t]
            rows = payload[t]
            if rows:
                db.executemany(
                    f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})",
                    [{c: r.get(c) for c in cols} for r in rows],
                )
        set_meta("data_version", version, db=db)
        set_meta("mirrored_epoch", epoch, db=db)
    return version
