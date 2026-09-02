<#
.SYNOPSIS
    监视邮件文件夹，一旦出现新的 *.txt 文件，就自动执行 open_edge_urls.ps1。

.DESCRIPTION
    与 Start-MorningRoutine.ps1 的 7:00-10:00 时间窗口无关，本脚本任何时间都工作。

    - 启动时把文件夹里已有的 txt 当作基线，不会一上来就触发（想让已有文件也触发就加 -IncludeExisting）
    - 之后每 IntervalSeconds 秒扫描一次，发现新文件后等 DelaySeconds 秒（默认 5 秒，
      让文件写完）再执行脚本
    - 同一轮里出现多个新文件也只执行一次脚本
    - 文件被移走/删除后，同名文件再次出现仍会触发

.EXAMPLE
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\Watch-MailFolderAndOpenUrls.ps1

.EXAMPLE
    # 只跑 5 分钟、10 秒一轮，用来测试
    .\Watch-MailFolderAndOpenUrls.ps1 -IntervalSeconds 10 -TimeoutMinutes 5

.EXAMPLE
    # 登录时自动启动（普通权限即可，注册一次；不设执行时间上限，因为要常驻）
    $ps        = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $script    = "C:\work\work-git\git\auto\automation\WatchFilesAndRunScripts\Watch-MailFolderAndOpenUrls.ps1"
    $action    = New-ScheduledTaskAction -Execute $ps -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                     -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds 0)
    Register-ScheduledTask -TaskName 'WatchMailFolder' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force
#>
param(
    [string]$WatchFolder     = 'C:\Users\whz\Documents\MailTask\SapAutomation\input\mail',
    [string]$Filter          = '*.txt',
    [string]$ScriptPath      = 'C:\work\work-git\git\auto\automation\tools\open_edge_urls.ps1',
    [int]   $IntervalSeconds = 60,
    [int]   $DelaySeconds    = 5,
    [int]   $TimeoutMinutes  = 0,
    [switch]$IncludeExisting,
    [string]$LogPath         = "$PSScriptRoot\Watch-MailFolderAndOpenUrls.log"
)

$ErrorActionPreference = "Stop"
$MaxLogBytes = 10MB
$MaxLogHistory = 3

function Invoke-LogRotation {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return
    }

    $item = Get-Item -LiteralPath $Path
    if ($item.Length -lt $MaxLogBytes) {
        return
    }

    for ($index = $MaxLogHistory; $index -ge 1; $index--) {
        $rotatedPath = "$Path.$index"
        if (-not (Test-Path -LiteralPath $rotatedPath -PathType Leaf)) {
            continue
        }

        if ($index -eq $MaxLogHistory) {
            Remove-Item -LiteralPath $rotatedPath -Force
            continue
        }

        Move-Item -LiteralPath $rotatedPath -Destination "$Path.$($index + 1)" -Force
    }

    Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
}

function Write-Log {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet("INFO", "WARN", "ERROR")]
        [string]$Level = "INFO"
    )

    $logDirectory = Split-Path -Path $LogPath -Parent
    if (-not [string]::IsNullOrWhiteSpace($logDirectory) -and -not (Test-Path -LiteralPath $logDirectory -PathType Container)) {
        New-Item -Path $logDirectory -ItemType Directory -Force | Out-Null
    }

    Invoke-LogRotation -Path $LogPath

    $line = "{0} [{1}] {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Level, $Message
    Add-Content -LiteralPath $LogPath -Value $line -Encoding UTF8
    Write-Host $line
}

# 文件名 -> "最后写入时间|大小"，用来区分“新文件”和“还在写入的文件”
function Get-FolderSnapshot {
    param(
        [Parameter(Mandatory = $true)][string]$Folder,
        [Parameter(Mandatory = $true)][string]$Filter
    )

    $map = @{}
    if (-not (Test-Path -LiteralPath $Folder -PathType Container)) {
        return $null
    }

    foreach ($file in Get-ChildItem -LiteralPath $Folder -Filter $Filter -File -ErrorAction SilentlyContinue) {
        $map[$file.Name] = "{0}|{1}" -f $file.LastWriteTimeUtc.Ticks, $file.Length
    }

    return $map
}

function Invoke-TargetScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Log -Level "WARN" -Message "要执行的脚本不存在: $Path"
        return
    }

    Write-Log -Message "执行: $Path"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path
    Write-Log -Message "执行完成（退出码 $LASTEXITCODE）。"
}

if ($IntervalSeconds -lt 1) {
    throw "IntervalSeconds must be greater than 0."
}

Write-Log -Message "==== Watch-MailFolderAndOpenUrls 开始 ===="
Write-Log -Message "监视文件夹: $WatchFolder\$Filter（间隔 $IntervalSeconds 秒，发现新文件后等待 $DelaySeconds 秒）"
Write-Log -Message "触发时执行: $ScriptPath"

if (-not (Test-Path -LiteralPath $ScriptPath -PathType Leaf)) {
    Write-Log -Level "WARN" -Message "要执行的脚本当前不存在，等到触发时会再检查一次: $ScriptPath"
}

# 启动基线：默认把已有文件当作“已处理”，只有之后新出现的才触发
$seen = Get-FolderSnapshot -Folder $WatchFolder -Filter $Filter
if ($null -eq $seen) {
    Write-Log -Level "WARN" -Message "监视文件夹当前不存在，等它出现后再建立基线: $WatchFolder"
    $seen = @{}
}
elseif ($IncludeExisting) {
    Write-Log -Message "已指定 -IncludeExisting，启动时已有的 $($seen.Count) 个文件也会被当作新文件。"
    $seen = @{}
}
else {
    Write-Log -Message "启动时已有 $($seen.Count) 个文件，作为基线不触发。"
}

$deadline = $null
if ($TimeoutMinutes -gt 0) {
    $deadline = (Get-Date).AddMinutes($TimeoutMinutes)
    Write-Log -Message "监视将在 $TimeoutMinutes 分钟后自动结束。"
}
else {
    Write-Log -Message "按 Ctrl+C 结束监视。"
}

while ($true) {
    if ($null -ne $deadline -and (Get-Date) -gt $deadline) {
        Write-Log -Message "已达到设定时限，结束监视。"
        break
    }

    Start-Sleep -Seconds $IntervalSeconds

    try {
        $current = Get-FolderSnapshot -Folder $WatchFolder -Filter $Filter
        if ($null -eq $current) {
            Write-Log -Level "WARN" -Message "监视文件夹不存在: $WatchFolder"
            continue
        }

        # 已被移走/删除的文件从基线里清掉，同名文件再次出现时还能触发
        foreach ($name in @($seen.Keys)) {
            if (-not $current.ContainsKey($name)) { $seen.Remove($name) }
        }

        $newFiles = New-Object System.Collections.Generic.List[string]
        foreach ($name in @($current.Keys)) {
            if ($seen.ContainsKey($name)) { continue }
            $seen[$name] = $current[$name]
            $newFiles.Add($name)
        }

        if ($newFiles.Count -gt 0) {
            Write-Log -Message ("发现 {0} 个新文件：{1}" -f $newFiles.Count, ($newFiles -join ", "))
            if ($DelaySeconds -gt 0) {
                # 稍等一下，让文件写入完成再执行
                Write-Log -Message "等待 $DelaySeconds 秒后执行…"
                Start-Sleep -Seconds $DelaySeconds
            }
            Invoke-TargetScript -Path $ScriptPath
        }
    }
    catch {
        Write-Log -Level "WARN" -Message "监视循环出错: $($_.Exception.Message)"
    }
}

Write-Log -Message "==== Watch-MailFolderAndOpenUrls 结束 ===="
