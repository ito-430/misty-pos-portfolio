"""host/standby の複製・自動昇格・フェンシングを、1プロセス内の2ノードで再現する。"""
import sqlite3

import pytest

from misty.config import ConfigError, Settings
from misty.standby import PeerMonitor
from tests.conftest import FakeNetwork, menu_id

HOST = "http://host.test"
STANDBY = "http://standby.test"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def cluster(make_app):
    host = make_app(role="host", device_name="レジ1")
    standby = make_app(role="standby", device_name="レジ2", peer_url=HOST, seed_sample_menu=False)
    net = FakeNetwork()
    net.add(HOST, host)
    net.add(STANDBY, standby)
    clock = Clock()
    hotspot_calls = []
    monitor = PeerMonitor(standby, session=net, clock=clock, sleep=lambda s: None,
                          hotspot=lambda: hotspot_calls.append(1) or (True, "Success"))
    return host, standby, net, clock, monitor, hotspot_calls


def fail_until_promoted(monitor, clock, max_steps=20):
    statuses = []
    for _ in range(max_steps):
        statuses.append(monitor.step())
        if statuses[-1] == "promoted":
            return statuses
        clock.t += monitor.settings.mirror_interval_sec
    raise AssertionError(statuses)


def test_standby_mirrors_host_and_skips_unchanged_snapshots(cluster):
    host, standby, net, clock, monitor, _ = cluster
    hc = host.test_client()
    gid = menu_id(hc)
    hc.post("/api/orders", json={"seat_no": 1, "items": [{"menu_item_id": gid, "qty": 2}]})

    assert monitor.step() == "mirrored"
    sc = standby.test_client()
    assert [o["seat_no"] for o in sc.get("/api/orders/pending").get_json()] == [1]
    assert sc.get("/api/logs").get_json()[0]["action"] == "order_create"  # 監査ログも複製される

    assert monitor.step() == "unchanged"  # ETag 一致 → 304
    hc.post("/api/chat", json={"text": "氷切れそう"})
    assert monitor.step() == "mirrored"


def test_failover_promotes_standby_with_higher_epoch(cluster):
    host, standby, net, clock, monitor, hotspot_calls = cluster
    gid = menu_id(host.test_client())
    host.test_client().post("/api/orders", json={"seat_no": 1, "items": [{"menu_item_id": gid, "qty": 1}]})
    monitor.step()

    net.down.add(HOST)
    statuses = fail_until_promoted(monitor, clock)
    assert statuses[0] == "failing" and statuses[-1] == "promoted"
    # 閾値 10 秒 / 周期 2.5 秒 → 5 回目の失敗で昇格
    assert len(statuses) == 5

    sc = standby.test_client()
    status = sc.get("/api/status").get_json()
    assert status["is_active"] and status["epoch"] == 2
    assert hotspot_calls == [1]
    # 昇格前に複製済みの注文を引き継ぎ、整理番号の排他も維持される
    r = sc.post("/api/orders", json={"seat_no": 1, "items": [{"menu_item_id": gid, "qty": 1}]})
    assert r.status_code == 409
    assert sc.post("/api/orders", json={"seat_no": 2, "items": [{"menu_item_id": gid, "qty": 1}]}).status_code == 201


def test_recovered_old_host_is_fenced(cluster):
    host, standby, net, clock, monitor, _ = cluster
    monitor.step()
    net.down.add(HOST)
    fail_until_promoted(monitor, clock)

    net.down.clear()  # 旧ホストが epoch=1 のまま復帰 → アクティブが2台
    assert host.test_client().get("/api/status").get_json()["is_active"] is True
    assert monitor.step() == "fenced_peer"

    hs = host.test_client().get("/api/status").get_json()
    assert hs["is_active"] is False and hs["fenced_by_epoch"] == 2
    r = host.test_client().post("/api/chat", json={"text": "x"})
    assert r.status_code == 409
    assert monitor.step() == "no_conflict"


def test_fence_request_with_lower_epoch_is_ignored(make_app):
    host = make_app().test_client()
    r = host.post("/api/cluster/fence", json={"epoch": 1})
    assert r.get_json()["fenced"] is False
    assert host.get("/api/status").get_json()["is_active"] is True


def test_standby_that_never_synced_does_not_promote(cluster):
    host, standby, net, clock, monitor, hotspot_calls = cluster
    net.down.add(HOST)
    for _ in range(10):
        status = monitor.step()
        clock.t += 5
    assert status == "waiting_first_sync"
    assert standby.test_client().get("/api/status").get_json()["is_active"] is False
    assert hotspot_calls == []


def test_brief_outage_below_threshold_does_not_promote(cluster):
    host, standby, net, clock, monitor, _ = cluster
    monitor.step()
    net.down.add(HOST)
    for _ in range(3):
        assert monitor.step() == "failing"
        clock.t += 2.5
    net.down.clear()
    assert monitor.step() in ("mirrored", "unchanged")
    net.down.add(HOST)
    assert monitor.step() == "failing"  # タイマーは復旧でリセットされている


def test_cluster_token_is_required_when_configured(make_app):
    host = make_app(cluster_token="s3cret").test_client()
    assert host.get("/api/sync/export").status_code == 401
    assert host.get("/api/sync/export", headers={"X-Cluster-Token": "s3cret"}).status_code == 200


def test_malformed_snapshot_rolls_back(cluster):
    host, standby, net, clock, monitor, _ = cluster
    monitor.step()
    from misty import models
    with standby.app_context():
        before = len(models.list_menu())
        with pytest.raises(sqlite3.IntegrityError):  # name の NOT NULL 違反
            models.apply_full_state({"menu_items": [{"id": 1}], "orders": [], "order_items": [],
                                     "chat_messages": [], "operation_log": [],
                                     "data_version": 1, "epoch": 1})
        assert len(models.list_menu()) == before


@pytest.mark.parametrize("env,msg", [
    ({"MISTY_ROLE": "stanby"}, "MISTY_ROLE"),
    ({"MISTY_PORT": "abc"}, "MISTY_PORT"),
    ({"MISTY_FAILOVER_THRESHOLD_SEC": "3"}, "2倍以上"),
])
def test_invalid_settings_fail_fast(env, msg):
    with pytest.raises(ConfigError, match=msg):
        Settings.from_env(env)


def test_sync_export_is_gzipped_when_requested(make_app):
    import gzip
    import json
    host = make_app().test_client()
    r = host.get("/api/sync/export", headers={"Accept-Encoding": "gzip"})
    assert r.headers["Content-Encoding"] == "gzip"
    assert json.loads(gzip.decompress(r.data))["menu_items"]
