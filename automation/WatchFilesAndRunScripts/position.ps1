Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing

if (-not ('Win32Cursor' -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;

public class Win32Cursor {
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT {
        public int X;
        public int Y;
    }

    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool GetCursorPos(out POINT lpPoint);
}
"@
}

function Get-MousePosition {
    $point = New-Object Win32Cursor+POINT
    if (-not [Win32Cursor]::GetCursorPos([ref]$point)) {
        throw "GetCursorPos failed."
    }

    [pscustomobject]@{
        X = $point.X
        Y = $point.Y
    }
}

function Clamp-Value {
    param(
        [Parameter(Mandatory = $true)][int]$Value,
        [Parameter(Mandatory = $true)][int]$Min,
        [Parameter(Mandatory = $true)][int]$Max
    )

    if ($Value -lt $Min) { return $Min }
    if ($Value -gt $Max) { return $Max }
    return $Value
}

$form = New-Object System.Windows.Forms.Form
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$form.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$form.TopMost = $true
$form.BackColor = [System.Drawing.Color]::Black
$form.Opacity = 0.8
$form.ShowInTaskbar = $false
$form.KeyPreview = $true

$label = New-Object System.Windows.Forms.Label
$label.AutoSize = $true
$label.BackColor = [System.Drawing.Color]::Black
$label.ForeColor = [System.Drawing.Color]::White
$label.Font = New-Object System.Drawing.Font("Segoe UI", 10, [System.Drawing.FontStyle]::Bold)
$label.Padding = New-Object System.Windows.Forms.Padding(4, 2, 4, 2)
$label.Text = "0, 0"
$form.Controls.Add($label)

$form.Add_KeyDown({
    if ($_.KeyCode -eq [System.Windows.Forms.Keys]::Escape) {
        $form.Close()
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 50
$timer.Add_Tick({
    $position = Get-MousePosition
    $label.Text = "DEC {0}, {1}  HEX 0x{0:X4}, 0x{1:X4}" -f $position.X, $position.Y

    $label.Size = $label.PreferredSize
    $windowWidth = $label.PreferredWidth + 8
    $windowHeight = $label.PreferredHeight + 4

    $screen = [System.Windows.Forms.Screen]::FromPoint(
        (New-Object System.Drawing.Point($position.X, $position.Y))
    ).WorkingArea

    $offsetX = 16
    $offsetY = 24
    $newX = Clamp-Value -Value ($position.X + $offsetX) -Min $screen.Left -Max ($screen.Right - $windowWidth)
    $newY = Clamp-Value -Value ($position.Y + $offsetY) -Min $screen.Top -Max ($screen.Bottom - $windowHeight)

    $form.Size = New-Object System.Drawing.Size($windowWidth, $windowHeight)
    $form.Location = New-Object System.Drawing.Point($newX, $newY)
})

Write-Host "Mouse position overlay is running. Press Esc to exit."
$timer.Start()
[void]$form.ShowDialog()
$timer.Stop()
$timer.Dispose()
$form.Dispose()
