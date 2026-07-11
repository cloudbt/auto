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

# 4. 在桌面创建快捷方式，方便今后打开
#    注意：不能用 WScript.Shell 的 CreateShortcut——它用系统 ANSI 代码页处理
#    路径，在非中文系统区域设置（本机为日文）下无法表示中文文件名，会报
#    “Value does not fall within the expected range.”。这里改用 Unicode 版的
#    IShellLink / IPersistFile 接口，可正确处理中文路径。
if (-not ('ShellLinkHelper.Lnk' -as [type])) {
    Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Text;
namespace ShellLinkHelper {
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    internal class ShellLink { }
    [ComImport, InterfaceType(ComInterfaceType.InterfaceIsIUnknown),
     Guid("000214F9-0000-0000-C000-000000000046")]
    internal interface IShellLinkW {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder f, int c, IntPtr p, int fl);
        void GetIDList(out IntPtr ppidl);
        void SetIDList(IntPtr pidl);
        void GetDescription([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder n, int c);
        void SetDescription([MarshalAs(UnmanagedType.LPWStr)] string n);
        void GetWorkingDirectory([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder d, int c);
        void SetWorkingDirectory([MarshalAs(UnmanagedType.LPWStr)] string d);
        void GetArguments([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder a, int c);
        void SetArguments([MarshalAs(UnmanagedType.LPWStr)] string a);
        void GetHotkey(out short w);
        void SetHotkey(short w);
        void GetShowCmd(out int c);
        void SetShowCmd(int c);
        void GetIconLocation([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder p, int c, out int i);
        void SetIconLocation([MarshalAs(UnmanagedType.LPWStr)] string p, int i);
        void SetRelativePath([MarshalAs(UnmanagedType.LPWStr)] string p, int r);
        void Resolve(IntPtr h, int fl);
        void SetPath([MarshalAs(UnmanagedType.LPWStr)] string f);
    }
    public static class Lnk {
        public static void Create(string lnkPath, string target, string workDir, string desc) {
            IShellLinkW link = (IShellLinkW)new ShellLink();
            link.SetPath(target);
            if (!string.IsNullOrEmpty(workDir)) link.SetWorkingDirectory(workDir);
            if (!string.IsNullOrEmpty(desc)) link.SetDescription(desc);
            ((IPersistFile)link).Save(lnkPath, false);
        }
    }
}
'@
}
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path -Path $desktop -ChildPath ([System.IO.Path]::GetFileNameWithoutExtension($fullPath) + '.lnk')
[ShellLinkHelper.Lnk]::Create(
    $shortcutPath,
    $fullPath,
    [System.IO.Path]::GetDirectoryName($fullPath),
    "打开 $([System.IO.Path]::GetFileName($fullPath))"
)
Write-Host "已在桌面创建快捷方式: $shortcutPath"

# 5. 打开文件
Invoke-Item -LiteralPath $fullPath
