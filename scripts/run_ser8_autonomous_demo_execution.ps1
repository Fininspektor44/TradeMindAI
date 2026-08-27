param(
    # NOT named $Db -- see scripts/run_ser8_mt5_reconciliation.ps1's own
    # comment on this exact same parameter for the confirmed real Windows
    # root cause (PowerShell's parameter binder treats "Db" as ambiguous
    # against the common parameter -Debug). DatabasePath cannot collide
    # with any PowerShell common parameter.
    [Parameter(Mandatory=$false)]
    [string]$DatabasePath = ".\data\ser8_registry.db",

    [Parameter(Mandatory=$false)]
    [string]$HypothesisId = "",

    # SER8 FULL SYMBOL UNIVERSE + RESEARCH RANKING V1: an explicitly
    # configured SET of ACCEPTED hypotheses for the generalized, symbol-
    # agnostic multi-hypothesis router. Empty by default -- the proven
    # single-hypothesis path above ($HypothesisId) remains completely
    # unaffected and is used whenever this stays empty. Mutually
    # exclusive with $HypothesisId, exactly like the Python CLI's own
    # --hypothesis-id / --hypothesis-ids.
    [Parameter(Mandatory=$false)]
    [string[]]$HypothesisIds = @(
        "core8-op:CHFJPY",
        "core8-op:EURJPY",
        "core8-op:EURNZD",
        "core8-op:GBPAUD",
        "core8-op:GBPNZD",
        "core8-op:NZDCAD",
        "core8-op:NZDCHF",
        "core8-op:USDJPY"
    ),

    [Parameter(Mandatory=$false)]
    [string]$MarketDataAccount = "77053345",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    [Parameter(Mandatory=$false)]
    [string]$VolumeSourceDir = "",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesRoot = "",

    [Parameter(Mandatory=$false)]
    [string]$CanonicalVolume = ".\data\volume_v1_4\volume_bars.csv",

    [Parameter(Mandatory=$false)]
    [string]$Account = "67206924",

    [Parameter(Mandatory=$false)]
    [string[]]$DemoAccountAllowlist = @("67206924"),

    [Parameter(Mandatory=$false)]
    [string]$RuntimeRoot = ".\data\live_signal_runtime_ecN_77053345",

    [Parameter(Mandatory=$false)]
    [string]$Mt5ExportDir = "",

    [Parameter(Mandatory=$false)]
    [string]$SealedHoldoutPath = ".\data\ser8_bootstrap_datasets\9aac0c46f54e51df3c04c9dc9a51ce906da501ed7c7fd9b9ad1ac1055b582a05.final-holdout.sealed.json",

    [Parameter(Mandatory=$false)]
    [string]$HoldoutPrimaryMetric = "",

    [Parameter(Mandatory=$false)]
    [string]$RiskProfile = ".\config\risk_profiles\ser8_supervised_demo_v1.json",

    [Parameter(Mandatory=$false)]
    [string]$CommonFilesDir = "",

    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$defaultCommonFilesRoot = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI"
$defaultVolumeSourceDir = Join-Path $env:APPDATA "MetaQuotes\Terminal\Common\Files\TradeMindAI_Volume_v1_4"
if ([string]::IsNullOrWhiteSpace($VolumeSourceDir)) {
    $VolumeSourceDir = $defaultVolumeSourceDir
}
if ([string]::IsNullOrWhiteSpace($CommonFilesRoot)) {
    $CommonFilesRoot = $defaultCommonFilesRoot
}
if ([string]::IsNullOrWhiteSpace($Mt5ExportDir)) {
    $Mt5ExportDir = $defaultCommonFilesRoot
}
if ([string]::IsNullOrWhiteSpace($CommonFilesDir)) {
    $CommonFilesDir = $defaultCommonFilesRoot
}

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
    # One mutex covers the complete operational chain. The producer is the
    # existing v1.22 runtime (canonical volume collector -> authoritative OTE
    # observations -> candidates.jsonl), followed by the existing autonomous
    # worker and the existing read-only reconciliation entrypoint. A slow
    # transport therefore cannot overlap the next producer/execution tick.
    $producerScript = Join-Path $PSScriptRoot "run_v121_live_signal_runtime.ps1"
    & $producerScript `
        -Login $MarketDataAccount `
        -VolumeSourceDir $VolumeSourceDir `
        -CommonFilesRoot $CommonFilesRoot `
        -CanonicalVolume $CanonicalVolume `
        -RuntimeRoot $RuntimeRoot `
        -ServerUTCOffsetHours $ServerUTCOffsetHours 2>&1 |
        Tee-Object -FilePath $logPath -Append

    $usingMultiHypothesis = $HypothesisIds.Count -gt 0
    $started = Get-Date
    if ($usingMultiHypothesis) {
        "[$($started.ToString('o'))] START account=$Account hypothesis_ids=$($HypothesisIds -join ',') db=$DatabasePath runtime_root=$RuntimeRoot dry_run=$($DryRun.IsPresent)" |
            Add-Content -Path $logPath -Encoding UTF8
    }
    else {
        "[$($started.ToString('o'))] START account=$Account hypothesis_id=$HypothesisId db=$DatabasePath runtime_root=$RuntimeRoot dry_run=$($DryRun.IsPresent)" |
            Add-Content -Path $logPath -Encoding UTF8
    }

    # Exactly one of --hypothesis-id / --hypothesis-ids is ever forwarded
    # -- never both, matching the Python CLI's own exactly-one-of
    # validation. The single-hypothesis branch below is byte-identical
    # to this wrapper's own original, proven argument list.
    if ($usingMultiHypothesis) {
        $arguments = @(
            $script,
            "--db", $DatabasePath,
            "--hypothesis-ids"
        ) + $HypothesisIds + @(
            "--account", $Account,
            "--demo-account-allowlist"
        ) + $DemoAccountAllowlist + @(
            "--runtime-root", $RuntimeRoot,
            "--mt5-export-dir", $Mt5ExportDir,
            "--sealed-holdout-path", $SealedHoldoutPath,
            "--risk-profile", $RiskProfile,
            "--common-files-dir", $CommonFilesDir,
            "--once"
        )
    }
    else {
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
            "--risk-profile", $RiskProfile,
            "--common-files-dir", $CommonFilesDir,
            "--once"
        )
    }
    # CONFIRMED REAL WINDOWS FAILURE (SER8 AUTONOMOUS WINDOWS HOLDOUT
    # METRIC ARGUMENT FIX V1): PowerShell silently DROPS an empty-string
    # array element when splatting @arguments into a NATIVE executable
    # call (python.exe below) -- "--holdout-primary-metric", "" does NOT
    # produce a literal empty-string argv value; the flag NAME survives
    # but its value vanishes, so the Python CLI's argparse sees a
    # dangling option and exits with a usage error BEFORE any
    # authorization/claim/send is ever attempted. --holdout-primary-metric
    # is also genuinely optional at the Python side (run_ser8_autonomous_
    # demo_execution.py's own --holdout-primary-metric default=None --
    # this worker never advances the research lifecycle, so the value is
    # never actually read for an already-ACCEPTED hypothesis). The fix is
    # therefore two-layered: the flag is OPTIONAL on the Python side, AND
    # this wrapper only ever forwards it when genuinely non-empty --
    # never passes a dangling/empty native argument at all.
    if ($HoldoutPrimaryMetric) {
        $arguments += @("--holdout-primary-metric", $HoldoutPrimaryMetric)
    }
    if ($DryRun) {
        $arguments += "--dry-run"
    }

    & $python @arguments 2>&1 | Tee-Object -FilePath $logPath -Append
    $executionExitCode = $LASTEXITCODE
    if ($executionExitCode -eq 3) {
        # Lock file contention -- another instance is genuinely running;
        # not an error worth alarming on, the mutex above already proves
        # this shouldn't normally happen.
        Write-Host "Autonomous execution cycle skipped: lock file busy."
    }
    elseif ($executionExitCode -ne 0 -and $executionExitCode -ne 1) {
        # Exit code 1 means the cycle ran but reported a non-fatal,
        # self-healing status (e.g. an authorization/claim conflict that
        # clears on its own once the existing TTL expires, or a
        # PENDING/PARTIAL execution state awaiting reconciliation) --
        # expected, non-fatal. Anything else is a genuine failure.
        throw "SER8 autonomous demo execution returned exit code $executionExitCode"
    }

    $reconciliationScript = Join-Path $PSScriptRoot "reconcile_ser8_mt5_execution.py"
    $reconciliationArguments = @(
        $reconciliationScript,
        "--db", $DatabasePath,
        "--account", $Account,
        "--mt5-export-dir", $Mt5ExportDir,
        "--once"
    )
    if ($DryRun) {
        $reconciliationArguments += "--dry-run"
    }
    & $python @reconciliationArguments 2>&1 | Tee-Object -FilePath $logPath -Append
    $reconciliationExitCode = $LASTEXITCODE
    if ($reconciliationExitCode -eq 3) {
        Write-Host "Reconciliation cycle skipped: lock file busy."
    }
    elseif ($reconciliationExitCode -ne 0 -and $reconciliationExitCode -ne 1) {
        throw "SER8 MT5 reconciliation returned exit code $reconciliationExitCode"
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
