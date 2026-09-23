"""Misty POS - Google スプレッドシートからのメニュー取込

Google Sheets API（OAuth・サービスアカウント）ではなく、共有リンクの CSV エクスポートを使う。
メニューを編集する部員は毎年入れ替わるので、「シートを『リンクを知っている全員』で
共有して URL を貼るだけ」という手順より複雑にすると、翌年以降に誰も運用できなくなる。

  https://docs.google.com/spreadsheets/d/<ID>/export?format=csv&gid=<タブのgid>

落とし穴と対策:
- 共有設定が非公開だと、Google は 200 OK でログイン画面の HTML を返す。そのまま
  CSV としてパースすると「0件取込」で静かに成功してしまうので、Content-Type と
  本文先頭を見て HTML なら明示的にエラーにする。
- 会場の回線は不安定なので、接続エラーと 5xx は指数バックオフで3回まで再試行する。
  4xx（URL 間違い・権限なし）は再試行しても直らないので即座に返す。
- 価格欄に「¥1,200」「300円」のような表記が混ざっても数値として読む。
"""
from __future__ import annotations

import csv
import io
import re
import time

import requests

from .errors import UpstreamError, ValidationError

_NAME_KEYS = ("商品名", "name", "品名")
_CATEGORY_KEYS = ("カテゴリー", "カテゴリ", "category")
_PRICE_KEYS = ("価格", "price", "値段")
_STOCK_KEYS = ("在庫数", "在庫", "stock_qty", "stock")

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_GID_RE = re.compile(r"[?&#]gid=(\d+)")
_NUMERIC_RE = re.compile(r"[^\d.\-]")

MAX_BYTES = 2 * 1024 * 1024  # メニュー表としてあり得ない大きさは読まない
RETRY_STATUS = {429, 500, 502, 503, 504}


def normalize_sheet_url(url: str) -> str:
    """ブラウザのアドレスバーの URL をそのまま貼られても、CSV エクスポート URL に変換する。"""
    url = (url or "").strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        raise ValidationError("URL は http:// または https:// で始めてください")
    if "output=csv" in url or "format=csv" in url:
        return url
    m = _SHEET_ID_RE.search(url)
    if not m:
        return url
    gid_match = _GID_RE.search(url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid}"


def parse_number(value):
    """'¥1,200' → 1200, '300円' → 300。数値として読めなければ元の値を返し、検証は呼び出し側に任せる。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    cleaned = _NUMERIC_RE.sub("", str(value))
    if not cleaned:
        return value
    try:
        n = float(cleaned)
    except ValueError:
        return value
    return int(n) if n.is_integer() else n


def _pick(row, keys):
    for k in keys:
        v = row.get(k)
        if v not in (None, ""):
            return v
    return None


def parse_menu_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if reader.fieldnames is None:
        return []
    # 見出しの前後の空白・全角空白で列が認識されない事故を防ぐ
    reader.fieldnames = [(h or "").strip().replace("\u3000", "") for h in reader.fieldnames]
    if _pick({h: h for h in reader.fieldnames}, _NAME_KEYS) is None:
        raise ValidationError(
            f"見出し行に商品名の列が見つかりません（{' / '.join(_NAME_KEYS)} のいずれか）"
        )
    rows = []
    for raw in reader:
        name = _pick(raw, _NAME_KEYS)
        if not name or not str(name).strip():
            continue  # 空行・区切り行
        rows.append({
            "name": str(name).strip(),
            "category": str(_pick(raw, _CATEGORY_KEYS) or "").strip(),
            "price": parse_number(_pick(raw, _PRICE_KEYS)),
            "stock_qty": parse_number(_pick(raw, _STOCK_KEYS)) or 0,
        })
    return rows


def fetch_menu_rows(sheet_url, *, session=None, timeout=10, retries=3, backoff=0.5, sleep=time.sleep):
    csv_url = normalize_sheet_url(sheet_url)
    if not csv_url:
        raise ValidationError("スプレッドシートの URL が指定されていません")
    http = session or requests.Session()

    last_exc = None
    for attempt in range(retries):
        try:
            resp = http.get(csv_url, timeout=timeout, allow_redirects=True)
        except requests.RequestException as exc:
            last_exc = exc
        else:
            if resp.status_code in RETRY_STATUS:
                last_exc = UpstreamError(f"スプレッドシートの取得に失敗しました（HTTP {resp.status_code}）")
            elif resp.status_code >= 400:
                raise UpstreamError(
                    f"スプレッドシートを取得できません（HTTP {resp.status_code}）。URL と共有設定を確認してください"
                )
            else:
                return _decode(resp)
        if attempt < retries - 1:
            sleep(backoff * (2 ** attempt))
    raise UpstreamError(f"スプレッドシートに接続できませんでした: {last_exc}")


def _decode(resp) -> list[dict]:
    body = resp.content
    if len(body) > MAX_BYTES:
        raise UpstreamError("スプレッドシートが大きすぎます（2MB 超）")
    ctype = resp.headers.get("Content-Type", "")
    head = body[:512].lstrip().lower()
    if "text/html" in ctype or head.startswith((b"<!doctype html", b"<html")):
        raise UpstreamError(
            "CSV ではなく Web ページが返ってきました。シートの共有設定が"
            "「リンクを知っている全員（閲覧者）」になっているか確認してください",
            code="sheet_not_shared",
        )
    # Google の CSV エクスポートは UTF-8。apparent_encoding の推測は短い日本語で
    # 外れることがあるので使わず、UTF-8 で読めなければ Excel 由来の CP932 を試す
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp932", errors="replace")
    return parse_menu_csv(text)
