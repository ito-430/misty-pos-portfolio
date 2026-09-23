"""Misty POS - JSON API

画面はすべてこの API をポーリングする（WebSocket は使わない）。
端末数が数台・更新周期が2〜3秒なら HTTP ポーリングで負荷は問題にならず、
iPad がスリープ復帰やホットスポット切り替えで接続を失っても、次の周期で自然に
回復する。常時接続の再接続処理を書くより、障害時の振る舞いが単純になる。

エラーはすべて {"error": <コード>, "message": <人向けの文>} の JSON で返す。
"""
from __future__ import annotations

import csv
import functools
import gzip
import hmac
import io
import logging
from urllib.parse import unquote

from flask import Blueprint, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from .. import db as dbmod
from .. import models, runtime, sheets
from ..errors import MistyError, NotActive, Unauthorized, ValidationError

bp = Blueprint("api", __name__, url_prefix="/api")
logger = logging.getLogger("misty.api")


# ------------------------------------------------------------ 共通

@bp.errorhandler(MistyError)
def _handle_domain_error(exc: MistyError):
    return jsonify(exc.to_dict()), exc.status


@bp.errorhandler(HTTPException)
def _handle_http_error(exc: HTTPException):
    return jsonify({"error": exc.name.lower().replace(" ", "_"), "message": exc.description}), exc.code


@bp.errorhandler(Exception)
def _handle_unexpected(exc: Exception):
    logger.exception("unhandled error on %s %s", request.method, request.path)
    return jsonify({"error": "internal_error", "message": "サーバー内部でエラーが発生しました"}), 500


def require_active(fn):
    """スタンバイ中・フェンス済みのノードでは書き込みを拒否する（単一ライターの保証）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        state = runtime.current()
        if not state.is_active:
            msg = "この端末は現在スタンバイ中です"
            if state.fenced_by_epoch:
                msg = ("新しいホストに切り替わったため、この端末では会計できません。"
                       "接続画面から新しいホストに繋ぎ直してください")
            raise NotActive(msg)
        return fn(*args, **kwargs)
    return wrapper


def require_cluster_token(fn):
    """ノード間 API の認証。トークン未設定なら素通し（同じホットスポット内を信頼境界とみなす運用）。"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        expected = runtime.settings().cluster_token
        if expected:
            given = request.headers.get("X-Cluster-Token", "")
            if not hmac.compare_digest(given, expected):
                raise Unauthorized("cluster token が一致しません")
        return fn(*args, **kwargs)
    return wrapper


def _body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValidationError("リクエスト本文は JSON オブジェクトで送ってください")
    return data


def _int_arg(name, default, lo, hi) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        return max(lo, min(hi, int(raw)))
    except ValueError:
        raise ValidationError(f"{name} は整数で指定してください") from None


def device_name() -> str:
    # 同じサーバーに複数のブラウザ端末が繋がるので、操作ログの「誰が」は各端末が名乗る
    name = unquote(request.headers.get("X-Device-Name") or "").strip()[:30]
    return name or runtime.current().device_name


# ------------------------------------------------------------ 状態・ノード間

@bp.get("/health")
def health():
    return jsonify({"ok": True, **runtime.current().as_status_dict()})


@bp.get("/status")
def status():
    return jsonify(runtime.current().as_status_dict())


@bp.get("/sync/export")
@require_cluster_token
def sync_export():
    # 世代番号の取得は1行 SELECT。変化がなければ全テーブルの読み出しもJSON化もしない
    state = runtime.current()
    etag = f'"{state.epoch}-{dbmod.data_version()}"'
    if request.headers.get("If-None-Match") == etag:
        return Response(status=304, headers={"ETag": etag})
    payload = models.export_full_state()
    resp = jsonify(payload)
    # export は別スナップショットで読むので、その間の書き込みも含めて ETag を作り直す
    resp.headers["ETag"] = f'"{state.epoch}-{payload["data_version"]}"'
    # 注文 3,000 件で JSON は約 1.7 MB になる。ホットスポットの帯域を会計の通信と
    # 奪い合わないよう gzip で送る（同じキー名が繰り返す JSON なので大きく縮む。scripts/bench_sync.py で計測）。
    # requests は Accept-Encoding: gzip を自動で付け、自動で展開する
    if "gzip" in request.headers.get("Accept-Encoding", ""):
        resp.set_data(gzip.compress(resp.get_data(), compresslevel=5))
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    return resp


@bp.post("/cluster/fence")
@require_cluster_token
def cluster_fence():
    data = _body()
    by_epoch = models._int(data.get("epoch"), "epoch", 1)
    state = runtime.current()
    fenced = state.fence(by_epoch)
    if fenced:
        models.log_action(state.device_name, "role_fenced",
                          detail={"by_epoch": by_epoch, "by": str(data.get("by", ""))[:30]})
    return jsonify({"fenced": fenced, **state.as_status_dict()})


# ------------------------------------------------------------ メニュー

@bp.get("/menu")
def get_menu():
    return jsonify(models.list_menu(active_only=request.args.get("active_only") == "1"))


@bp.post("/menu")
@require_active
def create_menu():
    d = _body()
    item = models.create_menu_item(
        device_name(), d.get("name"), d.get("category", ""), d.get("price"),
        d.get("stock_qty", 0), d.get("is_active", True),
    )
    return jsonify(item), 201


@bp.put("/menu/<int:item_id>")
@require_active
def update_menu(item_id):
    return jsonify(models.update_menu_item(device_name(), item_id, _body()))


@bp.post("/menu/import")
@require_active
def import_menu():
    d = _body()
    sheet_url = (d.get("sheet_url") or runtime.settings().menu_sheet_csv_url or "").strip()
    rows = sheets.fetch_menu_rows(sheet_url)
    return jsonify(models.import_menu_rows(rows, device_name()))


# ------------------------------------------------------------ 注文

@bp.get("/orders/pending")
def orders_pending():
    return jsonify(models.list_pending_orders())


@bp.get("/orders/recent")
def orders_recent():
    status = request.args.get("status")
    if status not in (None, "pending", "done", "void"):
        raise ValidationError("status が不正です")
    return jsonify(models.list_recent_orders(limit=_int_arg("limit", 50, 1, 500), status=status))


@bp.post("/orders")
@require_active
def create_order():
    d = _body()
    s = runtime.settings()
    order, created = models.place_order(
        d.get("seat_no"), d.get("items"), device_name(),
        request_id=d.get("request_id"), seat_range=(s.seat_no_min, s.seat_no_max),
    )
    # 再送で既存の注文を返した場合は 200、新規作成は 201
    return jsonify(order), (201 if created else 200)


@bp.post("/orders/<int:order_id>/complete")
@require_active
def complete_order(order_id):
    return jsonify(models.complete_order(order_id, device_name()))


@bp.post("/orders/<int:order_id>/uncomplete")
@require_active
def uncomplete_order(order_id):
    return jsonify(models.uncomplete_order(order_id, device_name()))


@bp.post("/orders/<int:order_id>/void")
@require_active
def void_order(order_id):
    d = request.get_json(silent=True) or {}
    return jsonify(models.void_order(order_id, device_name(), reason=d.get("reason", "")))


# ------------------------------------------------------------ チャット

@bp.get("/chat")
def chat_list():
    return jsonify(models.list_chat_messages(
        after_id=_int_arg("after", 0, 0, 2**62), limit=_int_arg("limit", 200, 1, 500)))


@bp.post("/chat")
@require_active
def chat_send():
    d = _body()
    return jsonify(models.send_chat_message(d.get("sender") or device_name(), d.get("text"))), 201


# ------------------------------------------------------------ 集計・ログ・エクスポート

@bp.get("/sales/summary")
def sales_summary():
    return jsonify(models.sales_summary())


@bp.get("/logs")
def logs():
    return jsonify(models.list_operation_log(limit=_int_arg("limit", 200, 1, 1000)))


def _csv_response(rows, header, filename):
    buf = io.StringIO()
    # UTF-8 の BOM を付ける。付けないと Windows の Excel が Shift_JIS と誤認して日本語が化ける
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@bp.get("/export/orders.csv")
def export_orders_csv():
    state = models.export_full_state()
    items_by_order: dict[int, list] = {}
    for it in state["order_items"]:
        items_by_order.setdefault(it["order_id"], []).append(it)
    rows = []
    for o in state["orders"]:
        for it in items_by_order.get(o["id"], [{}]):
            rows.append([
                o["id"], o["seat_no"], o["status"], o["created_at"], o["completed_at"],
                o["voided_at"], o["void_reason"], o["device_name"], o["total_amount"],
                it.get("menu_item_name", ""), it.get("qty", ""), it.get("unit_price", ""),
            ])
    return _csv_response(rows, ["order_id", "seat_no", "status", "created_at", "completed_at",
                                "voided_at", "void_reason", "device_name", "total_amount",
                                "menu_item_name", "qty", "unit_price"], "misty_orders.csv")


@bp.get("/export/summary.csv")
def export_summary_csv():
    s = models.sales_summary()
    rows = [[r["name"], r["qty"], r["amount"]] for r in s["ranking"]]
    rows += [[], ["order_count", s["order_count"]], ["total_amount", s["total_amount"]]]
    return _csv_response(rows, ["menu_item_name", "qty_sold", "amount"], "misty_summary.csv")


@bp.get("/export/log.csv")
def export_log_csv():
    rows = [[r["id"], r["ts"], r["device_name"], r["action"], r["order_id"], r["detail"]]
            for r in models.list_operation_log(limit=1_000_000)]
    return _csv_response(rows, ["id", "ts", "device_name", "action", "order_id", "detail"],
                         "misty_operation_log.csv")
