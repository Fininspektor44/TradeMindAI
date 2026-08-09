from datetime import datetime, timedelta, timezone

from trademind.discovery.split_engine import chronological_split, public_rows


def test_split_is_chronological_and_public_rows_exclude_holdout():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    timestamps = [start + timedelta(hours=i) for i in range(10)]
    rows = list(range(10))
    plan = chronological_split(timestamps)

    assert plan.discovery_count == 6
    assert plan.validation_count == 2
    assert plan.holdout_count == 2
    assert public_rows(rows, plan) == list(range(8))
    assert plan.holdout_start == timestamps[8].isoformat()


def test_unsorted_or_duplicate_timestamps_are_rejected():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for values in ([t, t], [t + timedelta(hours=1), t]):
        try:
            chronological_split(values)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe chronology must be rejected")
