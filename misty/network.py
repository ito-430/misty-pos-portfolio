"""Misty POS - 昇格時のモバイルホットスポット自動起動（ベストエフォート）

スタンバイ機が昇格しても、レジやバーテンの端末が繋ぐ Wi-Fi が落ちたままでは意味がない。
ホストとスタンバイのホットスポットに同じ SSID/パスワードを設定しておき、昇格時に
スタンバイ側のホットスポットを上げれば、各端末は OS の自動再接続でスタンバイに乗り換える。
ルーターなどの追加機材を使わずに Wi-Fi 側も冗長化するための仕組み。

Windows 10/11 では WinRT の NetworkOperatorTetheringManager を PowerShell から叩く。
StartTetheringAsync() は IAsyncOperation を返すので、AsTask() で .NET の Task に
変換して完了を待ち、結果の Status が Success かどうかで成否を判定する。
（戻り値を捨てると、起動に失敗してもプロセスは正常終了し、成功と誤判定する）

自動化に失敗しても POS の運用は止めない。失敗は操作ログと管理画面に出し、
人が OS の設定からホットスポットを入れる運用で吸収する。
"""
from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger("misty.network")

_POWERSHELL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($op, [Type]$resultType) {
    $task = $asTask.MakeGenericMethod($resultType).Invoke($null, @($op))
    $null = $task.Wait(12000)
    $task.Result
}
$connProfile = [Windows.Networking.Connectivity.NetworkInformation,Windows.Networking.Connectivity,ContentType=WindowsRuntime]::GetInternetConnectionProfile()
if ($null -eq $connProfile) { Write-Output 'NoConnectionProfile'; exit 3 }
$mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager,Windows.Networking.NetworkOperators,ContentType=WindowsRuntime]::CreateFromConnectionProfile($connProfile)
if ($mgr.TetheringOperationalState -eq 'On') { Write-Output 'AlreadyOn'; exit 0 }
$result = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
Write-Output $result.Status
if ($result.Status -ne 'Success') { exit 2 }
"""


def try_start_hotspot(runner=subprocess.run) -> tuple[bool, str]:
    """(成功したか, 詳細)。例外は外に出さない。runner はテスト用の差し替え口。"""
    if platform.system() != "Windows":
        return False, "unsupported_os"
    try:
        proc = runner(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", _POWERSHELL_SCRIPT],
            timeout=20, capture_output=True, text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("hotspot start failed to launch: %s", exc)
        return False, f"launch_error: {exc}"
    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()[:300]
    ok = proc.returncode == 0
    if not ok:
        logger.warning("hotspot start failed (rc=%s): %s", proc.returncode, detail)
    return ok, detail
