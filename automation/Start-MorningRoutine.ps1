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
    [string]$PasswordFile       = "$PSScriptRoot\pw.txt",
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
    param([string]$AppId, [string]$Tile, [string]$PwFile, [int]$AppTimeout, [int]$CredTimeout)

    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -AssemblyName System.Windows.Forms
    if (-not ('Win32Mouse' -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32Mouse {
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint f, uint dx, uint dy, uint d, IntPtr e);
    const uint DOWN = 0x0002, UP = 0x0004;
    public static void DoubleClick(int x, int y){
        SetCursorPos(x,y);
        mouse_event(DOWN,0,0,0,IntPtr.Zero); mouse_event(UP,0,0,0,IntPtr.Zero);
        System.Threading.Thread.Sleep(90);
        mouse_event(DOWN,0,0,0,IntPtr.Zero); mouse_event(UP,0,0,0,IntPtr.Zero);
    }
}
"@
    }

    if (-not ('NativeWin' -as [type])) {
    Add-Type @"
using System;
using System.Threading;
using System.Runtime.InteropServices;
public class NativeWin {
    [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, IntPtr pid);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll")] static extern bool AttachThreadInput(uint a, uint b, bool f);
    [DllImport("user32.dll")] static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] static extern bool ShowWindow(IntPtr h, int n);
    [DllImport("user32.dll", SetLastError=true)] static extern uint SendInput(uint n, INPUT[] p, int cb);
    [DllImport("user32.dll")] static extern short VkKeyScan(char ch);
    [DllImport("user32.dll")] static extern void keybd_event(byte bVk, byte bScan, uint dwFlags, UIntPtr dwExtraInfo);

    [StructLayout(LayoutKind.Sequential)] struct INPUT { public int type; public InputUnion u; }
    [StructLayout(LayoutKind.Explicit)] struct InputUnion {
        [FieldOffset(0)] public MOUSEINPUT mi;
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public HARDWAREINPUT hi;
    }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx; public int dy; public uint d; public uint f; public uint t; public IntPtr ex; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }
    [StructLayout(LayoutKind.Sequential)] struct HARDWAREINPUT { public uint msg; public ushort l; public ushort h; }

    const uint INPUT_KEYBOARD = 1, KEYUP = 0x0002, UNICODE = 0x0004;

    public static IntPtr Foreground(){ return GetForegroundWindow(); }
    public static void ForceForeground(IntPtr h){
        IntPtr fg = GetForegroundWindow();
        uint fgT = GetWindowThreadProcessId(fg, IntPtr.Zero);
        uint myT = GetCurrentThreadId();
        AttachThreadInput(myT, fgT, true);
        ShowWindow(h, 5); BringWindowToTop(h); SetForegroundWindow(h);
        AttachThreadInput(myT, fgT, false);
    }
    static void Send(ushort vk, ushort scan, uint flags){
        INPUT[] inp = new INPUT[1];
        inp[0].type = (int)INPUT_KEYBOARD;
        inp[0].u.ki.wVk = vk; inp[0].u.ki.wScan = scan; inp[0].u.ki.dwFlags = flags;
        SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
    }
    public static void TypeString(string s){
        foreach(char c in s){
            Send(0, (ushort)c, UNICODE);
            Send(0, (ushort)c, UNICODE | KEYUP);
            Thread.Sleep(15);
        }
    }
    // 逐键模拟真实键盘：算出虚拟键 + Shift/Ctrl/Alt 组合键，依次按下/抬起
    public static void TypeKeys(string s){
        const byte VK_SHIFT=0x10, VK_CONTROL=0x11, VK_MENU=0x12;
        foreach(char c in s){
            short vks = VkKeyScan(c);
            if (vks == -1){
                // 当前键盘布局打不出的字符，回退到 Unicode 注入
                Send(0, (ushort)c, UNICODE);
                Send(0, (ushort)c, UNICODE | KEYUP);
                Thread.Sleep(20);
                continue;
            }
            byte vk = (byte)(vks & 0xFF);
            int sh = (vks >> 8) & 0xFF;
            bool shift = (sh & 1)!=0, ctrl = (sh & 2)!=0, alt = (sh & 4)!=0;
            if (shift) keybd_event(VK_SHIFT,0,0,UIntPtr.Zero);
            if (ctrl)  keybd_event(VK_CONTROL,0,0,UIntPtr.Zero);
            if (alt)   keybd_event(VK_MENU,0,0,UIntPtr.Zero);
            keybd_event(vk,0,0,UIntPtr.Zero);
            keybd_event(vk,0,KEYUP,UIntPtr.Zero);
            if (alt)   keybd_event(VK_MENU,0,KEYUP,UIntPtr.Zero);
            if (ctrl)  keybd_event(VK_CONTROL,0,KEYUP,UIntPtr.Zero);
            if (shift) keybd_event(VK_SHIFT,0,KEYUP,UIntPtr.Zero);
            Thread.Sleep(20);
        }
    }
    public static void PressEnter(){
        Send(0x0D, 0, 0); Send(0x0D, 0, KEYUP);
    }
}
"@
    }

    $AE = [System.Windows.Automation.AutomationElement]
    $root = $AE::RootElement

    # --- 启动 Windows App ---
    Write-Log "启动 Windows App…"
    Start-Process "explorer.exe" "shell:AppsFolder\$AppId"

    # --- 等待主窗口出现 ---
    $appWin = $null
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $AppTimeout -and -not $appWin) {
        Start-Sleep -Seconds 2
        $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                 (New-Object System.Windows.Automation.PropertyCondition(
                    $AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)))
        foreach ($w in $wins) {
            if ($w.Current.Name -like '*Windows App*') { $appWin = $w; break }
        }
    }
    if (-not $appWin) { Write-Log "没等到 Windows App 主窗口。" 'WARN'; return }
    Write-Log "Windows App 窗口已就绪。"
    try { [void]$appWin.SetFocus() } catch {}
    Start-Sleep -Seconds 3

    # --- 找到 PC 磁贴 ---
    $tileEl = $null
    $sw.Restart()
    while ($sw.Elapsed.TotalSeconds -lt 30 -and -not $tileEl) {
        $all = $appWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, [System.Windows.Automation.Condition]::TrueCondition)
        foreach ($e in $all) {
            if ($e.Current.Name -like "*$Tile*") { $tileEl = $e; break }
        }
        if (-not $tileEl) { Start-Sleep -Seconds 2 }
    }
    if (-not $tileEl) { Write-Log "在 Windows App 里没找到磁贴 '$Tile'。" 'WARN'; return }
    Write-Log "找到磁贴 '$($tileEl.Current.Name)'，双击连接。"

    # --- 双击磁贴（优先 InvokePattern，失败则模拟鼠标双击）---
    $invoked = $false
    try {
        $ip = $tileEl.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
        $ip.Invoke(); $invoked = $true
    } catch {}
    if (-not $invoked) {
        try {
            $pt = $tileEl.GetClickablePoint()
        } catch {
            $r = $tileEl.Current.BoundingRectangle
            $pt = New-Object System.Windows.Point(($r.X + $r.Width/2), ($r.Y + $r.Height/2))
        }
        [Win32Mouse]::DoubleClick([int]$pt.X, [int]$pt.Y)
    }

    # --- 等待凭据窗口（约 2 分钟后才弹）---
    Write-Log "等待凭据窗口弹出（最长 $CredTimeout 秒）…"
    $credWin = $null
    $sw.Restart()
    while ($sw.Elapsed.TotalSeconds -lt $CredTimeout -and -not $credWin) {
        Start-Sleep -Seconds 3
        $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children,
                 (New-Object System.Windows.Automation.PropertyCondition(
                    $AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Window)))
        foreach ($w in $wins) {
            $cls = ''
            try { $cls = $w.Current.ClassName } catch {}
            if ($cls -eq 'Credential Dialog Xaml Host' -or $w.Current.Name -like '*Windows*Security*' -or $w.Current.Name -like '*Windows*セキュリティ*') {
                $credWin = $w; break
            }
        }
    }
    if (-not $credWin) {
        Write-Log "没有出现凭据窗口（可能已记住密码/已连接），结束远程连接步骤。" 'WARN'; return
    }
    $credClass = ''
    try { $credClass = $credWin.Current.ClassName } catch {}
    Write-Log ("凭据窗口已出现：Name='{0}' Class='{1}'" -f $credWin.Current.Name, $credClass)

    # --- SendKeys 转义函数 ---
    function ConvertTo-SendKeys([string]$s) {
        ($s.ToCharArray() | ForEach-Object {
            if ('+^%~(){}[]'.Contains($_)) { "{$_}" } else { [string]$_ }
        }) -join ''
    }

    # --- 从 pw.txt 读取密码（取第一行，去掉行尾换行）---
    if (-not (Test-Path -LiteralPath $PwFile)) {
        Write-Log "找不到密码文件 $PwFile，请在该文件里写入密码。" 'ERROR'; return
    }
    $plain = (Get-Content -LiteralPath $PwFile -TotalCount 1 -Encoding UTF8)
    if ($null -eq $plain) { $plain = '' }
    $plain = $plain.TrimEnd("`r", "`n")
    if ([string]::IsNullOrEmpty($plain)) {
        Write-Log "密码文件 $PwFile 为空。" 'ERROR'; return
    }

    # # --- 提醒：向凭据窗口注入需要管理员（UIPI）；非管理员通常会失败 ---
    # $amAdmin = $false
    # try { $amAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) } catch {}
    # if (-not $amAdmin) {
    #     Write-Log "警告：当前非管理员，向凭据窗口注入密码很可能被 UIPI 拦截而失败。建议用提权窗口运行，或让计划任务以最高权限运行。" 'WARN'
    # }

    # # --- 强制把凭据窗口拉到前台（突破前台锁定）---
    # $h = [IntPtr]::Zero
    # try { $h = [IntPtr]$credWin.Current.NativeWindowHandle } catch {}
    # if ($h -ne [IntPtr]::Zero) {
    #     [NativeWin]::ForceForeground($h)
    #     $fgNow = [NativeWin]::Foreground()
    #     Write-Log "已把凭据窗口拉到前台 (目标 hwnd=$h，当前前台 hwnd=$fgNow，匹配=$([IntPtr]$fgNow -eq $h))。"
    # } else {
    #     Write-Log "拿不到凭据窗口句柄，回退用 SetFocus。" 'WARN'
    #     try { [void]$credWin.SetFocus() } catch {}
    # }
    # Start-Sleep -Milliseconds 2000

    # # --- 尽量把焦点放到密码框（失败也无妨，弹窗默认就在密码框）---
    # try {
    #     $editCond = New-Object System.Windows.Automation.PropertyCondition(
    #         $AE::ControlTypeProperty, [System.Windows.Automation.ControlType]::Edit)
    #     $edits = $credWin.FindAll([System.Windows.Automation.TreeScope]::Descendants, $editCond)
    #     Write-Log "凭据窗口里枚举到 $($edits.Count) 个 Edit 控件。"
    #     $pwdField = $null
    #     foreach ($e in $edits) {
    #         $isPwd = $false
    #         try { $isPwd = [bool]$e.GetCurrentPropertyValue($AE::IsPasswordProperty) } catch {}
    #         if ($isPwd) { $pwdField = $e; break }
    #     }
    #     if ($pwdField) { [void]$pwdField.SetFocus(); Write-Log "已聚焦密码框。" }
    #     else { Write-Log "未定位到密码框，使用窗口默认焦点。" }
    # } catch { Write-Log "聚焦密码框时出错: $($_.Exception.Message)" 'WARN' }
    Start-Sleep -Milliseconds 300

    # --- 用 SendInput(Unicode) 逐字符输入密码，再回车提交 ---
    Write-Log "输入密码 (SendInput Unicode) 并回车…"
    [NativeWin]::TypeString($plain)
    Start-Sleep -Milliseconds 1000
    [NativeWin]::PressEnter()
    $plain = $null
    Write-Log "远程连接步骤完成。"
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
