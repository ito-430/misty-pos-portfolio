#!/usr/bin/env python3
"""フェイルオーバーの障害訓練を、実プロセス2つと実 HTTP で再現する。

  1. host と standby を別プロセスで起動し、host で会計する
  2. standby に複製されたことを確認する
  3. host を強制終了（電源断の代わり）し、standby が昇格するまでの秒数を測る
  4. 昇格した standby で会計を続ける
  5. 旧 host を同じ DB で再起動し、standby に自動でフェンスされることを確認する

    python scripts/failover_drill.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST_PORT, STANDBY_PORT = 5301, 5302
INTERVAL, THRESHOLD = 1.0, 4.0


def call(port, path, body=None, timeout=2):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def start(role, port, db, **extra):
    env = {**os.environ, "MISTY_ROLE": role, "MISTY_PORT": str(port), "MISTY_DB_PATH": str(db),
           "MISTY_DEVICE_NAME": f"{role}-{port}", "MISTY_MIRROR_INTERVAL_SEC": str(INTERVAL),
           "MISTY_FAILOVER_THRESHOLD_SEC": str(THRESHOLD), **extra}
    proc = subprocess.Popen([sys.executable, "run.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            call(port, "/api/health")
            return proc
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.1)
    raise RuntimeError(f"{role} did not start")


def wait_until(pred, timeout):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return time.monotonic() - t0
        time.sleep(0.1)
    raise TimeoutError


def main():
    tmp = Path(tempfile.mkdtemp())
    host = start("host", HOST_PORT, tmp / "host.db")
    standby = start("standby", STANDBY_PORT, tmp / "standby.db",
                    MISTY_PEER_URL=f"http://127.0.0.1:{HOST_PORT}", MISTY_SEED_SAMPLE_MENU="0")
    procs = [host, standby]
    try:
        _, menu = call(HOST_PORT, "/api/menu")
        item = menu[0]["id"]
        code, order = call(HOST_PORT, "/api/orders", {"seat_no": 1, "items": [{"menu_item_id": item, "qty": 2}]})
        print(f"[1] host で会計: HTTP {code} order#{order['id']}")

        wait_until(lambda: len(call(STANDBY_PORT, "/api/orders/pending")[1]) == 1, 10)
        print("[2] standby に複製された")

        host.kill()
        host.wait()
        t_kill = time.monotonic()
        wait_until(lambda: call(STANDBY_PORT, "/api/status")[1]["is_active"], 30)
        status = call(STANDBY_PORT, "/api/status")[1]
        print(f"[3] host 停止から {time.monotonic() - t_kill:.1f} 秒で standby が昇格 "
              f"(epoch={status['epoch']}, 閾値 {THRESHOLD}s / 周期 {INTERVAL}s)")

        code, _ = call(STANDBY_PORT, "/api/orders", {"seat_no": 2, "items": [{"menu_item_id": item, "qty": 1}]})
        code_dup, _ = call(STANDBY_PORT, "/api/orders", {"seat_no": 1, "items": [{"menu_item_id": item, "qty": 1}]})
        print(f"[4] 昇格後の会計: 新しい札 HTTP {code} / 複製済みの札1 HTTP {code_dup}（排他が引き継がれている）")

        old = start("host", HOST_PORT, tmp / "host.db")
        procs.append(old)
        elapsed = wait_until(lambda: call(HOST_PORT, "/api/status")[1]["fenced_by_epoch"] == 2, 15)
        code, body = call(HOST_PORT, "/api/chat", {"text": "x"})
        print(f"[5] 旧 host を再起動 → {elapsed:.1f} 秒でフェンス。旧 host への書き込み: HTTP {code} ({body['error']})")
    finally:
        for p in procs:
            p.kill()


if __name__ == "__main__":
    main()
