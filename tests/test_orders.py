"""会計・状態遷移・在庫の整合性。"""
import threading

import pytest

from misty import models
from tests.conftest import menu_id, stock_of


def order(client, seat, items, **extra):
    return client.post("/api/orders", json={"seat_no": seat, "items": items, **extra})


def test_checkout_decrements_stock_and_writes_log(client):
    gid = menu_id(client)
    r = order(client, 1, [{"menu_item_id": gid, "qty": 3}])
    assert r.status_code == 201
    assert r.get_json()["total_amount"] == 900
    assert stock_of(client, gid) == 37
    actions = [log["action"] for log in client.get("/api/logs").get_json()]
    assert actions[0] == "order_create"


def test_duplicate_lines_are_summed_before_stock_check(client):
    # 修正前: 各行を個別に在庫判定していたため、在庫40に対して30+30が通っていた
    gid = menu_id(client)
    r = order(client, 1, [{"menu_item_id": gid, "qty": 30}, {"menu_item_id": gid, "qty": 30}])
    assert r.status_code == 409
    assert r.get_json()["error"] == "out_of_stock"
    assert stock_of(client, gid) == 40


def test_seat_is_exclusive_while_pending(client):
    gid = menu_id(client)
    first = order(client, 5, [{"menu_item_id": gid, "qty": 1}]).get_json()
    r = order(client, 5, [{"menu_item_id": gid, "qty": 1}])
    assert r.status_code == 409 and r.get_json()["error"] == "seat_occupied"
    client.post(f"/api/orders/{first['id']}/complete")
    assert order(client, 5, [{"menu_item_id": gid, "qty": 1}]).status_code == 201


def test_retry_with_same_request_id_does_not_double_charge(client):
    gid = menu_id(client)
    body = [{"menu_item_id": gid, "qty": 2}]
    r1 = order(client, 3, body, request_id="req-abc")
    r2 = order(client, 3, body, request_id="req-abc")
    assert (r1.status_code, r2.status_code) == (201, 200)
    assert r1.get_json()["id"] == r2.get_json()["id"]
    assert stock_of(client, gid) == 38


def _run_concurrently(n, fn):
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        results[i] = fn(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_concurrent_checkouts_on_same_seat_only_one_wins(app, client):
    gid = menu_id(client)
    codes = _run_concurrently(8, lambda i: order(app.test_client(), 7, [{"menu_item_id": gid, "qty": 1}]).status_code)
    assert sorted(codes) == [201] + [409] * 7
    assert stock_of(client, gid) == 39


def test_concurrent_checkouts_never_oversell(app, client):
    gid = menu_id(client)
    client.put(f"/api/menu/{gid}", json={"stock_qty": 5})
    codes = _run_concurrently(
        12, lambda i: order(app.test_client(), i + 1, [{"menu_item_id": gid, "qty": 1}]).status_code)
    assert codes.count(201) == 5
    assert stock_of(client, gid) == 0


def test_concurrent_voids_restore_stock_exactly_once(app, client):
    gid = menu_id(client)
    oid = order(client, 1, [{"menu_item_id": gid, "qty": 10}]).get_json()["id"]
    assert stock_of(client, gid) == 30
    codes = _run_concurrently(8, lambda i: app.test_client().post(f"/api/orders/{oid}/void", json={}).status_code)
    assert codes.count(200) == 1
    assert stock_of(client, gid) == 40


def test_complete_twice_is_a_conflict_not_a_second_log(client):
    gid = menu_id(client)
    oid = order(client, 1, [{"menu_item_id": gid, "qty": 1}]).get_json()["id"]
    assert client.post(f"/api/orders/{oid}/complete").status_code == 200
    r = client.post(f"/api/orders/{oid}/complete")
    assert r.status_code == 409 and r.get_json()["error"] == "invalid_transition"
    actions = [log["action"] for log in client.get("/api/logs").get_json()]
    assert actions.count("order_complete") == 1


def test_uncomplete_is_blocked_when_seat_was_reused(client):
    gid = menu_id(client)
    first = order(client, 2, [{"menu_item_id": gid, "qty": 1}]).get_json()["id"]
    client.post(f"/api/orders/{first}/complete")
    order(client, 2, [{"menu_item_id": gid, "qty": 1}])
    r = client.post(f"/api/orders/{first}/uncomplete")
    assert r.status_code == 409 and r.get_json()["error"] == "seat_occupied"


def test_void_of_completed_order_restores_stock(client):
    gid = menu_id(client)
    oid = order(client, 1, [{"menu_item_id": gid, "qty": 4}]).get_json()["id"]
    client.post(f"/api/orders/{oid}/complete")
    assert client.post(f"/api/orders/{oid}/void", json={"reason": "打ち間違い"}).status_code == 200
    assert stock_of(client, gid) == 40
    summary = client.get("/api/sales/summary").get_json()
    assert summary == {"order_count": 0, "total_amount": 0, "ranking": []}


def test_price_change_after_checkout_does_not_rewrite_history(client):
    gid = menu_id(client)
    order(client, 1, [{"menu_item_id": gid, "qty": 1}])
    client.put(f"/api/menu/{gid}", json={"price": 500})
    assert client.get("/api/sales/summary").get_json()["total_amount"] == 300


@pytest.mark.parametrize("method,path,kwargs", [
    ("post", "/api/menu", {"data": "not json", "content_type": "application/json"}),
    ("post", "/api/menu", {"json": {"name": "x", "price": "abc"}}),
    ("post", "/api/menu", {"json": {"name": "x", "price": -1}}),
    ("get", "/api/orders/recent?limit=abc", {}),
    ("get", "/api/orders/recent?status=unknown", {}),
    ("post", "/api/orders", {"json": {"seat_no": 99, "items": [{"menu_item_id": 1, "qty": 1}]}}),
    ("post", "/api/orders", {"json": {"seat_no": 1, "items": [{"menu_item_id": 1, "qty": "a"}]}}),
    ("post", "/api/orders", {"json": {"seat_no": 1, "items": [{"menu_item_id": 1, "qty": 0}]}}),
    ("post", "/api/orders", {"json": {"seat_no": 1, "items": []}}),
    ("post", "/api/orders", {"json": {"seat_no": 1, "items": "oops"}}),
    ("put", "/api/menu/1", {"json": {"stock": 3}}),
    ("post", "/api/chat", {"json": {"text": "   "}}),
])
def test_bad_input_returns_json_4xx_not_500(client, method, path, kwargs):
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 400
    assert r.is_json and r.get_json()["message"]


def test_unknown_order_is_404(client):
    r = client.post("/api/orders/9999/complete")
    assert r.status_code == 404 and r.get_json()["error"] == "not_found"


def test_standby_rejects_writes_but_serves_reads(make_app):
    standby = make_app(role="standby").test_client()
    assert standby.get("/api/menu").status_code == 200
    r = standby.post("/api/chat", json={"text": "hi"})
    assert r.status_code == 409 and r.get_json()["error"] == "standby"


def test_csv_export_has_bom_for_excel(client):
    body = client.get("/api/export/summary.csv").data
    assert body.startswith("﻿".encode("utf-8"))


def test_device_name_header_is_url_decoded(client):
    gid = menu_id(client)
    client.post("/api/orders", json={"seat_no": 1, "items": [{"menu_item_id": gid, "qty": 1}]},
                headers={"X-Device-Name": "%E3%83%AC%E3%82%B8-1A2B"})
    assert client.get("/api/logs").get_json()[0]["device_name"] == "レジ-1A2B"


def test_stock_constraint_is_enforced_by_the_database(app):
    # アプリのチェックをすり抜けても、DB の CHECK 制約が負の在庫を拒否する
    import sqlite3
    with app.app_context():
        db = models.get_db()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE menu_items SET stock_qty = -1 WHERE id = 1")
