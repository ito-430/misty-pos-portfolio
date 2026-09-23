import pytest
import requests

from misty import netinfo, sheets
from misty.errors import UpstreamError, ValidationError


@pytest.mark.parametrize("url,expected", [
    ("https://docs.google.com/spreadsheets/d/AbC-12_x/edit#gid=345",
     "https://docs.google.com/spreadsheets/d/AbC-12_x/export?format=csv&gid=345"),
    ("https://docs.google.com/spreadsheets/d/AbC/edit",
     "https://docs.google.com/spreadsheets/d/AbC/export?format=csv&gid=0"),
    ("https://example.com/menu.csv?format=csv", "https://example.com/menu.csv?format=csv"),
])
def test_normalize_sheet_url(url, expected):
    assert sheets.normalize_sheet_url(url) == expected


def test_normalize_rejects_non_http():
    with pytest.raises(ValidationError):
        sheets.normalize_sheet_url("file:///etc/passwd")


@pytest.mark.parametrize("raw,expected", [("¥1,200", 1200), ("300円", 300), ("", ""), ("abc", "abc"), (None, None)])
def test_parse_number(raw, expected):
    assert sheets.parse_number(raw) == expected


def test_parse_csv_tolerates_bom_and_padded_headers():
    text = "﻿ 商品名 ,カテゴリー,価格,在庫数\nジン,酒,¥500,10\n,,,\n"
    assert sheets.parse_menu_csv(text) == [{"name": "ジン", "category": "酒", "price": 500, "stock_qty": 10}]


def test_parse_csv_without_name_column_is_an_error():
    with pytest.raises(ValidationError):
        sheets.parse_menu_csv("foo,bar\n1,2\n")


class FakeResp:
    def __init__(self, status, body=b"", ctype="text/csv"):
        self.status_code = status
        self.content = body
        self.headers = {"Content-Type": ctype}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


URL = "https://docs.google.com/spreadsheets/d/X/edit"


def test_fetch_retries_transient_errors_then_succeeds():
    s = FakeSession([requests.ConnectionError("x"), FakeResp(503),
                     FakeResp(200, "商品名,価格\nA,100\n".encode())])
    sleeps = []
    rows = sheets.fetch_menu_rows(URL, session=s, sleep=sleeps.append)
    assert rows[0]["name"] == "A" and s.calls == 3
    assert sleeps == [0.5, 1.0]  # 指数バックオフ


def test_fetch_does_not_retry_client_errors():
    s = FakeSession([FakeResp(404)])
    with pytest.raises(UpstreamError):
        sheets.fetch_menu_rows(URL, session=s, sleep=lambda _: None)
    assert s.calls == 1


def test_fetch_detects_login_page_for_unshared_sheet():
    s = FakeSession([FakeResp(200, b"<!DOCTYPE html><html>Sign in</html>", "text/html; charset=utf-8")])
    with pytest.raises(UpstreamError) as ei:
        sheets.fetch_menu_rows(URL, session=s, sleep=lambda _: None)
    assert ei.value.code == "sheet_not_shared"


def test_import_keeps_stock_of_already_sold_items(client):
    gid = next(m["id"] for m in client.get("/api/menu").get_json() if m["name"] == "ウーロン茶")
    client.post("/api/orders", json={"seat_no": 1, "items": [{"menu_item_id": gid, "qty": 5}]})
    from misty import models
    with client.application.app_context():
        result = models.import_menu_rows([
            {"name": "ウーロン茶", "category": "ソフトドリンク", "price": 350, "stock_qty": 100},
            {"name": "ジンジャーエール", "category": "ソフトドリンク", "price": 300, "stock_qty": 80},
            {"name": "新メニュー", "category": "フード", "price": 400, "stock_qty": 10},
            {"name": "壊れた行", "category": "", "price": "たかい", "stock_qty": 1},
        ], "test")
    assert (result["created"], result["updated"]) == (1, 2)
    assert result["skipped"][0]["row"] == 5
    menu = {m["name"]: m for m in client.get("/api/menu").get_json()}
    assert menu["ウーロン茶"]["stock_qty"] == 35 and menu["ウーロン茶"]["price"] == 350
    assert menu["ジンジャーエール"]["stock_qty"] == 80


def test_candidate_ips_prefer_hotspot_side_over_default_route():
    ips = netinfo.candidate_ips(host_ips=["10.20.30.40", "192.168.137.1", "127.0.0.1", "169.254.1.2"],
                                route_ip="10.20.30.40")
    assert ips == ["192.168.137.1", "10.20.30.40"]


def test_candidate_ips_fall_back_to_route_ip():
    assert netinfo.candidate_ips(host_ips=[], route_ip="172.16.0.5") == ["172.16.0.5"]


class _Proc:
    def __init__(self, rc, out=""):
        self.returncode, self.stdout, self.stderr = rc, out, ""


def test_hotspot_reports_failure_status(monkeypatch):
    from misty import network
    monkeypatch.setattr(network.platform, "system", lambda: "Windows")
    failed = network.try_start_hotspot(runner=lambda *a, **k: _Proc(2, "NoConnectionProfile"))
    assert failed == (False, "NoConnectionProfile")
    assert network.try_start_hotspot(runner=lambda *a, **k: _Proc(0, "Success")) == (True, "Success")


def test_hotspot_is_skipped_off_windows(monkeypatch):
    from misty import network
    monkeypatch.setattr(network.platform, "system", lambda: "Linux")
    assert network.try_start_hotspot()[0] is False
