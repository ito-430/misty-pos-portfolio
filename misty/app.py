"""Misty POS - アプリケーションファクトリ"""
from __future__ import annotations

import logging

from flask import Flask

from . import db as dbmod
from . import models, standby
from .config import Settings
from .routes.api import bp as api_bp
from .routes.pages import bp as pages_bp
from .runtime import EXTENSION_KEY, RuntimeState

logger = logging.getLogger("misty")

_SAMPLE_MENU = [
    ("ジンジャーエール", "ドリンク", 300, 40),
    ("ウーロン茶", "ドリンク", 300, 40),
    ("オレンジジュース", "ドリンク", 300, 40),
    ("ポテトチップス", "スナック", 200, 30),
    ("ミックスナッツ", "スナック", 200, 30),
]


def create_app(settings: Settings | None = None, *, start_monitor: bool = True) -> Flask:
    settings = settings or Settings.from_env()
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json.ensure_ascii = False
    app.json.sort_keys = False

    # スキーマ作成と epoch の読み出しは、リクエストを受ける前に1本の接続で済ませる
    conn = dbmod.connect(settings.db_path)
    try:
        dbmod.init_db(conn)
        epoch = int(dbmod.get_meta("epoch", "1", db=conn))
    finally:
        conn.close()

    app.extensions[EXTENSION_KEY] = RuntimeState(settings, epoch=epoch)
    app.teardown_appcontext(dbmod.close_db)
    app.register_blueprint(pages_bp)
    app.register_blueprint(api_bp)

    # スタンバイは host の内容で上書きされるので、サンプルを入れるのは host だけ
    if settings.seed_sample_menu and settings.role == "host":
        with app.app_context():
            if not models.list_menu():
                for name, category, price, stock in _SAMPLE_MENU:
                    models.create_menu_item("system", name, category, price, stock)
                logger.info("menu_items was empty; seeded %d sample items", len(_SAMPLE_MENU))

    logger.info("start role=%s device=%s epoch=%s port=%s db=%s",
                settings.role, settings.device_name, epoch, settings.port, settings.db_path)
    if start_monitor:
        app.extensions["misty.monitor"] = standby.start_background(app)
    return app


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    app = create_app()
    s = app.extensions[EXTENSION_KEY].settings
    try:
        # Flask 付属の開発サーバーではなく waitress を使う。Windows で動き、
        # PyInstaller で固めやすく、スレッドプールで同時接続を捌ける純 Python の WSGI サーバー
        from waitress import serve
    except ImportError:  # pragma: no cover - 依存が欠けていても起動だけはできるように
        logger.warning("waitress not installed; falling back to Flask dev server")
        app.run(host=s.host_bind, port=s.port, threaded=True)
        return
    serve(app, host=s.host_bind, port=s.port, threads=8, ident="misty-pos")


if __name__ == "__main__":
    main()
