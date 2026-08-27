param(
    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-SER8-Autonomous-Demo-Execution",

    [Parameter(Mandatory=$false)]
    [int]$IntervalMinutes = 1,

    # NOT named $Db -- see install_ser8_mt5_reconciliation.ps1's own
    # comment on this exact same parameter for the confirmed real Windows
    # root cause (PowerShell's parameter binder treated "Db" as ambiguous
    # against the common parameter -Debug). DatabasePath cannot collide
    # with any PowerShell common parameter.
    [Parameter(Mandatory=$false)]
    [string]$DatabasePath = ".\data\ser8_registry.db",

    [Parameter(Mandatory=$false)]
    [string]$HypothesisId = "",

    # SER8 FULL SYMBOL UNIVERSE + RESEARCH RANKING V1: see
    # run_ser8_autonomous_demo_execution.ps1's own comment on this exact
    # same parameter. Empty by default -- the proven single-hypothesis
    # deployment above ($HypothesisId) is completely unaffected.
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

    [switch]$DryRun,

    [switch]$Remove
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

if ($Remove) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    }
    catch {
        throw "Failed to remove scheduled task: $TaskName ($($_.Exception.Message))"
    }
    Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Green
    exit 0
}

if ($IntervalMinutes -lt 1) {
    throw "IntervalMinutes must be at least 1"
}

$watchScript = Join-Path $PSScriptRoot "run_ser8_autonomous_demo_execution.ps1"
if (-not (Test-Path $watchScript)) {
    throw "Autonomous execution wrapper script not found: $watchScript"
}

$powershell = Join-Path $PSHOME "powershell.exe"

# Registered via the native ScheduledTasks module (New-ScheduledTaskAction /
# New-ScheduledTaskTrigger / New-ScheduledTaskPrincipal / Register-
# ScheduledTask), NEVER schtasks.exe /Create /TR -- and the trigger below
# deliberately OMITS -RepetitionDuration rather than setting it to
# [TimeSpan]::MaxValue. Both are exactly the two real Windows registration
# failures this repository already hit and fixed for
# install_v121_live_signal_watch.ps1 / install_ser8_mt5_reconciliation.ps1
# -- this installer reuses that SAME proven pattern from the start rather
# than risking either bug again.
$allowlistArgument = ($DemoAccountAllowlist | ForEach-Object { "`"$_`"" }) -join " "
$taskArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchScript`" " +
    "-DatabasePath `"$DatabasePath`" -HypothesisId `"$HypothesisId`" -Account `"$Account`" " +
    "-DemoAccountAllowlist $allowlistArgument -RuntimeRoot `"$RuntimeRoot`" -Mt5ExportDir `"$Mt5ExportDir`" " +
    "-MarketDataAccount `"$MarketDataAccount`" -ServerUTCOffsetHours $ServerUTCOffsetHours " +
    "-VolumeSourceDir `"$VolumeSourceDir`" -CommonFilesRoot `"$CommonFilesRoot`" " +
    "-CanonicalVolume `"$CanonicalVolume`" " +
    "-SealedHoldoutPath `"$SealedHoldoutPath`" " +
    "-RiskProfile `"$RiskProfile`" -CommonFilesDir `"$CommonFilesDir`""
# -HoldoutPrimaryMetric is irrelevant to autonomous execution of an
# already-ACCEPTED hypothesis (see run_ser8_autonomous_demo_execution.ps1's
# own comment on this exact same parameter for the confirmed real Windows
# root cause) -- only forwarded when the operator genuinely set it, never
# baked in as a dangling empty value.
if ($HoldoutPrimaryMetric) {
    $taskArguments += " -HoldoutPrimaryMetric `"$HoldoutPrimaryMetric`""
}
# -HypothesisIds is forwarded ONLY when the operator genuinely configured
# a multi-hypothesis deployment -- never a dangling empty array argument.
# The wrapper script's own dispatch (HypothesisIds.Count -gt 0) decides
# which mode to use; -HypothesisId above is harmlessly ignored by the
# wrapper whenever -HypothesisIds is non-empty.
if ($HypothesisIds.Count -gt 0) {
    $hypothesisIdsArgument = ($HypothesisIds | ForEach-Object { "`"$_`"" }) -join " "
    $taskArguments += " -HypothesisIds $hypothesisIdsArgument"
}
if ($DryRun) {
    $taskArguments += " -DryRun"
}

$logPath = Join-Path $repo "data\ser8_autonomous_execution\logs\autonomous_execution.log"

Write-Host "Installing scheduled task: $TaskName" -ForegroundColor Cyan
Write-Host "TaskName: $TaskName"
Write-Host "MarketDataAccount: $MarketDataAccount"
Write-Host "Account: $Account"
Write-Host "RuntimeRoot: $RuntimeRoot"
Write-Host "RiskProfile: $RiskProfile"
Write-Host "DatabasePath: $DatabasePath"
Write-Host "DryRun: $($DryRun.IsPresent)"
Write-Host "Interval: every $IntervalMinutes minute(s)"
Write-Host "LogPath: $logPath"

$action = New-ScheduledTaskAction -Execute $powershell -Argument $taskArguments -WorkingDirectory $repo

# Indefinite-by-omission (see the comment above and
# install_ser8_mt5_reconciliation.ps1's own audit note): no
# -RepetitionDuration, no [TimeSpan]::MaxValue, no arbitrary substitute
# duration.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# Same S4U/RunLevel-Highest principal as every other installer in this
# repository -- runs whether or not the user is interactively logged on,
# never collects or stores a password.
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType S4U -RunLevel Highest

try {
    # This task now owns the complete producer -> execution -> reconciliation
    # cadence. Disable the former independently-scheduled producer/reconciler
    # so they cannot overlap this sequence. Missing legacy tasks are harmless.
    foreach ($legacyTaskName in @(
        "TradeMindAI-v1.32-ECN-LiveSignalRuntime",
        "TradeMindAI-v1.21-LiveSignalRuntime",
        "TradeMindAI-SER8-MT5-Reconciliation"
    )) {
        $legacyTask = Get-ScheduledTask -TaskName $legacyTaskName -ErrorAction SilentlyContinue
        if ($null -ne $legacyTask) {
            Disable-ScheduledTask -TaskName $legacyTaskName -ErrorAction Stop | Out-Null
        }
    }
    # -Force safely replaces any existing task of the same name in one step.
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Force -ErrorAction Stop | Out-Null
}
catch {
    throw "Failed to create scheduled task: $TaskName ($($_.Exception.Message))"
}

try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
}
catch {
    throw "Scheduled task was installed but could not be started: $TaskName ($($_.Exception.Message))"
}

Write-Host "`nInstalled: $TaskName" -ForegroundColor Green
Write-Host "Each run is one bounded producer -> execution -> reconciliation cycle guarded by one"
Write-Host "Windows Global Mutex plus internal worker locks -- overlapping runs are skipped."
Write-Host "This task ONLY produces authorized execution requests; the ONLY component that ever"
Write-Host "sends a real order to the broker is the unified MT5 executor EA (TradeMind_Demo_Order_Executor_v1.mq5)."
Write-Host "Former independently-scheduled producer/reconciliation tasks are disabled to prevent overlap."
