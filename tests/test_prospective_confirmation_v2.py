from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trademind.discovery.manifest import CriterionOperator, EvaluationCriterionV1
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.prospective_confirmation import (
    ProspectiveConfirmationError,
    ProspectiveConfirmationProtocolV2,
    ProspectiveOutcome,
    build_prospective_confirmation_protocol_v2,
    evaluate_prospective_snapshot_v2,
    load_prospective_confirmation_protocol_v2,
    persist_prospective_confirmation_protocol_v2,
    verify_prospective_confirmation_protocol_v2,
)
from trademind.signal_statistics_provenance import CodeProvenance, canonical_json_bytes

SYMBOL = ".US30CASH"
PATTERN = "BULLISH_FVG"
HORIZON = 12
MINIMUM_SAMPLE = 30
CUTOFF = "2026-07-31T23:45:00+00:00"
CREATED_AT = "2026-08-16T00:00:00+00:00"
CREATED_BY = "operator:test"

SIBLING_PROTOCOL_IDENTITY = f"sha256:{'d' * 64}"


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _protocol(
    *,
    minimum_sample: int = MINIMUM_SAMPLE,
    action: str | None = None,
    pattern: str | None = PATTERN,
    symbol: str = SYMBOL,
) -> ProspectiveConfirmationProtocolV2:
    return build_prospective_confirmation_protocol_v2(
        symbol=symbol,
        action=action,
        pattern=pattern,
        horizon=HORIZON,
        cutoff_time=CUTOFF,
        minimum_sample=minimum_sample,
        primary_metric="avg_net_atr",
        source_dataset_description="discovery-only mining batch: signals_real_20260731.csv",
        source_discovery_row_count=13538,
        sibling_protocol_semantic_identity=SIBLING_PROTOCOL_IDENTITY,
        code_provenance=_code_provenance(),
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


def _row(
    hours_after_cutoff: float,
    *,
    symbol: str = SYMBOL,
    action: str = "BUY",
    fvg_direction: str | None = "BULLISH",
    net_move: float = 2.0,
    atr: float = 1.0,
    outcome: str = "WIN",
) -> dict[str, str]:
    cutoff_dt = datetime.fromisoformat(CUTOFF)
    signal_time = (cutoff_dt + timedelta(hours=hours_after_cutoff)).isoformat()
    row = {
        "signal_time": signal_time,
        "symbol": symbol,
        "timeframe": "M5",
        "action": action,
        "atr": str(atr),
        f"net_move_{HORIZON}": str(net_move),
        f"outcome_{HORIZON}": outcome,
    }
    if fvg_direction is not None:
        row["fvg_direction"] = fvg_direction
    return row


def _rows_spaced(
    count: int, *, start_hours_after_cutoff: float = 1.0, step_hours: float = 24.0, **kwargs
) -> list[dict[str, str]]:
    return [_row(start_hours_after_cutoff + step_hours * index, **kwargs) for index in range(count)]


# ---------------------------------------------------------------------------
# Protocol construction / immutability / persistence
# ---------------------------------------------------------------------------


def test_protocol_builds_with_frozen_criteria() -> None:
    protocol = _protocol()
    assert protocol.symbol == SYMBOL
    assert protocol.action is None
    assert protocol.pattern == PATTERN
    assert protocol.horizon == HORIZON
    assert protocol.cutoff_time == CUTOFF
    assert protocol.minimum_sample == MINIMUM_SAMPLE
    assert protocol.sample_criterion == EvaluationCriterionV1(
        metric="trades", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=MINIMUM_SAMPLE
    )
    assert protocol.success_criterion == EvaluationCriterionV1(
        metric="avg_net_atr", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0
    )
    assert verify_prospective_confirmation_protocol_v2(protocol.canonical_bytes()) == protocol


def test_protocol_supports_pattern_and_action_together() -> None:
    protocol = _protocol(action="SELL", pattern="BEARISH_FVG", symbol="XAGUSD")
    assert protocol.action == "SELL"
    assert protocol.pattern == "BEARISH_FVG"
    assert verify_prospective_confirmation_protocol_v2(protocol.canonical_bytes()) == protocol


def test_protocol_is_frozen_dataclass() -> None:
    protocol = _protocol()
    with pytest.raises(dataclasses.FrozenInstanceError):
        protocol.minimum_sample = 5  # type: ignore[misc]


def test_action_and_pattern_both_none_is_rejected() -> None:
    with pytest.raises(ProspectiveConfirmationError, match="action, pattern, or both"):
        _protocol(action=None, pattern=None)


def test_invalid_pattern_is_rejected() -> None:
    with pytest.raises(ProspectiveConfirmationError, match="protocol.pattern"):
        _protocol(pattern="NOT_A_REAL_EVENT_LABEL")


def test_invalid_action_is_rejected() -> None:
    with pytest.raises(ProspectiveConfirmationError, match="protocol.action"):
        _protocol(action="HOLD", pattern=None)


def test_sample_criterion_must_match_minimum_sample() -> None:
    with pytest.raises(ProspectiveConfirmationError, match="sample_criterion"):
        ProspectiveConfirmationProtocolV2(
            symbol=SYMBOL,
            pattern=PATTERN,
            horizon=HORIZON,
            cutoff_time=CUTOFF,
            minimum_sample=30,
            primary_metric="avg_net_atr",
            sample_criterion=EvaluationCriterionV1(
                metric="trades", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=5
            ),
            success_criterion=EvaluationCriterionV1(
                metric="avg_net_atr", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0
            ),
            source_dataset_description="discovery-only mining batch",
            source_discovery_row_count=13538,
            sibling_protocol_semantic_identity=SIBLING_PROTOCOL_IDENTITY,
            code_provenance=_code_provenance(),
            diagnostics=_protocol().diagnostics,
        )


def test_hypothesis_candidate_mutation_via_tampered_wire_bytes_is_rejected() -> None:
    protocol = _protocol()
    payload = json.loads(protocol.canonical_bytes())
    payload["candidate"] = {**payload["candidate"], "symbol": ".SOMETHING_ELSE"}
    tampered = canonical_json_bytes(payload)
    with pytest.raises(ProspectiveConfirmationError, match="semantic identity"):
        verify_prospective_confirmation_protocol_v2(tampered)


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    protocol = _protocol()
    artifact = persist_prospective_confirmation_protocol_v2(protocol, artifact_store=store)
    loaded = load_prospective_confirmation_protocol_v2(artifact.hash_ref, artifact_store=store)
    assert loaded == protocol


# ---------------------------------------------------------------------------
# Pattern-based row filtering
# ---------------------------------------------------------------------------


def test_pattern_only_filter_excludes_non_matching_events() -> None:
    protocol = _protocol(action=None, pattern="BULLISH_FVG")
    rows = [
        _row(1.0, fvg_direction="BULLISH"),
        _row(2.0, fvg_direction="BEARISH"),
        _row(3.0, fvg_direction=None),
        _row(4.0, action="SELL", fvg_direction="BULLISH"),  # no action filter: still eligible.
    ]
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.eligible_rows_considered == 2


def test_pattern_and_action_filter_requires_both() -> None:
    protocol = _protocol(action="SELL", pattern="BEARISH_FVG", symbol="XAGUSD")
    rows = [
        _row(1.0, symbol="XAGUSD", action="SELL", fvg_direction="BEARISH"),
        _row(2.0, symbol="XAGUSD", action="BUY", fvg_direction="BEARISH"),  # wrong action.
        _row(3.0, symbol="XAGUSD", action="SELL", fvg_direction="BULLISH"),  # wrong pattern.
        _row(4.0, symbol="XAUUSD", action="SELL", fvg_direction="BEARISH"),  # wrong symbol.
    ]
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.eligible_rows_considered == 1


def test_action_only_filter_with_no_pattern_behaves_like_v1() -> None:
    protocol = _protocol(action="SELL", pattern=None, symbol="XAGUSD")
    rows = [
        _row(1.0, symbol="XAGUSD", action="SELL", fvg_direction=None),
        _row(2.0, symbol="XAGUSD", action="BUY", fvg_direction=None),
    ]
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.eligible_rows_considered == 1


def test_pre_cutoff_rows_are_rejected() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, start_hours_after_cutoff=-24 * 40)
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.eligible_rows_considered == 0
    assert result.outcome is ProspectiveOutcome.WAITING_FOR_DATA


# ---------------------------------------------------------------------------
# Sample-size gating / frozen criterion application
# ---------------------------------------------------------------------------


def test_below_minimum_sample_is_waiting_for_data() -> None:
    protocol = _protocol()
    rows = _rows_spaced(29, net_move=5.0)
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.completed_non_overlapping_trades == 29
    assert result.outcome is ProspectiveOutcome.WAITING_FOR_DATA


def test_at_or_above_minimum_sample_uses_frozen_criterion_pass() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, net_move=5.0)
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.completed_non_overlapping_trades == 30
    assert result.avg_net_atr == pytest.approx(5.0)
    assert result.outcome is ProspectiveOutcome.PASS


def test_at_or_above_minimum_sample_uses_frozen_criterion_fail() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, net_move=-5.0, outcome="LOSS")
    result = evaluate_prospective_snapshot_v2(protocol, rows)
    assert result.completed_non_overlapping_trades == 30
    assert result.avg_net_atr == pytest.approx(-5.0)
    assert result.outcome is ProspectiveOutcome.FAIL


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_evaluation_is_deterministic_across_repeated_calls() -> None:
    protocol = _protocol()
    rows = _rows_spaced(40, net_move=3.0)
    first = evaluate_prospective_snapshot_v2(protocol, rows)
    second = evaluate_prospective_snapshot_v2(protocol, list(rows))
    assert first == second


def test_evaluation_deterministic_after_protocol_reload(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    protocol = _protocol()
    artifact = persist_prospective_confirmation_protocol_v2(protocol, artifact_store=store)
    reloaded = load_prospective_confirmation_protocol_v2(artifact.hash_ref, artifact_store=store)

    rows = _rows_spaced(35, net_move=1.5)
    direct = evaluate_prospective_snapshot_v2(protocol, rows)
    via_reload = evaluate_prospective_snapshot_v2(reloaded, rows)
    assert direct == via_reload


# ---------------------------------------------------------------------------
# Row-level fail-closed checks
# ---------------------------------------------------------------------------


def test_naive_timestamp_row_fails_closed() -> None:
    protocol = _protocol()
    bad_row = _row(1.0)
    bad_row["signal_time"] = "2026-08-01T00:00:00"
    with pytest.raises(ProspectiveConfirmationError, match="timezone-aware"):
        evaluate_prospective_snapshot_v2(protocol, [bad_row])


def test_empty_timestamp_row_fails_closed() -> None:
    protocol = _protocol()
    bad_row = _row(1.0)
    bad_row["signal_time"] = ""
    with pytest.raises(ProspectiveConfirmationError, match="empty signal_time"):
        evaluate_prospective_snapshot_v2(protocol, [bad_row])
