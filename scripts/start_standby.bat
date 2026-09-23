@echo off
chcp 65001 > nul
REM このファイルは UTF-8。日本語版 Windows の cmd は既定で CP932 として読むので、
REM 日本語を含む行より前でコードページを切り替えておく（端末名の文字化け防止）
REM Misty POS - スタンバイ（レジ2）として起動する
REM このバッチファイルをダブルクリックしてください。
REM MISTY_PEER_URL はホスト機のホットスポット側アドレス。Windows の既定は
REM 192.168.137.1 だが、機種によって異なるので ipconfig で確認して合わせる
REM （docs/operations.md の「実機検証チェックリスト」参照）。

set MISTY_ROLE=standby
set MISTY_DEVICE_NAME=レジ2
set MISTY_PORT=5000
set MISTY_PEER_URL=http://192.168.137.1:5000

cd /d "%~dp0.."
if exist dist\MistyPOS\MistyPOS.exe (
    dist\MistyPOS\MistyPOS.exe
) else (
    python run.py
)
pause
