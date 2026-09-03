<#
    启动 Windows App，连接指定 PC，等凭据窗口弹出后自动输入密码并确定。
    密码 = 同目录 pw.txt 第一行。
    必须提权运行：凭据对话框受 UIPI 保护，普通进程的 SendInput 会返回成功但被静默丢弃。
    脚本会自己请求提权；用计划任务以"最高权限"运行时不会有 UAC 提示。
#>

# ------------------------------ 配置 ------------------------------
$AppId        = 'MicrosoftCorporationII.Windows365_8wekyb3d8bbwe!Windows365'
$PcName       = 'mini-pc'
$PasswordFile = Join-Path $PSScriptRoot 'pw.txt'
$LogPath      = Join-Path $PSScriptRoot 'AutoOpenWindowsApp.log'
$AppTimeout   = 60    # 等 Windows App 主窗口
$TileTimeout  = 30    # 等 PC 磁贴
$CredTimeout  = 240   # 等凭据窗口

$ErrorActionPreference = 'Stop'

function Log($m, $lv = 'INFO') {
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'HH:mm:ss'), $lv, $m
    Write-Host $line
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch {}
}

# 凭据对话框受 UIPI 保护，非提权进程注入的按键会被丢弃 -> 先把自己提权重启
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Log "当前非管理员，重新以管理员身份启动…"
    try { Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"" }
    catch { Log "提权被拒绝，无法自动输入密码。" 'ERROR' }
    exit
}

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type @"
using System; using System.Threading; using System.Runtime.InteropServices;
public class W {
    [DllImport("user32.dll")] static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
    [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] p, int cb);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr p);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool f);
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int n);

    [StructLayout(LayoutKind.Sequential)] struct INPUT { public int type; public U u; }
    [StructLayout(LayoutKind.Explicit)] struct U {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public HARDWAREINPUT hi;
    }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint d, f, t; public IntPtr ex; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr ex; }
    [StructLayout(LayoutKind.Sequential)] struct HARDWAREINPUT { public uint msg; public ushort l, h; }
    const uint KEYBOARD = 1, KEYUP = 0x0002, UNICODE = 0x0004;

    static void Key(ushort vk, ushort scan, uint flags) {
        INPUT[] i = new INPUT[1];
        i[0].type = (int)KEYBOARD;
        i[0].u.ki.wVk = vk; i[0].u.ki.wScan = scan; i[0].u.ki.dwFlags = flags;
        SendInput(1, i, Marshal.SizeOf(typeof(INPUT)));
    }
    // 把窗口强行拉到前台：借用当前前台线程的输入队列，绕过前台锁定
    public static void Foreground(IntPtr h) {
        uint fg = GetWindowThreadProcessId(GetForegroundWindow(), IntPtr.Zero), me = GetCurrentThreadId();
        AttachThreadInput(me, fg, true);
        ShowWindow(h, 5); BringWindowToTop(h); SetForegroundWindow(h);
        AttachThreadInput(me, fg, false);
    }
    public static void Click(int x, int y) {
        SetCursorPos(x, y); Thread.Sleep(120);
        mouse_event(0x0002, 0, 0, 0, IntPtr.Zero); mouse_event(0x0004, 0, 0, 0, IntPtr.Zero);
    }
    public static void DoubleClick(int x, int y) {
        Click(x, y); Thread.Sleep(90);
        mouse_event(0x0002, 0, 0, 0, IntPtr.Zero); mouse_event(0x0004, 0, 0, 0, IntPtr.Zero);
    }
    public static void Type(string s) {
        foreach (char c in s) { Key(0, (ushort)c, UNICODE); Key(0, (ushort)c, UNICODE | KEYUP); Thread.Sleep(25); }
    }
    public static void Enter() { Key(0x0D, 0, 0); Key(0x0D, 0, KEYUP); }
    public static void Back(int n) { for (int i = 0; i < n; i++) { Key(0x08, 0, 0); Key(0x08, 0, KEYUP); } }
}
"@

$AE   = [System.Windows.Automation.AutomationElement]
$Tree = [System.Windows.Automation.TreeScope]
$root = $AE::RootElement

# 在超时内轮询查找满足 $Match 的顶层窗口
function Find-Window([scriptblock]$Match, [int]$Timeout) {
    $cond = New-Object System.Windows.Automation.PropertyCondition(
        $AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Timeout) {
        foreach ($w in $root.FindAll($Tree::Children, $cond)) {
            if (& $Match $w) { return $w }
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

# 元素的可点击坐标（拿不到就用外框中心）
function Get-Point($el) {
    try { return $el.GetClickablePoint() } catch {
        $r = $el.Current.BoundingRectangle
        return New-Object System.Windows.Point(($r.X + $r.Width / 2), ($r.Y + $r.Height / 2))
    }
}

# ------------------------------ 主流程 ------------------------------
Log "==== 开始 ===="
try {
    # 先读密码，免得白等
    $pw = Get-Content -LiteralPath $PasswordFile -TotalCount 1 -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($pw)) { throw "密码文件为空: $PasswordFile" }
    $pw = $pw.Trim()

    Log "启动 Windows App…"
    Start-Process 'explorer.exe' "shell:AppsFolder\$AppId"

    $app = Find-Window { param($w) $w.Current.Name -like '*Windows App*' } $AppTimeout
    if (-not $app) { throw "没等到 Windows App 主窗口" }
    Log "Windows App 已就绪"
    [W]::Foreground([IntPtr]$app.Current.NativeWindowHandle)
    Start-Sleep -Seconds 3

    # 找 PC 磁贴
    $tile = $null
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TileTimeout -and -not $tile) {
        foreach ($e in $app.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
            if ($e.Current.Name -like "*$PcName*") { $tile = $e; break }
        }
        if (-not $tile) { Start-Sleep -Seconds 2 }
    }
    if (-not $tile) { throw "没找到磁贴 '$PcName'" }
    Log "找到磁贴 '$($tile.Current.Name)'，连接中…"

    try { $tile.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
    catch { $p = Get-Point $tile; [W]::DoubleClick([int]$p.X, [int]$p.Y) }

    # 等凭据窗口
    $cred = Find-Window {
        param($w)
        $c = ''; try { $c = $w.Current.ClassName } catch {}
        $c -eq 'Credential Dialog Xaml Host' -or $w.Current.Name -match 'Windows (Security|セキュリティ)'
    } $CredTimeout
    if (-not $cred) { throw "没有出现凭据窗口（可能已记住密码/已连接）" }

    $hCred = [IntPtr]$cred.Current.NativeWindowHandle
    Log ("凭据窗口: Name='{0}' hwnd={1}" -f $cred.Current.Name, $hCred)

    # 关键：先把凭据窗口强行拉到前台，否则按键会发给当时的前台窗口
    [W]::Foreground($hCred)
    Start-Sleep -Milliseconds 600
    $fg = [W]::GetForegroundWindow()
    Log ("前台 hwnd={0} 匹配={1}" -f $fg, ($fg -eq $hCred))

    # CredentialUIBroker 的内部控件对普通进程不可见（UIA 子树为空，定位不到密码框），
    # 但窗口弹出时焦点默认就在密码框上，SendInput 可以直接打进去。
    [W]::Back(40)   # 清掉可能的残留字符
    Log "输入密码并回车…"
    [W]::Type($pw)
    $pw = $null
    Start-Sleep -Milliseconds 500
    [W]::Enter()

    # 校验：凭据窗口消失即视为提交成功（连接协商中可能要十几秒才关）
    $sw.Restart(); $gone = $false
    while ($sw.Elapsed.TotalSeconds -lt 40 -and -not $gone) {
        Start-Sleep -Seconds 1
        try { $null = $cred.Current.Name } catch { $gone = $true }
    }
    if ($gone) { Log "凭据窗口已关闭，密码提交成功。" }
    else { Log "凭据窗口仍在，密码可能未被接受。" 'WARN' }
}
catch { Log $_.Exception.Message 'ERROR' }
Log "==== 结束 ===="
