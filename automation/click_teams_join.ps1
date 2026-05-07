# Bring the Teams launcher Edge window to front and send Tab + Enter
# to trigger the "このブラウザーで続ける" button (first focusable element).

[CmdletBinding()]
param(
    [string]$WindowTitleContains = '会話に参加',
    [int]$TimeoutSeconds = 30,
    [int]$DelayBeforeKeysMs = 1500
)

Add-Type -AssemblyName System.Windows.Forms
if (-not ([System.Management.Automation.PSTypeName]'Win32').Type) {
    Add-Type @'
using System;
using System.Runtime.InteropServices;
public static class Win32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool ShowWindowAsync(IntPtr hWnd, int nCmdShow);
}
'@
}

$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$proc = $null
while ((Get-Date) -lt $deadline -and -not $proc) {
    $proc = Get-Process msedge -ErrorAction SilentlyContinue |
        Where-Object {
            $_.MainWindowHandle -ne 0 -and
            $_.MainWindowTitle -like "*$WindowTitleContains*"
        } | Select-Object -First 1
    if (-not $proc) { Start-Sleep -Milliseconds 500 }
}

if (-not $proc) {
    Write-Error "Edge window with title containing '$WindowTitleContains' not found within $TimeoutSeconds s."
    exit 1
}

[Win32]::ShowWindowAsync($proc.MainWindowHandle, 9) | Out-Null   # SW_RESTORE
[Win32]::SetForegroundWindow($proc.MainWindowHandle) | Out-Null

Start-Sleep -Milliseconds $DelayBeforeKeysMs   # let focus settle / page finish rendering

[System.Windows.Forms.SendKeys]::SendWait('{TAB}')
Start-Sleep -Milliseconds 200
[System.Windows.Forms.SendKeys]::SendWait('{ENTER}')

Write-Host "Sent Tab + Enter to: $($proc.MainWindowTitle)"
