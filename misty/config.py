"""Misty POS - 起動設定

設定はすべて環境変数から読み、イミュータブルな Settings にまとめてアプリごとに保持する。
モジュール変数（グローバル）にしない理由は2つある。

1. 1プロセスの中でホスト/スタンバイの2ノードを同時に立ち上げ、フェイルオーバーを
   統合テストで再現するため（tests/test_failover.py）。
2. 不正な値を起動時に検出して即座に落とすため。本番中に「role のタイポで
   スタンバイのつもりがホストとして起動していた」と気付くのが一番まずい。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping

BASE_DIR = Path(__file__).resolve().parent.parent

ROLES = ("host", "standby")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    # 画面と操作ログに出る端末名。操作ログは後から照合に使うので、端末ごとに一意にする
    device_name: str = "レジ1"
    # host: 起動直後から書き込みを受け付ける / standby: 監視と複製のみ、条件を満たすと昇格する
    role: str = "host"
    # standby が監視する host のURL。Windows のモバイルホットスポットは既定で
    # 192.168.137.1 をゲートウェイに使うので、それを既定値にしている
    peer_url: str = "http://192.168.137.1:5000"
    port: int = 5000
    host_bind: str = "0.0.0.0"
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "misty.db")

    # 複製の周期と、昇格までの連続失敗時間。RPO（失いうる直近データ）は最大で
    # mirror_interval_sec、昇格までの時間はおおむね threshold + interval + HTTPタイムアウト
    mirror_interval_sec: float = 2.5
    failover_threshold_sec: float = 10.0

    # host/standby 間の共有トークン。設定した場合のみ /api/sync/* と /api/cluster/* で要求する
    cluster_token: str = ""

    # 取消・Undo ボタンを出しておく時間。サーバー側はいつでも取消を受け付ける
    # （管理画面からの訂正があるため）ので、これは UI 上の表示時間の単一ソース
    undo_checkout_sec: int = 60
    undo_complete_sec: int = 30

    # 整理番号札の範囲（物理的な札の枚数に合わせる）
    seat_no_min: int = 1
    seat_no_max: int = 20

    menu_sheet_csv_url: str = ""
    # QR に埋め込むアドレスを固定したいとき（複数NICで自動判定が外れるとき）だけ指定する
    advertise_host: str = ""
    seed_sample_menu: bool = True

    def __post_init__(self):
        if self.role not in ROLES:
            raise ConfigError(f"MISTY_ROLE は {ROLES} のいずれか: {self.role!r}")
        if not (1 <= self.port <= 65535):
            raise ConfigError(f"MISTY_PORT が範囲外: {self.port}")
        if self.mirror_interval_sec <= 0:
            raise ConfigError("MISTY_MIRROR_INTERVAL_SEC は正の数")
        # 閾値が周期以下だと、1回の取りこぼしで即昇格してしまう
        if self.failover_threshold_sec < 2 * self.mirror_interval_sec:
            raise ConfigError(
                "MISTY_FAILOVER_THRESHOLD_SEC は MIRROR_INTERVAL_SEC の2倍以上にする"
                f"（現在 {self.failover_threshold_sec} < 2 × {self.mirror_interval_sec}）"
            )
        if self.seat_no_min > self.seat_no_max:
            raise ConfigError("MISTY_SEAT_NO_MIN > MISTY_SEAT_NO_MAX")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, **overrides) -> "Settings":
        env = os.environ if environ is None else environ
        values = {}
        for f in fields(cls):
            key = "MISTY_" + f.name.upper()
            if key not in env:
                continue
            raw = env[key]
            try:
                values[f.name] = _coerce(f.name, raw)
            except ValueError as exc:
                raise ConfigError(f"{key}={raw!r} を解釈できない: {exc}") from None
        values.update(overrides)
        if "role" in values:
            values["role"] = str(values["role"]).lower()
        return cls(**values)


_INT_FIELDS = {"port", "undo_checkout_sec", "undo_complete_sec", "seat_no_min", "seat_no_max"}
_FLOAT_FIELDS = {"mirror_interval_sec", "failover_threshold_sec"}


def _coerce(name: str, raw: str):
    if name in _INT_FIELDS:
        return int(raw)
    if name in _FLOAT_FIELDS:
        return float(raw)
    if name == "db_path":
        return Path(raw)
    if name == "seed_sample_menu":
        return raw.strip().lower() not in ("0", "false", "no", "")
    if name == "peer_url":
        return raw.rstrip("/")
    return raw
