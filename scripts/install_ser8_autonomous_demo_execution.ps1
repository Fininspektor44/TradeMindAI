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

    [switch]$DryRun,

    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

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
    "-SealedHoldoutPath `"$SealedHoldoutPath`" -HoldoutPrimaryMetric `"$HoldoutPrimaryMetric`" " +
    "-RiskProfile `"$RiskProfile`" -CommonFilesDir `"$CommonFilesDir`""
if ($DryRun) {
    $taskArguments += " -DryRun"
}

$logPath = Join-Path $repo "data\ser8_autonomous_execution\logs\autonomous_execution.log"

Write-Host "Installing scheduled task: $TaskName" -ForegroundColor Cyan
Write-Host "TaskName: $TaskName"
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
Write-Host "Each run is one bounded cycle (--once) guarded by both a Windows Global Mutex and an"
Write-Host "internal lock file -- overlapping runs are always skipped, never queued."
Write-Host "This task ONLY produces authorized execution requests; the ONLY component that ever"
Write-Host "sends a real order to the broker is the unified MT5 executor EA (TradeMind_Demo_Order_Executor_v1.mq5)."
Write-Host "Coexists with TradeMindAI-v1.32-ECN-LiveSignalRuntime (candidate generation) and"
Write-Host "TradeMindAI-SER8-MT5-Reconciliation (broker-truth reconciliation) -- three separate tasks,"
Write-Host "three separate responsibilities."
