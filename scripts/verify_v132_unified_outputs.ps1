param(
    [string]$Login = "77053345"
)

$ErrorActionPreference = "Stop"
$common = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files"
$now = Get-Date

$expected = @(
    [pscustomobject]@{ Kind = "risk-account"; Path = Join-Path $common "TradeMindAI\mt5_risk_account_utc_$Login.csv"; MaxAgeSeconds = 90; RequireFresh = $true },
    [pscustomobject]@{ Kind = "risk-positions"; Path = Join-Path $common "TradeMindAI\mt5_risk_positions_utc_$Login.csv"; MaxAgeSeconds = 90; RequireFresh = $true },
    [pscustomobject]@{ Kind = "risk-symbols"; Path = Join-Path $common "TradeMindAI\mt5_risk_symbols_utc_$Login.csv"; MaxAgeSeconds = 90; RequireFresh = $true },
    [pscustomobject]@{ Kind = "deal-account"; Path = Join-Path $common "TradeMindAI\grid_account_$Login.csv"; MaxAgeSeconds = 150; RequireFresh = $true },
    [pscustomobject]@{ Kind = "deals"; Path = Join-Path $common "TradeMindAI\grid_deals_$Login.csv"; MaxAgeSeconds = 150; RequireFresh = $true },
    # The legacy deal-position exporter appends rows only when a position matches its scope.
    # With no matching positions the file can legitimately keep an old LastWriteTime.
    [pscustomobject]@{ Kind = "deal-positions"; Path = Join-Path $common "TradeMindAI\grid_positions_$Login.csv"; MaxAgeSeconds = 150; RequireFresh = $false }
)

$rows = foreach ($item in $expected) {
    $file = Get-Item $item.Path -ErrorAction SilentlyContinue
    if (-not $file) {
        [pscustomobject]@{
            Kind = $item.Kind
            Exists = $false
            AgeSeconds = $null
            Fresh = $false
            RequiredFresh = $item.RequireFresh
            Path = $item.Path
        }
        continue
    }

    $age = [math]::Round(($now - $file.LastWriteTime).TotalSeconds, 1)
    [pscustomobject]@{
        Kind = $item.Kind
        Exists = $true
        AgeSeconds = $age
        Fresh = ($age -le $item.MaxAgeSeconds)
        RequiredFresh = $item.RequireFresh
        Path = $file.FullName
    }
}

$volumeDir = Join-Path $common "TradeMindAI_Volume_v1_4"
$latestVolume = Get-ChildItem $volumeDir -File -Filter "volume_*_M5.csv" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($latestVolume) {
    $volumeAge = [math]::Round(($now - $latestVolume.LastWriteTime).TotalSeconds, 1)
    $rows += [pscustomobject]@{
        Kind = "volume-latest"
        Exists = $true
        AgeSeconds = $volumeAge
        Fresh = ($volumeAge -le 420)
        RequiredFresh = $true
        Path = $latestVolume.FullName
    }
} else {
    $rows += [pscustomobject]@{
        Kind = "volume-latest"
        Exists = $false
        AgeSeconds = $null
        Fresh = $false
        RequiredFresh = $true
        Path = $volumeDir
    }
}

$rows | Format-Table Kind,Exists,AgeSeconds,Fresh,RequiredFresh,Path -AutoSize

$failures = @($rows | Where-Object {
    (-not $_.Exists) -or ($_.RequiredFresh -and (-not $_.Fresh))
})
if ($failures.Count -gt 0) {
    Write-Host "FAILED unified output checks:" -ForegroundColor Red
    $failures | Format-Table Kind,Exists,AgeSeconds,Fresh,RequiredFresh,Path -AutoSize
    throw "One or more required unified exporter outputs are missing or stale. Keep or restore the legacy exporters until this is resolved."
}

$staleOptional = @($rows | Where-Object { $_.Exists -and (-not $_.RequiredFresh) -and (-not $_.Fresh) })
if ($staleOptional.Count -gt 0) {
    Write-Host "NOTE: deal-positions can remain timestamp-stale when no matching positions exist; this is not a failure." -ForegroundColor Yellow
}

Write-Host "v1.32 unified ECN required outputs are fresh for login $Login" -ForegroundColor Green
Write-Host "Unified exporter can run as the sole exporter after visually confirming the EA is still attached." -ForegroundColor Green
