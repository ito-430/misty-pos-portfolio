"""Misty POS - ドメイン例外

HTTP ステータスとエラーコードを例外側に持たせ、API 層では1つのエラーハンドラで
JSON に変換する。ルートごとに try/except を書くと、どこかで捕まえ忘れた例外が
Flask の HTML 500 ページになり、フロントの JSON パースごと壊れるため。
"""


class MistyError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code

    def to_dict(self):
        return {"error": self.code, "message": self.message}


class ValidationError(MistyError):
    status = 400
    code = "validation_error"


class NotFound(MistyError):
    status = 404
    code = "not_found"


class Conflict(MistyError):
    """状態遷移の競合（他の端末が先に操作した、整理番号が埋まっている等）。"""
    status = 409
    code = "conflict"


class NotActive(MistyError):
    """スタンバイ中、またはフェンスされたノードへの書き込み。"""
    status = 409
    code = "standby"


class Unauthorized(MistyError):
    status = 401
    code = "unauthorized"


class UpstreamError(MistyError):
    """スプレッドシート取得など外部依存の失敗。"""
    status = 502
    code = "upstream_error"
