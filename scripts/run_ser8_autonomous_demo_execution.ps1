param(
    # NOT named $Db -- see scripts/run_ser8_mt5_reconciliation.ps1's own
    # comment on this exact same parameter for the confirmed real Windows
    # root cause (PowerShell's parameter binder treats "Db" as ambiguous
    # against the common parameter -Debug). DatabasePath cannot collide
    # with any PowerShell common parameter.
    [Parameter(Mandatory=$false)]
    [string]$DatabasePath = ".\data\ser8_registry.db",

    [Parameter(Mandatory=$false)]
    [string]$HypothesisId = "rpi-v1:sha256:205b5260711f7578a59cef2feea59550b777b3df0956ffd192076b37c4e5866d:0",

    [Parameter(Mandatory=$false)]
    [string]$Account = "67206924",

    [Parameter(Mandatory=$false)]
    [string[]]$DemoAccountAllowlist = @("67206924"),

    [Parameter(Mandatory=$false)]
    [string]$RuntimeRoot = ".\data\live_signal_runtime_ecN_77053345",

    [Parameter(Mandatory=$false)]
    [string]$Mt5ExportDir = ".\data\mt5",

    [Parameter(Mandatory=$false)]
    [string]$SealedHoldoutPath = ".\data\ser8_bootstrap_datasets\9aac0c46f54e51df3c04c9dc9a51ce906da501ed7c7fd9b9ad1ac1055b582a05.final-holdout.sealed.json",

    [Parameter(Mandatory=$false)]
    [string]$HoldoutPrimaryMetric = "",

    [Parameter(Mandatory=$false)]
    [string]$RiskProfile = ".\config\risk_profiles\ser8_supervised_demo_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesDir = ".\data\mt5_common",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

# Single-instance guard -- the SAME Global Mutex pattern
# run_ser8_mt5_reconciliation.ps1 / run_v121_live_signal_watch.ps1 already
# use, so an overlapping Scheduled Task run (e.g. a slow cycle still
# running when the next tick fires) skips cleanly instead of racing. The
# Python script's own --once mode also carries an independent lock-file
# guard as defense in depth.
$mutexName = "Global\TradeMindAI-SER8-Autonomous-Demo-Execution-$Account"
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
if (-not $createdNew) {
    Write-Host "SER8 autonomous demo execution already running for account $Account. Skipping overlap."
    exit 0
}

$logDir = Join-Path $repo "data\ser8_autonomous_execution\logs"
$logPath = Join-Path $logDir "autonomous_execution.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

$python = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python environment not found: $python"
}
$script = Join-Path $PSScriptRoot "run_ser8_autonomous_demo_execution.py"

try {
    $started = Get-Date
    "[$($started.ToString('o'))] START account=$Account hypothesis_id=$HypothesisId db=$DatabasePath runtime_root=$RuntimeRoot dry_run=$($DryRun.IsPresent)" |
        Add-Content -Path $logPath -Encoding UTF8

    $arguments = @(
        $script,
        "--db", $DatabasePath,
        "--hypothesis-id", $HypothesisId,
        "--account", $Account,
        "--demo-account-allowlist"
    ) + $DemoAccountAllowlist + @(
        "--runtime-root", $RuntimeRoot,
        "--mt5-export-dir", $Mt5ExportDir,
        "--sealed-holdout-path", $SealedHoldoutPath,
        "--holdout-primary-metric", $HoldoutPrimaryMetric,
        "--risk-profile", $RiskProfile,
        "--common-files-dir", $CommonFilesDir,
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
        Write-Host "Autonomous execution cycle skipped: lock file busy."
    }
    elseif ($LASTEXITCODE -ne 0 -and $LASTEXITCODE -ne 1) {
        # Exit code 1 means the cycle ran but reported a non-fatal,
        # self-healing status (e.g. an authorization/claim conflict that
        # clears on its own once the existing TTL expires, or a
        # PENDING/PARTIAL execution state awaiting reconciliation) --
        # expected, non-fatal. Anything else is a genuine failure.
        throw "SER8 autonomous demo execution returned exit code $LASTEXITCODE"
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
