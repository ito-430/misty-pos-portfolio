@echo off
chcp 65001 > nul
REM このファイルは UTF-8。日本語版 Windows の cmd は既定で CP932 として読むので、
REM 日本語を含む行より前でコードページを切り替えておく（端末名の文字化け防止）
REM Misty POS - ホスト（レジ1）として起動する
REM このバッチファイルをダブルクリックしてください。

set MISTY_ROLE=host
set MISTY_DEVICE_NAME=レジ1
set MISTY_PORT=5000

cd /d "%~dp0.."
if exist dist\MistyPOS\MistyPOS.exe (
    dist\MistyPOS\MistyPOS.exe
) else (
    python run.py
)
pause
