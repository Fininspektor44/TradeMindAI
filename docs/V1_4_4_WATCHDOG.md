# TradeMind AI v1.4.4 Watchdog

## Purpose

The watchdog is a separate read-only health circuit for the 24/7 TradeMind research stack. It does not calculate entries, change research rules, publish client signals or send MetaTrader orders.

It checks:

- all 14 expected MT5 M5 volume streams;
- non-zero tick counts and `tick_copy_status=OK`;
- freshness of the canonical volume archive;
- freshness of FX observations, validation states and dashboard;
- Windows Scheduled Task state and last result for the volume collector and FX research cycle;
- the distribution of validation statuses.

## Outputs

Every run atomically refreshes:

```text
data/watchdog_v1_4_4/status.json
data/watchdog_v1_4_4/report.txt
data/watchdog_v1_4_4/index.html
```

When the overall state is `ERROR`, the PowerShell runner also creates:

```text
data/watchdog_v1_4_4/ALERT.txt
```

The desktop notification is emitted only when the state changes into `ERROR`. Repeated runs in the same failed state do not spam the user.

## Status rules

- `OK`: all expected streams, derived files and tasks are healthy.
- `WARN`: a task is currently running or data is stale during a weekend.
- `ERROR`: a stream/file/task is missing, disabled, stale on a trading weekday, malformed or has a failed task result.

The default freshness threshold is 20 minutes. It can be changed from the runner arguments without changing research logic.

## First run

From the project directory:

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\scripts\run_v144_watchdog.ps1" `
  -OpenReport
```

The report should show `Overall status: OK` when MT5, both existing tasks and all research files are healthy.

## Install the scheduled watchdog

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File ".\scripts\install_v144_watchdog_task.ps1" `
  -EveryMinutes 15 `
  -RunNow
```

Check it with:

```powershell
Get-ScheduledTaskInfo -TaskName "TradeMindAI-v1.4.4-Watchdog" |
  Format-List LastRunTime,LastTaskResult,NextRunTime
```

`LastTaskResult : 0` means the full pipeline was healthy. A non-zero result means the watchdog deliberately found an error; open `data/watchdog_v1_4_4/index.html` or `ALERT.txt` for the exact cause.

## Safety

The watchdog reads CSV, HTML, JSON and Windows task metadata. It only writes health reports. It contains no trade API calls and cannot open, modify or close positions.
