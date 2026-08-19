param(
    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-SER8-MT5-Reconciliation",

    [Parameter(Mandatory=$false)]
    [int]$IntervalMinutes = 1,

    [Parameter(Mandatory=$false)]
    [string]$Db = ".\data\ser8_registry.db",

    [Parameter(Mandatory=$false)]
    [string]$Account = "67206924",

    [Parameter(Mandatory=$false)]
    [string]$Mt5ExportDir = ".\data\mt5",

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

$watchScript = Join-Path $PSScriptRoot "run_ser8_mt5_reconciliation.ps1"
if (-not (Test-Path $watchScript)) {
    throw "Reconciliation wrapper script not found: $watchScript"
}

$powershell = Join-Path $PSHOME "powershell.exe"

# Registered via the native ScheduledTasks module (New-ScheduledTaskAction /
# New-ScheduledTaskTrigger / New-ScheduledTaskPrincipal / Register-
# ScheduledTask), NEVER schtasks.exe /Create /TR -- and the trigger below
# deliberately OMITS -RepetitionDuration rather than setting it to
# [TimeSpan]::MaxValue. Both are exactly the two real Windows registration
# failures this repository already hit and fixed for
# install_v121_live_signal_watch.ps1 (the /TR 261-character limit, and the
# "XML-код задачи содержит значение в неправильном формате" duration-
# serialization error) -- this installer reuses that SAME proven pattern
# from the start rather than risking either bug again.
$taskArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchScript`" -Db `"$Db`" -Account `"$Account`" -Mt5ExportDir `"$Mt5ExportDir`""
if ($DryRun) {
    $taskArguments += " -DryRun"
}

Write-Host "Installing scheduled task: $TaskName" -ForegroundColor Cyan
Write-Host "Interval: every $IntervalMinutes minute(s)"
Write-Host "Account: $Account"
Write-Host "Registry db: $Db"
Write-Host "MT5 export dir: $Mt5ExportDir"
Write-Host "Dry run: $($DryRun.IsPresent)"

$action = New-ScheduledTaskAction -Execute $powershell -Argument $taskArguments -WorkingDirectory $repo

# Indefinite-by-omission (see the comment above and
# install_v121_live_signal_watch.ps1's own audit note): no
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
Write-Host "Reconciliation runs every $IntervalMinutes minute(s); each run is one bounded cycle (--once) guarded"
Write-Host "by both a Windows Global Mutex and an internal lock file -- overlapping runs are always skipped, never queued."
Write-Host "Log: $(Join-Path $repo 'data\ser8_reconciliation\logs\reconcile.log')"
Write-Host "Read-only reconciliation. No broker order is ever sent by this task." -ForegroundColor Green
