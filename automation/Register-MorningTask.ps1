<#
.SYNOPSIS
    注册"登录时触发"的计划任务，登录后运行 Start-MorningRoutine.ps1。
    脚本内部会判断当前是否在 7:00-10:00，不在窗口内会自动退出，
    所以即使非早上登录也不会执行那些动作。

.NOTES
    - 任务以当前用户身份、在交互式会话里运行（UI 自动化需要桌面）。
    - 默认 RunLevel Limited（普通权限）：普通用户即可注册、无需管理员，
      适合“没有管理员权限”的目标机。前提是那台机上密码能直接注入
      （凭据窗口为中完整性时可行），或已用 Save-RdpCmdKey.ps1 预存凭据。
    - 若当前账户是管理员、且想登录时自动提权（无 UAC）注入高完整性窗口，
      加 -Elevated 注册为 RunLevel Highest（此时注册这一步需要管理员）。
    - 想卸载：Unregister-ScheduledTask -TaskName 'MorningRoutine' -Confirm:$false
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'MorningRoutine',
    [string]$ScriptPath = "$PSScriptRoot\Start-MorningRoutine.ps1",
    [switch]$Elevated
)

# 优先用 pwsh(7)，没有就用 Windows PowerShell
$ps = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $ps) { $ps = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" }

$action = New-ScheduledTaskAction -Execute $ps `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$ScriptPath`""

# 登录时触发，再延迟 30 秒等桌面稳定
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$trigger.Delay = 'PT30S'

$runLevel = if ($Elevated) { 'Highest' } else { 'Limited' }
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel $runLevel

$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null

Write-Host "已注册计划任务 '$TaskName'（登录时触发，RunLevel=$runLevel）。" -ForegroundColor Green
Write-Host "解释器: $ps"
Write-Host "脚本  : $ScriptPath"
Write-Host ""
Write-Host "手动测试（忽略时间窗口）:"
Write-Host "  .\Start-MorningRoutine.ps1 -IgnoreTimeWindow"
