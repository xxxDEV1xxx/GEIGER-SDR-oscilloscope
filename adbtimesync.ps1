# Android vs Windows clock diagnostic
# Requires adb.exe in the same directory as this script.

$ErrorActionPreference = "Stop"

$adbPath = Join-Path $PSScriptRoot "adb.exe"

if (-not (Test-Path $adbPath)) {
    Write-Host "ERROR: adb.exe not found:"
    Write-Host "  $adbPath"
    exit 1
}

Write-Host ""
Write-Host "========================================"
Write-Host "  WINDOWS vs ANDROID CLOCK DIAGNOSTIC"
Write-Host "========================================"
Write-Host ""

# ------------------------------------------------------------
# Check ADB
# ------------------------------------------------------------

$devices = & $adbPath devices 2>&1

$device = $devices |
    Where-Object { $_ -match "^\S+\s+device$" } |
    Select-Object -First 1

if (-not $device) {
    Write-Host "ERROR: No Android device connected via ADB."
    Write-Host ""
    $devices | ForEach-Object { Write-Host "  $_" }
    exit 1
}

Write-Host "ADB device: $device"
Write-Host ""

# ------------------------------------------------------------
# Android raw clock information
# ------------------------------------------------------------

$androidEpochSec = (& $adbPath shell date +%s).Trim()
$androidEpochMs  = (& $adbPath shell date +%s%3N).Trim()
$androidLocal    = (& $adbPath shell date).Trim()
$androidUtc      = (& $adbPath shell date -u).Trim()
$androidTz       = (& $adbPath shell getprop persist.sys.timezone).Trim()

Write-Host "ANDROID RAW CLOCK"
Write-Host "----------------------------------------"
Write-Host "Epoch seconds : $androidEpochSec"
Write-Host "Epoch ms      : $androidEpochMs"
Write-Host "Local date    : $androidLocal"
Write-Host "UTC date      : $androidUtc"
Write-Host "Timezone      : $androidTz"
Write-Host ""

# ------------------------------------------------------------
# Validate Android epoch
# ------------------------------------------------------------

if ($androidEpochSec -notmatch '^\d+$') {
    Write-Host "ERROR: Invalid Android epoch seconds:"
    Write-Host "  $androidEpochSec"
    exit 1
}

$androidSec = [Int64]$androidEpochSec

if ($androidEpochMs -match '^\d+$') {
    $androidMs = [Int64]$androidEpochMs
}
else {
    $androidMs = $androidSec * 1000
}

# ------------------------------------------------------------
# Windows clock
# ------------------------------------------------------------

$windowsNow = [DateTimeOffset]::UtcNow
$windowsMs  = $windowsNow.ToUnixTimeMilliseconds()
$windowsLocal = Get-Date

$offsetMs = $androidMs - $windowsMs

Write-Host "WINDOWS CLOCK"
Write-Host "----------------------------------------"
Write-Host "UTC   : $($windowsNow.ToString('yyyy-MM-dd HH:mm:ss.fff'))"
Write-Host "Local : $($windowsLocal.ToString('yyyy-MM-dd HH:mm:ss.fff'))"
Write-Host "Epoch : $windowsMs"
Write-Host ""

# ------------------------------------------------------------
# Comparison
# ------------------------------------------------------------

Write-Host "CLOCK COMPARISON"
Write-Host "----------------------------------------"

if ($offsetMs -gt 0) {
    Write-Host "Android ahead : $offsetMs ms"
}
elseif ($offsetMs -lt 0) {
    Write-Host "Android behind: $([Math]::Abs($offsetMs)) ms"
}
else {
    Write-Host "Clocks exactly equal."
}

Write-Host "Difference    : $([Math]::Round($offsetMs / 1000.0, 3)) seconds"
Write-Host ""

# ------------------------------------------------------------
# Multiple samples
# ------------------------------------------------------------

Write-Host "5-SAMPLE CLOCK TEST"
Write-Host "----------------------------------------"

$samples = @()

for ($i = 1; $i -le 5; $i++) {

    $pcBefore = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

    $aSec = (& $adbPath shell date +%s).Trim()

    $pcAfter = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()

    if ($aSec -match '^\d+$') {

        $aMs = [Int64]$aSec * 1000
        $pcMid = [Math]::Floor(($pcBefore + $pcAfter) / 2)
        $diff = $aMs - $pcMid
        $rtt = $pcAfter - $pcBefore

        $samples += $diff

        Write-Host ("Sample {0}: Android offset {1} ms | ADB RTT {2} ms" -f `
            $i, $diff, $rtt)
    }
    else {
        Write-Host "Sample ${i}: Android clock read failed."
    }

    Start-Sleep -Milliseconds 250
}

if ($samples.Count -gt 0) {

    $min = ($samples | Measure-Object -Minimum).Minimum
    $max = ($samples | Measure-Object -Maximum).Maximum
    $avg = ($samples | Measure-Object -Average).Average

    Write-Host ""
    Write-Host "SAMPLE SUMMARY"
    Write-Host "----------------------------------------"
    Write-Host "Minimum offset : $min ms"
    Write-Host "Maximum offset : $max ms"
    Write-Host "Average offset : $([Math]::Round($avg, 3)) ms"
    Write-Host "Spread         : $($max - $min) ms"
}

Write-Host ""
Write-Host "========================================"
Write-Host "  END"
Write-Host "========================================"
Write-Host ""
