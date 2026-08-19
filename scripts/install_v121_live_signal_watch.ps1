param(
    [Parameter(Mandatory=$false)]
    [string]$TaskName = "TradeMindAI-v1.21-LiveSignalRuntime",

    [Parameter(Mandatory=$false)]
    [int]$IntervalMinutes = 1,

    [Parameter(Mandatory=$false)]
    [string]$Login = "37365712",

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    # Threaded straight through to run_v121_live_signal_watch.ps1's own
    # -RuntimeRoot (see that script for why). Set this explicitly when
    # installing a task for an account whose live workspace must not be
    # the shared default (e.g. an ECN market-data account's own
    # data\live_signal_runtime_ecN_<login> root) -- this is the ONE
    # installer for this Scheduled Task; there is no other supported way
    # to wire a non-default RuntimeRoot into it.
    [Parameter(Mandatory=$false)]
    [string]$RuntimeRoot = ".\data\live_signal_runtime_v1",

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
if ($ServerUTCOffsetHours -lt -14 -or $ServerUTCOffsetHours -gt 14) {
    throw "ServerUTCOffsetHours must be between -14 and 14"
}

$watchScript = Join-Path $PSScriptRoot "run_v121_live_signal_watch.ps1"
if (-not (Test-Path $watchScript)) {
    throw "Watch script not found: $watchScript"
}

$powershell = Join-Path $PSHOME "powershell.exe"

# Registered via the native ScheduledTasks module (New-ScheduledTaskAction /
# New-ScheduledTaskTrigger / New-ScheduledTaskPrincipal / Register-
# ScheduledTask) instead of schtasks.exe /Create /TR. schtasks.exe's /TR
# parameter is the ENTIRE command line collapsed into ONE string, and
# Windows caps it at 261 characters ("Значение параметра "/TR" не может
# содержать более 261 знаков"). A real repo checkout path plus the full
# watch-script path plus -Login/-ServerUTCOffsetHours/-RuntimeRoot routinely
# exceeds that limit, and task creation failed outright. Register-
# ScheduledTask stores Execute and Argument as SEPARATE Task Scheduler XML
# fields with no such combined-string cap, so this failure class cannot
# recur -- with no path shortened, no account/data hardcoded, and no second
# launcher/wrapper introduced.
$taskArguments = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$watchScript`" -Login `"$Login`" -ServerUTCOffsetHours $ServerUTCOffsetHours -RuntimeRoot `"$RuntimeRoot`""

Write-Host "Installing scheduled task: $TaskName" -ForegroundColor Cyan
Write-Host "Interval: every $IntervalMinutes minute(s)"
Write-Host "Account: $Login"
Write-Host "Broker server UTC offset: $ServerUTCOffsetHours"
Write-Host "Runtime root: $RuntimeRoot"

# Same working directory the wrapper itself already establishes via its own
# Split-Path/Set-Location -- set explicitly here too so the task's own
# starting directory (before the wrapper's own Set-Location runs) is never
# left to whatever default Task Scheduler would otherwise pick.
$action = New-ScheduledTaskAction -Execute $powershell -Argument $taskArguments -WorkingDirectory $repo

# schtasks.exe /SC MINUTE /MO $IntervalMinutes has no single native trigger
# type in the ScheduledTasks module; the standard equivalent is one "run
# once" start time combined with a repetition interval, which reproduces
# the same "every N minutes" cadence. -RepetitionDuration is deliberately
# OMITTED, not set to [TimeSpan]::MaxValue: Task Scheduler's own repetition
# Duration is an OPTIONAL XML field, and per its authoritative semantics,
# omitting it makes repetition continue indefinitely. Passing
# [TimeSpan]::MaxValue instead serializes to a literal
# "P99999999DT23H59M59S" ISO-8601 duration string, which Task Scheduler's
# own XML schema rejects outright ("XML-код задачи содержит значение в
# неправильном формате или за пределами допустимого диапазона") -- this is
# the real Windows registration failure this fix closes. No arbitrary
# 10-year/100-year duration is substituted either: indefinite-by-omission is
# the correct, authoritative way to express "forever" here, not a
# workaround with its own eventual expiry.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)

# Runs as the installing user at the highest available privileges (the same
# /RL HIGHEST semantics as before) using S4U logon: runs whether or not the
# user is interactively logged on, and -- exactly like the original
# schtasks.exe /Create call, which never passed /RU or /RP -- never
# collects or stores a password.
$currentUser = "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType S4U -RunLevel Highest

try {
    # -Force safely replaces any existing task of the same name in one
    # step -- the native equivalent of schtasks.exe /Create ... /F.
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
Write-Host "The runtime checks every $IntervalMinutes minute(s), but processes only a new closed M5 bar."
Write-Host "Status: $(Join-Path $repo (Join-Path $RuntimeRoot 'status.json'))"
Write-Host "Log: $(Join-Path $repo (Join-Path $RuntimeRoot 'logs\live_signal_watch.log'))"
Write-Host "Live candidates.jsonl: $(Join-Path $repo (Join-Path $RuntimeRoot 'candidates.jsonl'))"
Write-Host "Read-only. Orders OFF. Publication OFF." -ForegroundColor Green
