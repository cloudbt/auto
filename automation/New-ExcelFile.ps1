<#
.SYNOPSIS
    在指定文件夹里生成一个指定名字的空 Excel 文件，并打开它。

.PARAMETER Folder
    目标文件夹路径。不存在时会自动创建。

.PARAMETER Name
    Excel 文件名。可以带或不带 .xlsx 扩展名。

.EXAMPLE
    .\New-ExcelFile.ps1 -Folder "C:\Temp" -Name "报表"

.EXAMPLE
    .\New-ExcelFile.ps1 "C:\Temp" "report.xlsx"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Folder,

    [Parameter(Mandatory = $true, Position = 1)]
    [string]$Name
)

$ErrorActionPreference = 'Stop'

# 1. 确保文件夹存在
if (-not (Test-Path -LiteralPath $Folder)) {
    New-Item -ItemType Directory -Path $Folder -Force | Out-Null
    Write-Host "已创建文件夹: $Folder"
}

# 2. 补全扩展名并拼出完整路径
if ([System.IO.Path]::GetExtension($Name) -ne '.xlsx') {
    $Name = "$Name.xlsx"
}
$fullPath = Join-Path -Path (Resolve-Path -LiteralPath $Folder) -ChildPath $Name

if (Test-Path -LiteralPath $fullPath) {
    Write-Warning "文件已存在，将直接打开: $fullPath"
}
else {
    # 3. 用 Excel COM 创建一个真正的空 .xlsx 文件
    $excel = New-Object -ComObject Excel.Application
    $excel.Visible = $false
    $excel.DisplayAlerts = $false
    try {
        $workbook = $excel.Workbooks.Add()
        # xlOpenXMLWorkbook = 51 (.xlsx 格式)
        $workbook.SaveAs($fullPath, 51)
        $workbook.Close($false)
        Write-Host "已生成空 Excel 文件: $fullPath"
    }
    finally {
        $excel.Quit()
        # 释放 COM 对象，避免残留 EXCEL.EXE 进程
        [System.Runtime.InteropServices.Marshal]::ReleaseComObject($excel) | Out-Null
        [GC]::Collect()
        [GC]::WaitForPendingFinalizers()
    }
}

# 4. 打开文件
Invoke-Item -LiteralPath $fullPath
