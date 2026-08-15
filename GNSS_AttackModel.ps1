param(
    [string]$InputFile = "C:\gnss\gnss_log_2026_04_05_13_57_15.txt",
    [string]$OutputDir = "C:\gnss\forensic_output",
    [double]$GroundTruthLat = 33.800465,
    [double]$GroundTruthLon = -117.220354,
    [double]$GpsToleranceFeet = 15.0
)

if (-not (Test-Path $OutputDir)) {
    New-Item -ItemType Directory -Path $OutputDir | Out-Null
}

$timestampTag = (Get-Date -Format "yyyyMMdd_HHmmss")
$caseDir = Join-Path $OutputDir "GNSS_AttackCase_$timestampTag"
New-Item -ItemType Directory -Path $caseDir | Out-Null

$evidenceCopy = Join-Path $caseDir (Split-Path $InputFile -Leaf)
$incidentsCsv = Join-Path $caseDir "incidents.csv"
$kmlOut       = Join-Path $caseDir "points.kml"

Copy-Item $InputFile $evidenceCopy -Force
(Get-Item $evidenceCopy).IsReadOnly = $true

$hashFile = Join-Path $caseDir "evidence_hash.txt"
$hash = Get-FileHash -Path $evidenceCopy -Algorithm SHA256
"File: $($hash.Path)"  | Out-File $hashFile
"Algorithm: $($hash.Algorithm)" >> $hashFile
"Hash: $($hash.Hash)"  >> $hashFile

function Get-DistanceMeters {
    param(
        [double]$Lat1, [double]$Lon1,
        [double]$Lat2, [double]$Lon2
    )
    $R = 6371000
    $dLat = ($Lat2 - $Lat1) * [math]::PI / 180
    $dLon = ($Lon2 - $Lon1) * [math]::PI / 180
    $a = [math]::Sin($dLat/2)*[math]::Sin($dLat/2) +
         [math]::Cos($Lat1*[math]::PI/180) *
         [math]::Cos($Lat2*[math]::PI/180) *
         [math]::Sin($dLon/2)*[math]::Sin($dLon/2)
    $c = 2 * [math]::Atan2([math]::Sqrt($a), [math]::Sqrt(1-$a))
    return $R * $c
}

function Get-ProviderClass {
    param([string]$Provider)
    switch ($Provider.ToUpper()) {
        "GPS" { return "GNSS" }
        "FLP" { return "FUSED" }
        "NLP" { return "NETWORK" }
        default { return "UNKNOWN" }
    }
}

# --- CellMapper loading ------------------------------------------------------

function Load-CellMapperData {
    param(
        [string]$GnssInputFile
    )

    $cmBase = "C:\gnss_dump\android\data\data\cellmapper.net.cellmapper\files"
    $dates = @()

    # Try to extract date from gnss_log_YYYY_MM_DD_...
    if ($GnssInputFile -match 'gnss_log_(\d{4})_(\d{2})_(\d{2})_') {
        $y = [int]$matches[1]
        $m = [int]$matches[2]
        $d = [int]$matches[3]
        $baseDate = [datetime]::new($y, $m, $d)
        $dates += $baseDate
        $dates += $baseDate.AddDays(1)
    }

    $rows = @()

    foreach ($dt in $dates) {
        $cmPath = Join-Path $cmBase ("signal-{0:yyyy-MM-dd}.csv" -f $dt)
        if (Test-Path $cmPath) {
            Write-Host "Loading CellMapper file: $cmPath"
            Get-Content $cmPath | Where-Object { $_ -match '^\d' } | ForEach-Object {
                $p = $_.Split(',')
                if ($p.Count -ge 12) {
                    $rows += [pscustomobject]@{
                        Lat     = [double]$p[0]
                        Lon     = [double]$p[1]
                        Alt     = $p[2]
                        MCC     = $p[3]
                        MNC     = $p[4]
                        TAC     = $p[5]
                        ECI     = $p[6]
                        RSRP    = $p[7]
                        RAT1    = $p[8]
                        RAT2    = $p[9]
                        PCI     = $p[10]
                        EARFCN  = $p[11]
                        RawLine = $_
                    }
                }
            }
        }
    }

    return $rows
}

function Get-NearbyCell {
    param(
        [double]$Lat,
        [double]$Lon,
        [object[]]$Cells
    )

    if (-not $Cells -or $Cells.Count -eq 0) {
        return $null
    }

    $best = $null
    $bestDist = [double]::MaxValue

    foreach ($c in $Cells) {
        $d = Get-DistanceMeters -Lat1 $Lat -Lon1 $Lon -Lat2 $c.Lat -Lon2 $c.Lon
        if ($d -lt $bestDist) {
            $bestDist = $d
            $best = $c
        }
    }

    if ($best -eq $null) {
        return $null
    }

    return ,@($best, $bestDist)
}

$cellMapperData = Load-CellMapperData -GnssInputFile $InputFile

# --- Main processing --------------------------------------------------------

$prevByProvider = @{}
$incidents = @()
$points = @()
$incidentId = 1
$lineIndex = 0

Get-Content $evidenceCopy | Where-Object { $_ -like "Fix,*" } | ForEach-Object {

    $lineIndex++
    $line = $_
    $parts = $line.Split(",")

    if ($parts.Count -lt 9) { return }

    $provider = $parts[1]
    $lat      = [double]$parts[2]
    $lon      = [double]$parts[3]
    $alt      = $parts[4]
    $acc      = $parts[6]
    $unixMs   = $parts[8]
    $usedSats = if ($parts.Count -gt 14) { $parts[14] } else { "" }

    $providerClass = Get-ProviderClass -Provider $provider

    $dt = $null
    if ($unixMs -match '^\d+$') {
        $epoch = [DateTime]"1970-01-01T00:00:00Z"
        $dt = $epoch.AddMilliseconds([double]$unixMs)
    }

    # Distance from ground truth
    $distToTruth_m  = Get-DistanceMeters -Lat1 $GroundTruthLat -Lon1 $GroundTruthLon -Lat2 $lat -Lon2 $lon
    $distToTruth_ft = $distToTruth_m * 3.28084

    # CellMapper association (only for NETWORK / NLP)
    $cellInfo = $null
    $cellDist_m = $null
    if ($providerClass -eq "NETWORK" -and $cellMapperData.Count -gt 0) {
        $result = Get-NearbyCell -Lat $lat -Lon $lon -Cells $cellMapperData
        if ($result -ne $null -and $result.Count -eq 2) {
            $cellInfo   = $result[0]
            $cellDist_m = $result[1]
        }
    }

    # Record point for mapping
    $points += [pscustomobject]@{
        LineNumber        = $lineIndex
        Provider          = $provider
        ProviderClass     = $providerClass
        Latitude          = $lat
        Longitude         = $lon
        AltitudeMeters    = $alt
        AccuracyMeters    = $acc
        UnixTimeMillis    = $unixMs
        UtcTime           = $dt
        UsedSignals       = $usedSats
        DistToTruth_m     = [math]::Round($distToTruth_m,3)
        DistToTruth_ft    = [math]::Round($distToTruth_ft,2)
        Cell_Lat          = if ($cellInfo) { $cellInfo.Lat } else { $null }
        Cell_Lon          = if ($cellInfo) { $cellInfo.Lon } else { $null }
        Cell_MCC          = if ($cellInfo) { $cellInfo.MCC } else { $null }
        Cell_MNC          = if ($cellInfo) { $cellInfo.MNC } else { $null }
        Cell_TAC          = if ($cellInfo) { $cellInfo.TAC } else { $null }
        Cell_ECI          = if ($cellInfo) { $cellInfo.ECI } else { $null }
        Cell_RSRP         = if ($cellInfo) { $cellInfo.RSRP } else { $null }
        Cell_RAT1         = if ($cellInfo) { $cellInfo.RAT1 } else { $null }
        Cell_RAT2         = if ($cellInfo) { $cellInfo.RAT2 } else { $null }
        Cell_PCI          = if ($cellInfo) { $cellInfo.PCI } else { $null }
        Cell_EARFCN       = if ($cellInfo) { $cellInfo.EARFCN } else { $null }
        Cell_DistMeters   = if ($cellDist_m) { [math]::Round($cellDist_m,1) } else { $null }
        Cell_RawLine      = if ($cellInfo) { $cellInfo.RawLine } else { $null }
    }

    # Per-provider movement incidents
    if (-not $prevByProvider.ContainsKey($providerClass)) {
        $prevByProvider[$providerClass] = @{
            Lat      = $lat
            Lon      = $lon
            Time     = $dt
            Unix     = $unixMs
            UsedSats = $usedSats
            Line     = $lineIndex
            CellInfo = $cellInfo
            CellDist = $cellDist_m
        }
        return
    }

    $prev    = $prevByProvider[$providerClass]
    $dist_m  = Get-DistanceMeters -Lat1 $prev.Lat -Lon1 $prev.Lon -Lat2 $lat -Lon2 $lon
    $dist_ft = $dist_m * 3.28084

    $deltaSec = $null
    if ($dt -and $prev.Time) {
        $deltaSec = ($dt - $prev.Time).TotalSeconds
    }

    $severity = ""
    $physicsViolation = $false

    if ($providerClass -eq "NETWORK") {
        if ($dist_ft -gt 5 -and $dist_ft -le 20) {
            $severity = "NLP_ANOMALY"
            $physicsViolation = $true
        } elseif ($dist_ft -gt 20) {
            $severity = "NLP_CRIME"
            $physicsViolation = $true
        }
    }

    if ($providerClass -eq "GNSS") {
        if ($distToTruth_ft -gt $GpsToleranceFeet) {
            $severity = "GPS_OUT_OF_RADIUS"
            $physicsViolation = $true
        }
    }

    if ($physicsViolation) {
        $incidents += [pscustomobject]@{
            IncidentId          = $incidentId
            LineNumberTo        = $lineIndex
            Provider            = $provider
            ProviderClass       = $providerClass
            Severity            = $severity
            FromLatitude        = $prev.Lat
            FromLongitude       = $prev.Lon
            ToLatitude          = $lat
            ToLongitude         = $lon
            DistanceMeters      = [math]::Round($dist_m,3)
            DistanceFeet        = [math]::Round($dist_ft,2)
            DistToTruth_m       = [math]::Round($distToTruth_m,3)
            DistToTruth_ft      = [math]::Round($distToTruth_ft,2)
            TimeFromUtc         = $prev.Time
            TimeToUtc           = $dt
            TimeDeltaSeconds    = [math]::Round($deltaSec,3)
            FromUsedSignals     = $prev.UsedSats
            ToUsedSignals       = $usedSats
            FromUnixTimeMillis  = $prev.Unix
            ToUnixTimeMillis    = $unixMs
            RawLineTo           = $line
            Cell_Lat            = if ($cellInfo) { $cellInfo.Lat } else { $null }
            Cell_Lon            = if ($cellInfo) { $cellInfo.Lon } else { $null }
            Cell_MCC            = if ($cellInfo) { $cellInfo.MCC } else { $null }
            Cell_MNC            = if ($cellInfo) { $cellInfo.MNC } else { $null }
            Cell_TAC            = if ($cellInfo) { $cellInfo.TAC } else { $null }
            Cell_ECI            = if ($cellInfo) { $cellInfo.ECI } else { $null }
            Cell_RSRP           = if ($cellInfo) { $cellInfo.RSRP } else { $null }
            Cell_RAT1           = if ($cellInfo) { $cellInfo.RAT1 } else { $null }
            Cell_RAT2           = if ($cellInfo) { $cellInfo.RAT2 } else { $null }
            Cell_PCI            = if ($cellInfo) { $cellInfo.PCI } else { $null }
            Cell_EARFCN         = if ($cellInfo) { $cellInfo.EARFCN } else { $null }
            Cell_DistMeters     = if ($cellDist_m) { [math]::Round($cellDist_m,1) } else { $null }
            Cell_RawLine        = if ($cellInfo) { $cellInfo.RawLine } else { $null }
        }
        $incidentId++
    }

    $prevByProvider[$providerClass].Lat      = $lat
    $prevByProvider[$providerClass].Lon      = $lon
    $prevByProvider[$providerClass].Time     = $dt
    $prevByProvider[$providerClass].Unix     = $unixMs
    $prevByProvider[$providerClass].UsedSats = $usedSats
    $prevByProvider[$providerClass].Line     = $lineIndex
    $prevByProvider[$providerClass].CellInfo = $cellInfo
    $prevByProvider[$providerClass].CellDist = $cellDist_m
}

$incidents | Export-Csv -Path $incidentsCsv -NoTypeInformation -Encoding UTF8

# Also export points for Python viewer
$pointsCsv = Join-Path $caseDir "points.csv"
$points | Export-Csv -Path $pointsCsv -NoTypeInformation -Encoding UTF8

# --- KML with ground truth + GPS + NLP ---------------------------------------

$kmlHeader = @"
<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document>
<name>GNSS Attack Model</name>

<Style id="truthStyle">
  <IconStyle>
    <color>ff00ff00</color>
    <scale>1.4</scale>
    <Icon>
      <href>http://maps.google.com/mapfiles/kml/paddle/grn-circle.png</href>
    </Icon>
  </IconStyle>
</Style>

<Style id="gpsStyle">
  <IconStyle>
    <color>ffff0000</color>
    <scale>1.0</scale>
    <Icon>
      <href>http://maps.google.com/mapfiles/kml/paddle/blu-circle.png</href>
    </Icon>
  </IconStyle>
</Style>

<Style id="nlpStyle">
  <IconStyle>
    <color>ff0000ff</color>
    <scale>1.2</scale>
    <Icon>
      <href>http://maps.google.com/mapfiles/kml/paddle/purple-circle.png</href>
    </Icon>
  </IconStyle>
</Style>
"@

$kmlFooter = @"
</Document>
</kml>
"@

$placemarks = ""

# Ground truth marker
$placemarks += @"
<Placemark>
  <name>Ground Truth</name>
  <styleUrl>#truthStyle</styleUrl>
  <description><![CDATA[
    Ground truth position provided by examiner.
    Latitude: $GroundTruthLat
    Longitude: $GroundTruthLon
    GPS tolerance radius: $GpsToleranceFeet ft
  ]]></description>
  <Point>
    <coordinates>$GroundTruthLon,$GroundTruthLat,0</coordinates>
  </Point>
</Placemark>
"@

foreach ($p in $points) {

    $timeLabel = if ($p.UtcTime) { $p.UtcTime.ToString("yyyy-MM-dd HH:mm:ss 'UTC'") } else { $p.UnixTimeMillis }

    $style = "#gpsStyle"
    if ($p.ProviderClass -eq "NETWORK") { $style = "#nlpStyle" }

    $desc = @"
Line: $($p.LineNumber)
Provider: $($p.Provider) ($($p.ProviderClass))
UTC Time: $timeLabel
Latitude: $($p.Latitude)
Longitude: $($p.Longitude)
Altitude (m): $($p.AltitudeMeters)
Accuracy (m): $($p.AccuracyMeters)
Used Signals: $($p.UsedSignals)
Distance to ground truth (m): $($p.DistToTruth_m)
Distance to ground truth (ft): $($p.DistToTruth_ft)

Cell Lat: $($p.Cell_Lat)
Cell Lon: $($p.Cell_Lon)
Cell MCC: $($p.Cell_MCC)
Cell MNC: $($p.Cell_MNC)
Cell TAC: $($p.Cell_TAC)
Cell ECI: $($p.Cell_ECI)
Cell RSRP: $($p.Cell_RSRP)
Cell RAT1: $($p.Cell_RAT1)
Cell RAT2: $($p.Cell_RAT2)
Cell PCI: $($p.Cell_PCI)
Cell EARFCN: $($p.Cell_EARFCN)
Cell Dist (m): $($p.Cell_DistMeters)
"@

    $placemarks += @"
<Placemark>
  <name>$($p.Provider) - $timeLabel</name>
  <styleUrl>$style</styleUrl>
  <description><![CDATA[$desc]]></description>
  <Point>
    <coordinates>$($p.Longitude),$($p.Latitude),$($p.AltitudeMeters)</coordinates>
  </Point>
</Placemark>
"@
}

($kmlHeader + $placemarks + $kmlFooter) | Out-File $kmlOut -Encoding utf8

Write-Host "Attack model complete."
Write-Host "Case folder: $caseDir"
Write-Host "Incidents CSV: $incidentsCsv"
Write-Host "Points CSV: $pointsCsv"
Write-Host "KML: $kmlOut"
