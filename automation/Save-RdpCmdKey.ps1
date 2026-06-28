<#
.SYNOPSIS
    把远程 PC 的凭据预存到 Windows 凭据管理器（TERMSRV/<目标>），
    让 Windows App / RDP 连接时自动取用，从而【不再弹出密码窗口】。
    这样早上的例程就能以【普通（非管理员）】身份成功连接。

    【不需要管理员权限】：cmdkey 写的是“当前用户自己的”凭据保险箱。
    密码从纯文本文件 pw.txt 读取（取第一行）。

.PARAMETER Target
    远程主机，凭据窗口里显示的那个地址，默认 192.168.0.240。

.PARAMETER UserName
    远程登录用户名，默认 ou。

.EXAMPLE
    .\Save-RdpCmdKey.ps1
    .\Save-RdpCmdKey.ps1 -Target 192.168.0.240 -UserName ou

.NOTES
    查看已存：cmdkey /list:TERMSRV/192.168.0.240
    删除：    cmdkey /delete:TERMSRV/192.168.0.240
#>
[CmdletBinding()]
param(
    [string]$Target       = '192.168.0.240',
    [string]$UserName     = 'ou',
    [string]$PasswordFile = "$PSScriptRoot\pw.txt"
)

if (-not (Test-Path -LiteralPath $PasswordFile)) {
    Write-Error "找不到密码文件 $PasswordFile，请在该文件里写入密码（第一行）。"
    return
}

$pass = (Get-Content -LiteralPath $PasswordFile -TotalCount 1 -Encoding UTF8)
if ($null -ne $pass) { $pass = $pass.TrimEnd("`r", "`n") }
if ([string]::IsNullOrEmpty($pass)) {
    Write-Error "密码文件 $PasswordFile 为空。"
    return
}

# 写入凭据管理器（RDP 用 TERMSRV/<host> 作为目标名）
& cmdkey.exe "/generic:TERMSRV/$Target" "/user:$UserName" "/pass:$pass" | Out-Null
$pass = $null

if ($LASTEXITCODE -eq 0) {
    Write-Host "已写入凭据：TERMSRV/$Target  (user=$UserName)" -ForegroundColor Green
    Write-Host "现在用普通身份连一次试试："
    Write-Host "  .\Start-MorningRoutine.ps1 -IgnoreTimeWindow -SkipBrowser -SkipExcel"
    Write-Host ""
    Write-Host "（查看：cmdkey /list:TERMSRV/$Target  ；删除：cmdkey /delete:TERMSRV/$Target）"
} else {
    Write-Error "cmdkey 写入失败，退出码 $LASTEXITCODE"
}
