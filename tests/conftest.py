import pytest
import requests

from misty.app import create_app
from misty.config import Settings


@pytest.fixture
def make_app(tmp_path):
    counter = {"n": 0}

    def _make(**overrides):
        counter["n"] += 1
        overrides.setdefault("db_path", tmp_path / f"node{counter['n']}.db")
        settings = Settings.from_env({}, **overrides)
        return create_app(settings, start_monitor=False)

    return _make


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def client(app):
    return app.test_client()


def menu_id(client, name="ジンジャーエール"):
    return next(m["id"] for m in client.get("/api/menu").get_json() if m["name"] == name)


def stock_of(client, item_id):
    return next(m["stock_qty"] for m in client.get("/api/menu").get_json() if m["id"] == item_id)


class _Resp:
    """werkzeug のテストレスポンスを requests.Response 風に見せる。"""

    def __init__(self, r):
        self.status_code = r.status_code
        self.headers = r.headers
        self._r = r

    def json(self):
        return self._r.get_json()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeNetwork:
    """URL のホスト部で Flask アプリに振り分ける、プロセス内の擬似ネットワーク。

    down にしたノード宛の通信は ConnectionError になる（電源断・Wi-Fi 断の再現）。
    """

    def __init__(self):
        self.nodes = {}
        self.down = set()

    def add(self, base_url, app):
        self.nodes[base_url] = app

    def _route(self, url):
        for base, app in self.nodes.items():
            if url.startswith(base):
                if base in self.down:
                    raise requests.ConnectionError(f"{base} is down")
                return app.test_client(), url[len(base):]
        raise requests.ConnectionError(f"no route to {url}")

    def get(self, url, headers=None, timeout=None):
        client, path = self._route(url)
        return _Resp(client.get(path, headers=headers or {}))

    def post(self, url, json=None, headers=None, timeout=None):
        client, path = self._route(url)
        return _Resp(client.post(path, json=json, headers=headers or {}))
