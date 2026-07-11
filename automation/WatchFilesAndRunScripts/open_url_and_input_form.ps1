$url = "https://www.library.city.kita.lg.jp/opw/OPW/OPWUSERCONF.CSP"
Start-Process "msedge.exe" $url
$Username = "xxx"
$Password = "xxx"
$ClickX = 85
$ClickY = 106

if (-not ('Win32Mouse' -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;

public class Win32Mouse {
    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, UIntPtr dwExtraInfo);

    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    private const uint MOUSEEVENTF_LEFTUP = 0x0004;

    public static void LeftClick(int x, int y) {
        SetCursorPos(x, y);
        mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, UIntPtr.Zero);
        mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, UIntPtr.Zero);
    }
}
"@
}

function Invoke-MouseClick {
    param(
        [Parameter(Mandatory = $true)][int]$X,
        [Parameter(Mandatory = $true)][int]$Y
    )

    [Win32Mouse]::LeftClick($X, $Y)
}

$wshell = New-Object -ComObject wscript.shell
# Start-Sleep -Seconds 2
# $wshell.SendKeys($Username)
# Start-Sleep -Seconds 1
# $wshell.SendKeys("{TAB}")
# Start-Sleep -Seconds 1
# $wshell.SendKeys($Password)
Start-Sleep -Seconds 1
$wshell.SendKeys("{ENTER}")

# # Windows11 Ctrl + Vでクリップボードの内容を貼り付ける
# $wshell.SendKeys("^(v)")

# # Windows11 Ctrl + Sで保存する
# $wshell.SendKeys("^(s)")
# # Windows11 Ctrl + Aで選択する
# $wshell.SendKeys("^(a)")

# 鼠标点击指定的屏幕坐标位置
Invoke-MouseClick -X $ClickX -Y $ClickY

# $TodayText = Get-Date -Format "MMdd"
# $ClipboardText = "Init$TodayText#Osei"
# Set-Clipboard -Value $ClipboardText
