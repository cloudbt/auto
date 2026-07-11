param(
    [Parameter(Mandatory)]
    [string]$Secret,
    [int]$Digits = 6,
    [int]$Period = 30
)

function Get-TotpCode {
    param(
        [string]$Secret,
        [int]$Digits,
        [int]$Period
    )

    $base32 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567'
    $clean = ($Secret.ToUpper() -replace '[\s=]', '')
    $bits = -join ($clean.ToCharArray() | ForEach-Object {
        $idx = $base32.IndexOf($_)
        if ($idx -lt 0) { throw "Invalid base32 character: $_" }
        [Convert]::ToString($idx, 2).PadLeft(5, '0')
    })
    $byteCount = [Math]::Floor($bits.Length / 8)
    $key = New-Object byte[] $byteCount
    for ($i = 0; $i -lt $byteCount; $i++) {
        $key[$i] = [Convert]::ToByte($bits.Substring($i * 8, 8), 2)
    }

    $unix = [int64]([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())
    $counter = [int64][Math]::Floor($unix / $Period)
    $counterBytes = [BitConverter]::GetBytes($counter)
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($counterBytes) }

    $hmac = [System.Security.Cryptography.HMACSHA1]::new($key)
    try {
        $hash = $hmac.ComputeHash($counterBytes)
    } finally {
        $hmac.Dispose()
    }

    $offset = $hash[-1] -band 0x0F
    $bin = ((($hash[$offset]     -band 0x7F) -shl 24) -bor `
            (($hash[$offset + 1] -band 0xFF) -shl 16) -bor `
            (($hash[$offset + 2] -band 0xFF) -shl 8 ) -bor `
            ( $hash[$offset + 3] -band 0xFF))
    $code = $bin % [int][Math]::Pow(10, $Digits)
    return ([string]$code).PadLeft($Digits, '0')
}

Get-TotpCode -Secret $Secret -Digits $Digits -Period $Period
