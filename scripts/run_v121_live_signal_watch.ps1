param(
    # CANONICAL MT5 ACCOUNT ROLES: no default. The operator must state the
    # account explicitly, so this script can never silently fall back to an
    # obsolete account or its workspace, and cannot bind the MARKET-DATA-ONLY
    # account into a risk/account context by omission.
    [Parameter(Mandatory=$true)]
    [string]$Login,

    [Parameter(Mandatory=$false)]
    [int]$ServerUTCOffsetHours = 3,

    # Must match whatever -RuntimeRoot the Scheduled Task's own /TR command
    # line supplies. This is the ONE supported way to point this wrapper --
    # and, through it, run_v121_live_signal_runtime.ps1 /
    # trademind.live_signal_runtime_v122 -- at an account-specific live
    # workspace (for example an ECN market-data account's own
    # data\live_signal_runtime_ecN_<login> root) instead of the default
    # data\live_signal_runtime_v1. Any parameter the Scheduled Task passes
    # that this param() block does not declare causes PowerShell to reject
    # the invocation before the runtime ever starts (LastTaskResult=1) --
    # this is exactly the failure mode this fix closes.
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

# Logs live alongside whichever RuntimeRoot this invocation actually
# targets, so an account-specific run (e.g. the ECN root) never silently
# writes its log into the unrelated default v1 root's log directory.
$logDir = Join-Path $RuntimeRoot "logs"
$logPath = Join-Path $logDir "live_signal_watch.log"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

try {
    $started = Get-Date
    "[$($started.ToString('o'))] START account=$Login offset=$ServerUTCOffsetHours runtime_root=$RuntimeRoot" | Add-Content -Path $logPath -Encoding UTF8
    & (Join-Path $PSScriptRoot "run_v121_live_signal_runtime.ps1") `
        -Login $Login `
        -ServerUTCOffsetHours $ServerUTCOffsetHours `
        -RuntimeRoot $RuntimeRoot 2>&1 | Tee-Object -FilePath $logPath -Append
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
