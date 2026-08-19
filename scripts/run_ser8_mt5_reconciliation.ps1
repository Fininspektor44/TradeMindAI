param(
    [Parameter(Mandatory=$false)]
    [string]$Db = ".\data\ser8_registry.db",

    [Parameter(Mandatory=$false)]
    [string]$Account = "67206924",

    # The unified executor's own Common\Files output folder -- the SAME
    # directory mt5_risk_orders_utc_<Account>.csv / mt5_risk_deals_utc_
    # <Account>.csv (and every other mt5_risk_*_utc_*.csv export) already
    # lands in.
    [Parameter(Mandatory=$false)]
    [string]$Mt5ExportDir = ".\data\mt5",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Single-instance guard -- the SAME Global Mutex pattern
# run_v121_live_signal_watch.ps1 already uses, so overlapping Scheduled
# Task runs (e.g. a slow cycle still running when the next tick fires)
# skip cleanly instead of racing. The Python script's own --once mode
# also carries an independent lock-file guard as defense in depth.
$mutexName = "Global\TradeMindAI-SER8-MT5-Reconciliation-$Account"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-Host "SER8 MT5 reconciliation already running for account $Account. Skipping overlap."
    exit 0
}

$logDir = Join-Path $repo "data\ser8_reconciliation\logs"
$logPath = Join-Path $logDir "reconcile.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}
$script = Join-Path $PSScriptRoot "reconcile_ser8_mt5_execution.py"

try {
    $started = Get-Date
    "[$($started.ToString('o'))] START account=$Account db=$Db mt5_export_dir=$Mt5ExportDir dry_run=$($DryRun.IsPresent)" |
        Add-Content -Path $logPath -Encoding UTF8

    $arguments = @(
        $script,
        "--db", $Db,
        "--account", $Account,
        "--mt5-export-dir", $Mt5ExportDir,
        "--once"
    )
    if ($DryRun) {
        $arguments += "--dry-run"
    }

    & $python @arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    if ($LASTEXITCODE -eq 3) {
        # Lock file contention -- another instance is genuinely running;
        # not an error worth alarming on, the mutex above already proves
        # this shouldn't normally happen.
        Write-Host "Reconciliation cycle skipped: lock file busy."
    }
    elseif ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
        # Exit code 1 means the cycle ran but reported AMBIGUOUS_PRESENT
        # (expected, non-fatal -- some legs need more evidence next
        # cycle); anything else is a genuine failure.
        throw "SER8 MT5 reconciliation returned exit code $LASTEXITCODE"
    }

    $finished = Get-Date
    "[$($finished.ToString('o'))] OK duration_seconds=$([math]::Round(($finished - $started).TotalSeconds, 2))" |
        Add-Content -Path $logPath -Encoding UTF8
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
