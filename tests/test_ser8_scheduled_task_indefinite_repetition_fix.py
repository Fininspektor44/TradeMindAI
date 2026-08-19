"""Tests for SER8 SCHEDULED TASK INDEFINITE REPETITION FIX V1.

Real Windows failure this task fixes:

    XML-код задачи содержит значение в неправильном формате или за
    пределами допустимого диапазона.
    (8,42):Duration:P99999999DT23H59M59S

scripts/install_v121_live_signal_watch.ps1's New-ScheduledTaskTrigger call
previously passed ``-RepetitionDuration ([TimeSpan]::MaxValue)`` to make the
every-N-minutes repetition run forever. [TimeSpan]::MaxValue serializes to
the ISO-8601 duration "P99999999DT23H59M59S", which Task Scheduler's own
XML schema rejects outright. Task Scheduler's repetition Duration field is
OPTIONAL, and per its own authoritative semantics, omitting it makes
repetition continue indefinitely -- so this task's fix is to stop passing
-RepetitionDuration at all, not to substitute a different (arbitrary,
eventually-expiring) duration value.

This file does not import test helpers from sibling test files (consistent
with this session's own established convention for new SER8 test modules).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_v121_live_signal_watch.ps1"
WATCH_SCRIPT = REPO_ROOT / "scripts" / "run_v121_live_signal_watch.ps1"


def _executable_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.strip().startswith("#")]


def _trigger_block(text: str) -> str:
    start = text.index("$trigger = New-ScheduledTaskTrigger")
    # The statement is a backtick-continued multi-line PowerShell command;
    # capture through the first line that does NOT end in a line-
    # continuation backtick.
    lines = text[start:].splitlines()
    block_lines: list[str] = []
    for line in lines:
        block_lines.append(line)
        if not line.rstrip().endswith("`"):
            break
    return "\n".join(block_lines)


# ---------------------------------------------------------------------------
# 1: [TimeSpan]::MaxValue is never used.
# ---------------------------------------------------------------------------


def test_timespan_maxvalue_never_used_anywhere_in_installer() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    for line in _executable_lines(text):
        assert "[TimeSpan]::MaxValue" not in line, f"TimeSpan::MaxValue still used: {line!r}"
        assert "MaxValue" not in line, f"MaxValue still referenced: {line!r}"


# ---------------------------------------------------------------------------
# 2: no explicit -RepetitionDuration of ANY kind (not MaxValue, not an
# arbitrary long-lived substitute like 10/100 years).
# ---------------------------------------------------------------------------


def test_no_explicit_repetition_duration_passed_at_all() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    trigger_block = _trigger_block(text)
    assert "-RepetitionDuration" not in trigger_block
    for line in _executable_lines(text):
        assert "-RepetitionDuration" not in line, f"-RepetitionDuration still passed: {line!r}"


def test_no_arbitrary_multi_year_duration_substituted() -> None:
    # Requirement 2: do not replace MaxValue with an arbitrary 10-year/
    # 100-year duration either -- indefinite-by-omission is the correct
    # fix, not a longer-but-still-finite workaround.
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"New-TimeSpan\s+-Days\s+\d{3,}|New-TimeSpan\s+-\w+\s+\(?\d+\s*\*\s*365|\[timespan\]::FromDays\(\s*\d{3,}\s*\)",
        re.I,
    )
    assert not forbidden.search(text)


# ---------------------------------------------------------------------------
# 3: -RepetitionInterval remains, so the every-N-minutes cadence itself is
# preserved.
# ---------------------------------------------------------------------------


def test_repetition_interval_still_present_and_unchanged() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert "-RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes)" in text
    trigger_block = _trigger_block(text)
    assert "-RepetitionInterval" in trigger_block


# ---------------------------------------------------------------------------
# 4: the trigger is indefinite BY OMISSION -- New-ScheduledTaskTrigger is
# still called with -Once/-At/-RepetitionInterval and nothing else that
# would bound its lifetime.
# ---------------------------------------------------------------------------


def test_trigger_remains_indefinite_by_omission_of_duration() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    trigger_block = _trigger_block(text)
    assert "New-ScheduledTaskTrigger -Once -At (Get-Date)" in trigger_block
    assert "-RepetitionInterval" in trigger_block
    assert "-RepetitionDuration" not in trigger_block
    # No other bounding parameter (-EndBoundary, -Until, etc.) was
    # introduced as a substitute either.
    for forbidden_param in ("-EndBoundary", "-Until", "-ExpirationDate"):
        assert forbidden_param not in trigger_block


# ---------------------------------------------------------------------------
# 5: no schtasks /TR regression -- the prior task's fix stays intact.
# ---------------------------------------------------------------------------


def test_no_schtasks_tr_regression() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    for line in _executable_lines(text):
        assert "schtasks.exe" not in line
        assert "/TR" not in line
    assert "Register-ScheduledTask" in text
    assert "New-ScheduledTaskAction" in text


# ---------------------------------------------------------------------------
# 6: single scheduled runtime entrypoint preserved.
# ---------------------------------------------------------------------------


def test_single_installer_for_ser8_live_candidate_task_preserved() -> None:
    candidates = [
        path
        for path in (REPO_ROOT / "scripts").glob("*.ps1")
        if "run_v121_live_signal_watch.ps1" in path.read_text(encoding="utf-8")
        and "Register-ScheduledTask" in path.read_text(encoding="utf-8")
    ]
    assert candidates == [INSTALL_SCRIPT]


def test_single_watch_wrapper_preserved() -> None:
    watch_wrappers = [
        path
        for path in (REPO_ROOT / "scripts").glob("*.ps1")
        if "run_v121_live_signal_runtime.ps1" in path.read_text(encoding="utf-8")
        and "New-Object System.Threading.Mutex" in path.read_text(encoding="utf-8")
    ]
    assert watch_wrappers == [WATCH_SCRIPT]


# ---------------------------------------------------------------------------
# Everything else this task's requirement 3 lists as "must preserve".
# ---------------------------------------------------------------------------


def test_everything_else_required_to_be_preserved_is_still_present() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    assert '[string]$TaskName = "TradeMindAI-v1.21-LiveSignalRuntime"' in text
    assert "[int]$IntervalMinutes = 1" in text
    assert "Register-ScheduledTask -TaskName $TaskName" in text
    assert "-WorkingDirectory $repo" in text
    assert "-LogonType S4U" in text
    assert "-RunLevel Highest" in text
    assert "Start-ScheduledTask -TaskName $TaskName" in text
    assert '[string]$RuntimeRoot = ".\\data\\live_signal_runtime_v1"' in text
    assert "-RuntimeRoot `\"$RuntimeRoot`\"" in text
    assert "-Login `\"$Login`\"" in text
    assert "-ServerUTCOffsetHours $ServerUTCOffsetHours" in text
    assert "-Force" in text
    assert "Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false" in text


def test_no_broker_order_or_risk_machinery_introduced() -> None:
    text = INSTALL_SCRIPT.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"order_send|OrderSend\(|trade\.Buy\(|trade\.Sell\(|CTrade|MetaTrader5\.|risk_manager|evaluate_risk",
        re.I,
    )
    assert not forbidden.search(text)
