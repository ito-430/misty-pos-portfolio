"""Misty POS - 画面（HTML）"""
from __future__ import annotations

from functools import lru_cache

from flask import Blueprint, current_app, render_template, request, send_from_directory

from .. import runtime
from ..netinfo import advertised_host
from ..qrcode_gen import qr_data_uri

bp = Blueprint("pages", __name__)


@lru_cache(maxsize=32)
def _qr(url: str) -> str:
    # 接続画面は開きっぱなしにされ再読み込みも多い。同じ URL の QR を毎回
    # PNG エンコードし直す必要はないので、URL をキーにキャッシュする
    return qr_data_uri(url)


@bp.get("/")
def connect():
    s = runtime.settings()
    host, candidates = advertised_host(s.advertise_host)
    base = f"http://{host}:{s.port}"
    targets = {k: f"{base}/{k}" for k in ("register", "bartender", "admin")}
    return render_template(
        "connect.html",
        targets=targets,
        qr_codes={k: _qr(v) for k, v in targets.items()},
        state=runtime.current(),
        base_url=base,
        viewed_via=f"http://{request.host}",
        alternatives=[f"http://{ip}:{s.port}" for ip in candidates if ip != host],
    )


def _page(template, active):
    s = runtime.settings()
    return render_template(template, state=runtime.current(), settings=s, active=active)


@bp.get("/register")
def register():
    return _page("register.html", "register")


@bp.get("/bartender")
def bartender():
    return _page("bartender.html", "bartender")


@bp.get("/admin")
def admin():
    return _page("admin.html", "admin")


@bp.get("/service-worker.js")
def service_worker():
    # service worker の制御範囲は配信パス以下に限られる。/static/ から配信すると
    # /register などの画面を制御できないので、ルートから配信する
    resp = send_from_directory(current_app.static_folder, "service-worker.js", mimetype="text/javascript")
    resp.headers["Cache-Control"] = "no-cache"
    return resp
