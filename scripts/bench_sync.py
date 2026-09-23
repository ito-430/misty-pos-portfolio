#!/usr/bin/env python3
"""全量スナップショット複製のコストを測る。

「差分ではなく全量を毎周期送る」設計が学園祭の規模で成立するかを数字で確かめるためのもの。
生成するデータは同じ形の注文の繰り返しなので、gzip の圧縮率は実データより高めに出る。
    python scripts/bench_sync.py            # 既定: 注文 3,000 件
    python scripts/bench_sync.py 10000
"""
from __future__ import annotations

import gzip
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from misty import db as dbmod  # noqa: E402
from misty import models  # noqa: E402
from misty.app import create_app  # noqa: E402
from misty.config import Settings  # noqa: E402


def timed(fn, repeat=5):
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples)


def main(n_orders: int):
    tmp = Path(tempfile.mkdtemp())
    host = create_app(Settings.from_env({}, db_path=tmp / "host.db"), start_monitor=False)
    standby = create_app(Settings.from_env({}, db_path=tmp / "standby.db", role="standby",
                                           seed_sample_menu=False), start_monitor=False)

    with host.app_context():
        models.update_menu_item("bench", 1, {"stock_qty": 100_000})
        models.update_menu_item("bench", 2, {"stock_qty": 100_000})
        db = dbmod.get_db()
        with dbmod.transaction(db):
            for i in range(n_orders):
                cur = db.execute(
                    "INSERT INTO orders (seat_no, status, created_at, device_name, total_amount) "
                    "VALUES (?, 'done', ?, 'bench', 600)", (i % 20 + 1, models.now_iso()))
                db.executemany(
                    "INSERT INTO order_items (order_id, menu_item_id, menu_item_name, qty, unit_price) "
                    "VALUES (?, ?, ?, 1, 300)",
                    [(cur.lastrowid, 1, "ジンジャーエール"), (cur.lastrowid, 2, "ウーロン茶")])
                models._log(db, "bench", "order_create", order_id=cur.lastrowid, detail={"seat_no": i % 20 + 1})

        export_ms = timed(models.export_full_state)
        snapshot = models.export_full_state()
        payload = json.dumps(snapshot, ensure_ascii=False).encode()
        version_ms = timed(lambda: dbmod.data_version(), repeat=50)

    with standby.app_context():
        apply_ms = timed(lambda: models.apply_full_state(snapshot))

    print(f"orders={n_orders:,}  order_items={len(snapshot['order_items']):,}  "
          f"operation_log={len(snapshot['operation_log']):,}")
    gz = gzip.compress(payload, compresslevel=5)
    gzip_ms = timed(lambda: gzip.compress(payload, compresslevel=5))
    print(f"snapshot JSON size       : {len(payload) / 1024:,.0f} KiB "
          f"(gzip: {len(gz) / 1024:,.0f} KiB, {gzip_ms:,.1f} ms)")
    print(f"export (host, median)    : {export_ms:,.1f} ms")
    print(f"apply  (standby, median) : {apply_ms:,.1f} ms")
    print(f"ETag check (host, median): {version_ms:,.3f} ms  ← 変化がない周期はこれだけ")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3000)
