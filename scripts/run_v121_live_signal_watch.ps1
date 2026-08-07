param(
    [Parameter(Mandatory=$false)]
    [string]$Login = "37365712",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    [Parameter(Mandatory=$false)]
    [string]$RuntimeRoot = ".\data\live_signal_runtime_v1"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$mutexName = "Global\TradeMindAI-v1.21-LiveSignalRuntime-$Login"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-Host "Live Signal Runtime already running for account $Login. Skipping overlap."
    exit 0
}

$resolvedRuntimeRoot = if ([System.IO.Path]::IsPathRooted($RuntimeRoot)) {
    $RuntimeRoot
}
else {
    Join-Path $repo $RuntimeRoot
}
$logDir = Join-Path $resolvedRuntimeRoot "logs"
$logPath = Join-Path $logDir "live_signal_watch.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

try {
    $started = Get-Date
    "[$($started.ToString('o'))] START account=$Login offset=$ServerUTCOffsetHours runtime_root=$resolvedRuntimeRoot" | Add-Content -Path $logPath -Encoding UTF8
    & (Join-Path $PSScriptRoot "run_v121_live_signal_runtime.ps1") `
        -Login $Login `
        -ServerUTCOffsetHours $ServerUTCOffsetHours `
        -RuntimeRoot $resolvedRuntimeRoot 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "Live Signal Runtime returned exit code $LASTEXITCODE"
    }
    $finished = Get-Date
    "[$($finished.ToString('o'))] OK duration_seconds=$([math]::Round(($finished - $started).TotalSeconds, 2))" | Add-Content -Path $logPath -Encoding UTF8
}
catch {
    "[$((Get-Date).ToString('o'))] ERROR $($_.Exception.Message)" | Add-Content -Path $logPath -Encoding UTF8
    throw
}
finally {
    try {
        $mutex.ReleaseMutex()
    }
    catch {
    }
    $mutex.Dispose()
}
