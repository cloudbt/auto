<#
.SYNOPSIS
    早上开机例程：在 7:00-10:00 这个时间段内登录时，自动
      1. 打开 Edge 收藏夹分组 "test" 里的所有链接
      2. 打开 Excel: output\report.xlsx
      3. 启动 Windows App，连接里面唯一的一台 PC，
         等待约 2 分钟后弹出的凭据窗口，自动输入密码并确定。

    本脚本通常由"登录时触发"的计划任务调用（见 Register-MorningTask.ps1），
    脚本内部自己判断当前时间是否在 7-10 点窗口内。

.NOTES
    密码从纯文本文件 pw.txt 读取（取第一行）。
    注意：向那个凭据窗口注入密码受 Windows UIPI 限制，【需要管理员/提权】才能成功，
    这与密码存放方式无关。日常用计划任务以"最高权限"在登录时自动提权运行即可（无 UAC 弹窗）。
#>
[CmdletBinding()]
param(
    [int]   $StartHour          = 7,
    [int]   $EndHour            = 10,
    [string]$ExcelPath          = 'C:\work\work-git\git\auto\automation\output\report.xlsx',
    [string]$EdgeProfile        = 'Default',
    [string]$BookmarkFolder     = 'test',
    [string]$WindowsAppId       = 'MicrosoftCorporationII.Windows365_8wekyb3d8bbwe!Windows365',
    [string]$PcName             = 'mini-pc',
    [string]$PasswordFile       = "C:\work\work-git\git\auto\automation\StartMorningRoutine\pw.txt",
    [int]   $StartupDelaySeconds= 15,
    [int]   $WaitForAppSeconds  = 60,
    [int]   $WaitForCredSeconds = 240,
    [switch]$IgnoreTimeWindow,
    [switch]$SkipBrowser,
    [switch]$SkipExcel,
    [switch]$SkipRemotePc,
    [string]$LogPath            = "$PSScriptRoot\morning-routine.log"
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- 日志
function Write-Log {
    param([string]$Message, [string]$Level = 'INFO')
    $line = "{0} [{1}] {2}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Message
    Write-Host $line
    try { Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8 } catch {}
}

Write-Log "==== Start-MorningRoutine 开始 ===="
try {
    $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-Log ("运行身份：{0}  管理员={1}  PS版本={2}" -f $env:USERNAME, $isAdmin, $PSVersionTable.PSVersion)
} catch {}

# ---------------------------------------------------------------- 邮件文件夹监视
# 后台启动：邮件文件夹出现新 txt -> 自动执行 open_edge_urls.ps1（不受 7-10 窗口限制）
# try {
#     Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','C:\work\work-git\git\auto\automation\WatchFilesAndRunScripts\Watch-MailFolderAndOpenUrls.ps1'
#     Write-Log "已后台启动邮件文件夹监视 Watch-MailFolderAndOpenUrls.ps1。"
# }
# catch { Write-Log "启动邮件文件夹监视失败: $($_.Exception.Message)" 'ERROR' }

# ---------------------------------------------------------------- 时间窗口判断
if (-not $IgnoreTimeWindow) {
    $now   = Get-Date
    $start = (Get-Date).Date.AddHours($StartHour)
    $end   = (Get-Date).Date.AddHours($EndHour)
    if ($now -lt $start -or $now -gt $end) {
        Write-Log ("当前时间 {0} 不在 {1:HH:mm}-{2:HH:mm} 窗口内，退出。" -f $now.ToString('HH:mm'), $start, $end)
        return
    }
    Write-Log ("时间窗口检查通过：{0}" -f $now.ToString('HH:mm'))
}
else {
    Write-Log "已指定 -IgnoreTimeWindow，跳过时间检查。"
}

if ($StartupDelaySeconds -gt 0) {
    Write-Log "等待 $StartupDelaySeconds 秒，让桌面环境就绪…"
    Start-Sleep -Seconds $StartupDelaySeconds
}

# ================================================================
# 1) 打开 Edge 收藏夹分组 "test"
# ================================================================
function Get-BookmarkFolderUrls {
    param([string]$ProfileName, [string]$FolderName)
    $bmPath = "$env:LOCALAPPDATA\Microsoft\Edge\User Data\$ProfileName\Bookmarks"
    if (-not (Test-Path -LiteralPath $bmPath)) {
        Write-Log "找不到 Edge 书签文件: $bmPath" 'WARN'; return @()
    }
    $bm = Get-Content -LiteralPath $bmPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $urls = New-Object System.Collections.Generic.List[string]
    $script:found = $false
    function Walk($node) {
        foreach ($c in $node.children) {
            if ($c.type -eq 'folder') {
                if ($c.name -eq $FolderName) {
                    $script:found = $true
                    foreach ($u in $c.children) {
                        if ($u.type -eq 'url' -and $u.url) { $urls.Add($u.url) }
                    }
                }
                Walk $c
            }
        }
    }
    if ($bm.roots.bookmark_bar) { Walk $bm.roots.bookmark_bar }
    if ($bm.roots.other)        { Walk $bm.roots.other }
    if ($bm.roots.synced)       { Walk $bm.roots.synced }
    if (-not $script:found) { Write-Log "在书签里没找到名为 '$FolderName' 的文件夹。" 'WARN' }
    return $urls
}

if ($SkipBrowser) { Write-Log "已指定 -SkipBrowser，跳过浏览器。" }
try {
    if ($SkipBrowser) { $urls = @() } else { $urls = Get-BookmarkFolderUrls -ProfileName $EdgeProfile -FolderName $BookmarkFolder }
    if ($urls.Count -gt 0) {
        Write-Log ("打开 Edge 分组 '{0}' 的 {1} 个链接…" -f $BookmarkFolder, $urls.Count)
        $edge = "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe"
        if (-not (Test-Path $edge)) { $edge = "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe" }
        $edgeArgs = @("--profile-directory=$EdgeProfile") + $urls
        Start-Process -FilePath $edge -ArgumentList $edgeArgs
    }
}
catch { Write-Log "打开浏览器分组失败: $($_.Exception.Message)" 'ERROR' }

# ================================================================
# 2) 打开 Excel report.xlsx
# ================================================================
if ($SkipExcel) { Write-Log "已指定 -SkipExcel，跳过 Excel。" }
else {
    try {
        if (Test-Path -LiteralPath $ExcelPath) {
            Write-Log "打开 Excel: $ExcelPath"
            Invoke-Item -LiteralPath $ExcelPath
        }
        else { Write-Log "Excel 文件不存在: $ExcelPath" 'WARN' }
    }
    catch { Write-Log "打开 Excel 失败: $($_.Exception.Message)" 'ERROR' }
}

# ================================================================
# 3) Windows App -> 连接 PC -> 输入密码
# ================================================================
function Connect-RemotePc {
    param([string]$AppId, [string]$Tile, [string]$PwFile, [int]$AppTimeout, [int]$CredTimeout, [int]$TileTimeout = 30)

    # 凭据对话框受 UIPI 保护：非提权进程的 SendInput 会返回成功、但按键被静默丢弃。
    if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
            [Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Log "当前非管理员，凭据窗口的按键会被 UIPI 丢弃，密码无法自动输入。请把计划任务注册为最高权限（Register-MorningTask.ps1 -Elevated）。" 'WARN'
    }

    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    if (-not ('W' -as [type])) {
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
    [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);

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
    }

    $AE   = [System.Windows.Automation.AutomationElement]
    $Tree = [System.Windows.Automation.TreeScope]
    $root = $AE::RootElement

    # 在超时内轮询查找满足 $Match 的顶层窗口
    function Find-TopWindow([scriptblock]$Match, [int]$Timeout) {
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

    # 先读密码，免得白等
    if (-not (Test-Path -LiteralPath $PwFile)) { throw "找不到密码文件 $PwFile" }
    $pw = Get-Content -LiteralPath $PwFile -TotalCount 1 -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($pw)) { throw "密码文件为空: $PwFile" }
    $pw = $pw.Trim()

    Write-Log "启动 Windows App…"
    Start-Process 'explorer.exe' "shell:AppsFolder\$AppId"

    $app = Find-TopWindow { param($w) $w.Current.Name -like '*Windows App*' } $AppTimeout
    if (-not $app) { throw "没等到 Windows App 主窗口" }
    Write-Log "Windows App 已就绪"
    [W]::Foreground([IntPtr]$app.Current.NativeWindowHandle)
    Start-Sleep -Seconds 3

    # 找 PC 磁贴
    $tileEl = $null
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $TileTimeout -and -not $tileEl) {
        foreach ($e in $app.FindAll($Tree::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
            if ($e.Current.Name -like "*$Tile*") { $tileEl = $e; break }
        }
        if (-not $tileEl) { Start-Sleep -Seconds 2 }
    }
    if (-not $tileEl) { throw "没找到磁贴 '$Tile'" }
    Write-Log "找到磁贴 '$($tileEl.Current.Name)'，连接中…"

    try { $tileEl.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
    catch { $p = Get-Point $tileEl; [W]::DoubleClick([int]$p.X, [int]$p.Y) }

    # 等凭据窗口
    $cred = Find-TopWindow {
        param($w)
        $c = ''; try { $c = $w.Current.ClassName } catch {}
        $c -eq 'Credential Dialog Xaml Host' -or $w.Current.Name -match 'Windows (Security|セキュリティ)'
    } $CredTimeout
    if (-not $cred) { throw "没有出现凭据窗口（可能已记住密码/已连接）" }

    $hCred = [IntPtr]$cred.Current.NativeWindowHandle
    Write-Log ("凭据窗口: Name='{0}' hwnd={1}" -f $cred.Current.Name, $hCred)

    # 关键：先把凭据窗口强行拉到前台，否则按键会发给当时的前台窗口
    [W]::Foreground($hCred)
    Start-Sleep -Milliseconds 600
    $fg = [W]::GetForegroundWindow()
    Write-Log ("前台 hwnd={0} 匹配={1}" -f $fg, ($fg -eq $hCred))

    # CredentialUIBroker 的内部控件对外不可见（UIA 子树为空，定位不到密码框），
    # 但窗口弹出时焦点默认就在密码框上，SendInput 可以直接打进去。
    [W]::Back(40)   # 清掉可能的残留字符
    Write-Log "输入密码并回车…"
    [W]::Type($pw)
    $pw = $null
    Start-Sleep -Milliseconds 500
    [W]::Enter()

    # 校验：用 IsWindow 判断凭据窗口是否已销毁
    #（不能用 $cred.Current.Name 抛不抛异常来判断：窗口销毁后它返回空串而非抛错）
    $sw.Restart()
    while ($sw.Elapsed.TotalSeconds -lt 20 -and [W]::IsWindow($hCred)) { Start-Sleep -Seconds 1 }
    if (-not [W]::IsWindow($hCred)) { Write-Log "凭据窗口已关闭，密码提交成功。" }
    else { Write-Log "凭据窗口仍在，密码可能未被接受。" 'WARN' }
}

if (-not $SkipRemotePc) {
    try {
        Connect-RemotePc -AppId $WindowsAppId -Tile $PcName -PwFile $PasswordFile `
                         -AppTimeout $WaitForAppSeconds -CredTimeout $WaitForCredSeconds
    }
    catch { Write-Log "远程连接出错: $($_.Exception.Message)" 'ERROR' }
}
else { Write-Log "已指定 -SkipRemotePc，跳过远程连接。" }

Write-Log "==== Start-MorningRoutine 结束 ===="
