"""Misty POS - ノードの実行時状態（役割・エポック・フェンシング）

Settings.role は「起動時にどちらとして立ち上げたか」という静的な値で、
RuntimeState は「今この瞬間に書き込みを受け付けてよいか」という動的な値を持つ。

エポック（epoch）について:
  host は epoch=1 で始まり、standby は複製時に host の epoch を記録しておく。
  standby が昇格するときは epoch を +1 する。旧 host が復帰して「アクティブが2台」
  （スプリットブレイン）になったとき、epoch の大きい側が小さい側をフェンス
  （読み取り専用化）する。Raft の term と同じ発想で、どちらを正とするかを
  時刻ではなく単調増加の番号で決めるので、端末間の時計ずれに影響されない。
"""
from __future__ import annotations

import threading
import time

from flask import current_app

from .config import Settings

EXTENSION_KEY = "misty"


class RuntimeState:
    def __init__(self, settings: Settings, epoch: int = 1):
        self.settings = settings
        self.device_name = settings.device_name
        self.configured_role = settings.role
        self.is_active = settings.role == "host"
        self.epoch = epoch
        self.promoted_at: float | None = None
        self.fenced_by_epoch: int | None = None
        # 自分がアクティブなのに、epoch の小さい別のアクティブが見えている状態
        self.split_brain = False
        self.peer_healthy = True
        self.peer_last_ok: float | None = None
        self.lock = threading.Lock()

    def promote(self, observed_epoch: int) -> int | None:
        """standby → host。すでにアクティブなら None。新しい epoch を返す。"""
        with self.lock:
            if self.is_active:
                return None
            self.epoch = max(self.epoch, observed_epoch) + 1
            self.is_active = True
            self.promoted_at = time.time()
            return self.epoch

    def fence(self, by_epoch: int) -> bool:
        """自分より大きい epoch を名乗るノードからの要求でだけ書き込みを止める。"""
        with self.lock:
            if by_epoch <= self.epoch:
                return False
            self.is_active = False
            self.fenced_by_epoch = by_epoch
            return True

    def as_status_dict(self) -> dict:
        return {
            "device_name": self.device_name,
            "configured_role": self.configured_role,
            "is_active": self.is_active,
            "epoch": self.epoch,
            "promoted_at": self.promoted_at,
            "fenced_by_epoch": self.fenced_by_epoch,
            "split_brain": self.split_brain,
            "peer_healthy": self.peer_healthy,
        }


def current() -> RuntimeState:
    return current_app.extensions[EXTENSION_KEY]


def settings() -> Settings:
    return current().settings
