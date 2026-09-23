"""Misty POS - 接続用 QR コード

画像ファイルを書き出さず data URI で HTML に埋め込む。PyInstaller で固めた exe では
書き込み可能な static ディレクトリがあるとは限らず、一時ファイルの掃除も要らなくなる。
"""
import base64
import io

import qrcode


def qr_data_uri(text: str) -> str:
    img = qrcode.make(text, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
