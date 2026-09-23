"""Misty POS - スタンバイの監視・複製・自動昇格・フェンシング

role=standby のノードだけ、バックグラウンドスレッドでこのループが回る。

  [監視中]  mirror_interval ごとに host の /api/health と /api/sync/export を取得し、
            自分の DB を host と同じ内容に置き換える（ETag が同じなら何もしない）。
     │      失敗が failover_threshold 秒連続したら
     ▼
  [昇格]    epoch を +1 して書き込みを受け付け始め、ホットスポットの起動を試みる。
     │
     ▼
  [フェンス監視] 旧 host が復帰して「アクティブが2台」になっていないかを見張り続け、
            見つけたら epoch の小さい旧 host に書き込み停止（フェンス）を要求する。

時間の計測には time.monotonic() を使う。ホットスポット機が上流のネットに繋がった瞬間に
NTP で時計が補正されると、time.time() の差分が大きく跳んで誤昇格・昇格遅延の原因になる。
"""
from __future__ import annotations

import logging
import threading
import time

import requests

from . import db as dbmod
from . import models, network
from .runtime import EXTENSION_KEY

logger = logging.getLogger("misty.standby")


class PeerUnavailable(Exception):
    pass


class PeerMonitor:
    def __init__(self, app, *, session=None, clock=time.monotonic, sleep=time.sleep,
                 hotspot=network.try_start_hotspot):
        self.app = app
        self.state = app.extensions[EXTENSION_KEY]
        self.settings = self.state.settings
        self.session = session or requests.Session()  # keep-alive で周期ごとの TCP 接続を省く
        self.clock = clock
        self.sleep = sleep
        self.hotspot = hotspot
        self.first_failure_at: float | None = None
        self.etag: str | None = None
        # 一度も複製できていないのに昇格すると、空の DB でホストになってしまう。
        # （スタンバイ機を先に起動し、ホストがまだ立ち上がっていない場合など）
        self.ever_synced = False
        self.peer_fenced = False

    # ------------------------------------------------------------ HTTP

    def _headers(self):
        token = self.settings.cluster_token
        return {"X-Cluster-Token": token} if token else {}

    def _peer_health(self) -> dict:
        try:
            r = self.session.get(f"{self.settings.peer_url}/api/health",
                                 headers=self._headers(), timeout=2)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as exc:
            raise PeerUnavailable(str(exc)) from exc

    def mirror_once(self) -> bool:
        """host の最新状態を取り込む。変更があって適用したら True。"""
        health = self._peer_health()
        if not health.get("is_active"):
            # 相手もスタンバイ/フェンス済みなら、書き込みを受けているホストはどこにもいない
            raise PeerUnavailable("peer is not active")
        headers = self._headers()
        if self.etag:
            headers["If-None-Match"] = self.etag
        try:
            r = self.session.get(f"{self.settings.peer_url}/api/sync/export",
                                 headers=headers, timeout=5)
            if r.status_code == 304:
                return False
            r.raise_for_status()
            snapshot = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise PeerUnavailable(str(exc)) from exc
        with self.app.app_context():
            models.apply_full_state(snapshot)
        self.etag = r.headers.get("ETag")
        return True

    # ------------------------------------------------------------ 状態遷移

    def step(self) -> str:
        """1周期分の処理。戻り値はテストとログのための状態名。"""
        if self.state.is_active:
            return self._watch_old_host()
        try:
            changed = self.mirror_once()
        except PeerUnavailable as exc:
            return self._on_failure(exc)
        self.first_failure_at = None
        self.ever_synced = True
        self.state.peer_healthy = True
        self.state.peer_last_ok = time.time()
        return "mirrored" if changed else "unchanged"

    def _on_failure(self, exc) -> str:
        self.state.peer_healthy = False
        now = self.clock()
        if self.first_failure_at is None:
            self.first_failure_at = now
            logger.info("host unreachable, failover timer started: %s", exc)
        if now - self.first_failure_at < self.settings.failover_threshold_sec:
            return "failing"
        if not self.ever_synced:
            logger.warning("host unreachable but never synced; refusing to promote with empty data")
            return "waiting_first_sync"
        self.promote()
        return "promoted"

    def promote(self):
        with self.app.app_context():
            db = dbmod.get_db()
            observed = int(dbmod.get_meta("mirrored_epoch", self.state.epoch, db=db))
            new_epoch = self.state.promote(observed)
            if new_epoch is None:
                return
            with dbmod.transaction(db):
                dbmod.set_meta("epoch", new_epoch, db=db)
                models._log(db, self.state.device_name, "role_promote", detail={
                    "reason": "peer_unresponsive", "epoch": new_epoch,
                    "threshold_sec": self.settings.failover_threshold_sec,
                })
        ok, detail = self.hotspot()
        logger.warning("STANDBY PROMOTED TO HOST epoch=%s hotspot_ok=%s (%s)", new_epoch, ok, detail)
        with self.app.app_context():
            models.log_action(self.state.device_name, "hotspot_start",
                              detail={"ok": ok, "detail": detail})

    def _watch_old_host(self) -> str:
        """昇格後、旧 host がアクティブのまま復帰していたらフェンスする。"""
        try:
            health = self._peer_health()
        except PeerUnavailable:
            self.state.split_brain = False
            return "peer_down"
        peer_epoch = int(health.get("epoch", 0))
        if not health.get("is_active") or peer_epoch >= self.state.epoch:
            self.state.split_brain = False
            return "no_conflict"
        self.state.split_brain = True
        try:
            r = self.session.post(f"{self.settings.peer_url}/api/cluster/fence",
                                  json={"epoch": self.state.epoch, "by": self.state.device_name},
                                  headers=self._headers(), timeout=3)
            r.raise_for_status()
        except requests.RequestException as exc:
            logger.error("split brain: failed to fence old host: %s", exc)
            return "split_brain"
        self.state.split_brain = False
        with self.app.app_context():
            models.log_action(self.state.device_name, "peer_fenced",
                              detail={"peer_epoch": peer_epoch, "my_epoch": self.state.epoch})
        return "fenced_peer"

    def run_forever(self):
        while True:
            try:
                status = self.step()
            except Exception:  # noqa: BLE001 - 監視スレッドが死ぬと以後フェイルオーバーしない
                logger.exception("monitor step crashed; continuing")
                status = "error"
            logger.debug("monitor: %s", status)
            self.sleep(self.settings.mirror_interval_sec)


def start_background(app):
    state = app.extensions[EXTENSION_KEY]
    if state.configured_role != "standby":
        return None
    monitor = PeerMonitor(app)
    thread = threading.Thread(target=monitor.run_forever, daemon=True, name="misty-peer-monitor")
    thread.start()
    return monitor
