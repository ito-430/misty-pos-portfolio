#!/usr/bin/env python3
"""Misty POS の起動スクリプト。

設定は環境変数で与える（一覧は misty/config.py の Settings）。

    # スタンバイ機として起動する例（Windows）
    set MISTY_ROLE=standby
    set MISTY_DEVICE_NAME=レジ2
    set MISTY_PEER_URL=http://192.168.137.1:5000
    python run.py
"""
from misty.app import main

if __name__ == "__main__":
    main()
