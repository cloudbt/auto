param(
    [string]$ConfigPath = "C:\work\work-git\git\auto\Watch-FilesAndRunScripts.config.json",
    [int]$IntervalSeconds = 120,
    [string]$LogPath = "C:\work\work-git\git\auto\WatchFilesAndRunScripts\Watch-FilesAndRunScripts.log"
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

    if ($Level -eq "WARN") {
        Write-Warning $Message
    }
    elseif ($Level -eq "ERROR") {
        Write-Error $Message
    }
    else {
        Write-Host $line
    }
}

function Get-FileState {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)]
        [ValidateSet("Metadata", "ContentHash")]
        [string]$CheckMode
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }

    $item = Get-Item -LiteralPath $Path

    if ($CheckMode -eq "ContentHash") {
        $hash = Get-FileHash -LiteralPath $Path -Algorithm SHA256
        return $hash.Hash
    }

    return "{0}|{1}" -f $item.LastWriteTimeUtc.Ticks, $item.Length
}

function Invoke-TargetScript {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Log -Level "WARN" -Message "Script not found: $Path"
        return
    }

    Write-Log -Message "Running: $Path"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $Path
}

function New-WatchJob {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$ScriptPath,
        [Parameter(Mandatory = $true)]
        [ValidateSet("Metadata", "ContentHash")]
        [string]$CheckMode
    )

    [pscustomobject]@{
        Name = $Name
        FilePath = $FilePath
        ScriptPath = $ScriptPath
        CheckMode = $CheckMode
        LastState = $null
        PendingState = $null
        PendingSince = $null
    }
}

function Get-WatchJobs {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath
    )

    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        throw "Config file not found: $ConfigPath"
    }

    $config = Get-Content -LiteralPath $ConfigPath -Raw | ConvertFrom-Json
    return @($config.WatchJobs | Where-Object { $_.Enabled -ne $false } | ForEach-Object {
        New-WatchJob -Name $_.Name -FilePath $_.FilePath -ScriptPath $_.ScriptPath -CheckMode $_.CheckMode
    })
}

$WatchJobs = Get-WatchJobs -ConfigPath $ConfigPath

if ($WatchJobs.Count -eq 0) {
    Write-Log -Level "WARN" -Message "No enabled watch jobs found in config: $ConfigPath"
    Write-Host "Usage:"
    Write-Host "  powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -ConfigPath `"C:\work\work-git\git\auto\Watch-FilesAndRunScripts.config.json`""
    exit 1
}

if ($IntervalSeconds -lt 1) {
    throw "IntervalSeconds must be greater than 0."
}

Write-Log -Message "Watching files every $IntervalSeconds seconds. Config: $ConfigPath"
$WatchJobs | ForEach-Object {
    Write-Log -Message ("{0}: {1} -> {2} [{3}]" -f $_.Name, $_.FilePath, $_.ScriptPath, $_.CheckMode)
}
Write-Log -Message "Press Ctrl+C to stop."

foreach ($job in $WatchJobs) {
    if ([string]::IsNullOrWhiteSpace($job.FilePath)) {
        throw "$($job.Name) FilePath is empty."
    }

    if ([string]::IsNullOrWhiteSpace($job.ScriptPath)) {
        throw "$($job.Name) ScriptPath is empty."
    }

    $job.LastState = Get-FileState -Path $job.FilePath -CheckMode $job.CheckMode
}

while ($true) {
    Start-Sleep -Seconds $IntervalSeconds

    foreach ($job in $WatchJobs) {
        try {
            $currentState = Get-FileState -Path $job.FilePath -CheckMode $job.CheckMode

            if ($null -eq $currentState) {
                Write-Log -Level "WARN" -Message "$($job.Name) not found: $($job.FilePath)"
                $job.PendingState = $null
                $job.PendingSince = $null
                continue
            }

            if ($null -eq $job.LastState) {
                $job.LastState = $currentState
                $job.PendingState = $null
                $job.PendingSince = $null
                continue
            }

            if ($currentState -eq $job.LastState) {
                if ($null -ne $job.PendingState) {
                    Write-Log -Message "$($job.Name) returned to baseline. Pending execution canceled."
                }
                $job.PendingState = $null
                $job.PendingSince = $null
                continue
            }

            if ($job.PendingState -ne $currentState) {
                $job.PendingState = $currentState
                $job.PendingSince = Get-Date
                Write-Log -Message "$($job.Name) changed. Waiting $IntervalSeconds seconds before running script."
                continue
            }

            $elapsedSeconds = ((Get-Date) - $job.PendingSince).TotalSeconds
            if ($elapsedSeconds -ge $IntervalSeconds) {
                Write-Log -Message "$($job.Name) change confirmed after waiting. Executing script."
                $job.LastState = $currentState
                $job.PendingState = $null
                $job.PendingSince = $null
                Invoke-TargetScript -Path $job.ScriptPath
            }
        }
        catch {
            Write-Log -Level "WARN" -Message "$($job.Name): $($_.Exception.Message)"
        }
    }
}
