from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trademind.discovery.manifest import CriterionOperator, EvaluationCriterionV1
from trademind.orchestrator.artifact_store import ArtifactStore
from trademind.prospective_confirmation import (
    ProspectiveConfirmationError,
    ProspectiveConfirmationProtocolV1,
    ProspectiveOutcome,
    build_prospective_confirmation_protocol_v1,
    evaluate_prospective_snapshot,
    load_prospective_confirmation_protocol_v1,
    persist_prospective_confirmation_protocol_v1,
    verify_prospective_confirmation_protocol_v1,
)
from trademind.signal_statistics_provenance import CodeProvenance, canonical_json_bytes

SYMBOL = ".USTECHCASH"
ACTION = "SELL"
HORIZON = 12
MINIMUM_SAMPLE = 30
CUTOFF = "2026-07-31T23:45:00+00:00"
CREATED_AT = "2026-08-16T00:00:00+00:00"
CREATED_BY = "operator:test"

SOURCE_HYPOTHESIS_ID = f"rpi-v1:sha256:{'7' * 64}:0"
SOURCE_MANIFEST_HASH = f"sha256:{'a' * 64}"
SOURCE_MANIFEST_REF = f"sha256:{'b' * 64}"
SOURCE_HOLDOUT_RESULT_REF = f"sha256:{'c' * 64}"


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="1.31.1",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _protocol(*, minimum_sample: int = MINIMUM_SAMPLE) -> ProspectiveConfirmationProtocolV1:
    return build_prospective_confirmation_protocol_v1(
        symbol=SYMBOL,
        action=ACTION,
        horizon=HORIZON,
        cutoff_time=CUTOFF,
        minimum_sample=minimum_sample,
        primary_metric="avg_net_atr",
        source_hypothesis_id=SOURCE_HYPOTHESIS_ID,
        source_manifest_semantic_hash=SOURCE_MANIFEST_HASH,
        source_manifest_artifact_hash_ref=SOURCE_MANIFEST_REF,
        source_final_holdout_result_artifact_hash_ref=SOURCE_HOLDOUT_RESULT_REF,
        code_provenance=_code_provenance(),
        created_at=CREATED_AT,
        created_by=CREATED_BY,
    )


def _row(hours_after_cutoff: float, *, symbol: str = SYMBOL, action: str = ACTION, net_move: float = 2.0, atr: float = 1.0, outcome: str = "WIN") -> dict[str, str]:
    cutoff_dt = datetime.fromisoformat(CUTOFF)
    signal_time = (cutoff_dt + timedelta(hours=hours_after_cutoff)).isoformat()
    return {
        "signal_time": signal_time,
        "symbol": symbol,
        "timeframe": "M5",
        "action": action,
        "atr": str(atr),
        f"net_move_{HORIZON}": str(net_move),
        f"outcome_{HORIZON}": outcome,
    }


def _rows_spaced(count: int, *, start_hours_after_cutoff: float = 1.0, step_hours: float = 24.0, **kwargs) -> list[dict[str, str]]:
    # Spaced well beyond the horizon's bar-duration so validate_rows' reused
    # non-overlapping-trade filtering never collapses these fixture rows.
    return [
        _row(start_hours_after_cutoff + step_hours * index, **kwargs)
        for index in range(count)
    ]


# ---------------------------------------------------------------------------
# Protocol construction / immutability / persistence
# ---------------------------------------------------------------------------


def test_protocol_builds_with_frozen_criteria() -> None:
    protocol = _protocol()
    assert protocol.symbol == SYMBOL
    assert protocol.action == ACTION
    assert protocol.horizon == HORIZON
    assert protocol.cutoff_time == CUTOFF
    assert protocol.minimum_sample == MINIMUM_SAMPLE
    assert protocol.sample_criterion == EvaluationCriterionV1(
        metric="trades", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=MINIMUM_SAMPLE
    )
    assert protocol.success_criterion == EvaluationCriterionV1(
        metric="avg_net_atr", operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0
    )
    assert verify_prospective_confirmation_protocol_v1(protocol.canonical_bytes()) == protocol


def test_protocol_is_frozen_dataclass() -> None:
    protocol = _protocol()
    with pytest.raises(dataclasses.FrozenInstanceError):
        protocol.minimum_sample = 5  # type: ignore[misc]


def test_internally_inconsistent_threshold_tamper_is_rejected() -> None:
    import json

    protocol = _protocol()
    payload = json.loads(protocol.canonical_bytes())
    payload["minimum_sample"] = 1  # tamper the threshold without recomputing the hash.
    tampered = canonical_json_bytes(payload)
    with pytest.raises(ProspectiveConfirmationError, match="sample_criterion"):
        verify_prospective_confirmation_protocol_v1(tampered)


def test_internally_consistent_threshold_tamper_is_still_rejected_by_hash() -> None:
    # Tamper minimum_sample AND its criterion together (self-consistent), but
    # leave the old semantic_identity in place: the internal consistency
    # check alone cannot catch this, only the recomputed hash comparison can.
    import json

    protocol = _protocol()
    payload = json.loads(protocol.canonical_bytes())
    payload["minimum_sample"] = 1
    payload["sample_criterion"] = {**payload["sample_criterion"], "threshold": 1}
    tampered = canonical_json_bytes(payload)
    with pytest.raises(ProspectiveConfirmationError, match="semantic identity"):
        verify_prospective_confirmation_protocol_v1(tampered)


def test_hypothesis_candidate_mutation_via_tampered_wire_bytes_is_rejected() -> None:
    import json

    protocol = _protocol()
    payload = json.loads(protocol.canonical_bytes())
    payload["candidate"] = {**payload["candidate"], "symbol": ".SOMETHING_ELSE"}
    tampered = canonical_json_bytes(payload)
    with pytest.raises(ProspectiveConfirmationError, match="semantic identity"):
        verify_prospective_confirmation_protocol_v1(tampered)


def test_sample_criterion_must_match_minimum_sample() -> None:
    with pytest.raises(ProspectiveConfirmationError, match="sample_criterion"):
        ProspectiveConfirmationProtocolV1(
            symbol=SYMBOL,
            action=ACTION,
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
            source_hypothesis_id=SOURCE_HYPOTHESIS_ID,
            source_manifest_semantic_hash=SOURCE_MANIFEST_HASH,
            source_manifest_artifact_hash_ref=SOURCE_MANIFEST_REF,
            source_final_holdout_result_artifact_hash_ref=SOURCE_HOLDOUT_RESULT_REF,
            code_provenance=_code_provenance(),
            diagnostics=_protocol().diagnostics,
        )


def test_persist_load_verified_cas_round_trip(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    protocol = _protocol()
    artifact = persist_prospective_confirmation_protocol_v1(protocol, artifact_store=store)
    loaded = load_prospective_confirmation_protocol_v1(artifact.hash_ref, artifact_store=store)
    assert loaded == protocol


def test_deterministic_reload_after_restart(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    protocol = _protocol()
    artifact = persist_prospective_confirmation_protocol_v1(protocol, artifact_store=store)

    restarted_store = ArtifactStore(tmp_path / "artifacts")
    reloaded = load_prospective_confirmation_protocol_v1(artifact.hash_ref, artifact_store=restarted_store)
    assert reloaded == protocol
    assert reloaded.protocol_semantic_identity == protocol.protocol_semantic_identity


# ---------------------------------------------------------------------------
# Cutoff enforcement
# ---------------------------------------------------------------------------


def test_pre_cutoff_rows_are_rejected() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, start_hours_after_cutoff=-24 * 40)  # all strictly before cutoff.
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.eligible_rows_considered == 0
    assert result.outcome is ProspectiveOutcome.WAITING_FOR_DATA


def test_cutoff_row_itself_is_rejected() -> None:
    protocol = _protocol()
    exact_cutoff_row = _row(0.0)  # signal_time == cutoff exactly.
    assert exact_cutoff_row["signal_time"] == CUTOFF
    result = evaluate_prospective_snapshot(protocol, [exact_cutoff_row])
    assert result.eligible_rows_considered == 0


def test_post_cutoff_rows_are_accepted() -> None:
    protocol = _protocol()
    rows = _rows_spaced(5, start_hours_after_cutoff=1.0)
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.eligible_rows_considered == 5


def test_mixed_pre_and_post_cutoff_only_counts_post_cutoff() -> None:
    protocol = _protocol()
    pre = _rows_spaced(10, start_hours_after_cutoff=-24 * 40)
    post = _rows_spaced(5, start_hours_after_cutoff=1.0)
    result = evaluate_prospective_snapshot(protocol, pre + post)
    assert result.eligible_rows_considered == 5


def test_last_eligible_signal_time_is_the_latest_eligible_row() -> None:
    protocol = _protocol()
    rows = _rows_spaced(5, start_hours_after_cutoff=1.0, step_hours=24.0)
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.last_eligible_signal_time == rows[-1]["signal_time"]


def test_last_eligible_signal_time_is_none_when_nothing_is_eligible() -> None:
    protocol = _protocol()
    rows = _rows_spaced(3, start_hours_after_cutoff=-24 * 40)
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.last_eligible_signal_time is None


def test_non_matching_symbol_or_action_excluded() -> None:
    protocol = _protocol()
    rows = [
        _row(1.0, symbol="XAUUSD"),
        _row(2.0, action="BUY"),
        _row(3.0, symbol=SYMBOL, action=ACTION),
    ]
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.eligible_rows_considered == 1


# ---------------------------------------------------------------------------
# Sample-size gating / frozen criterion application
# ---------------------------------------------------------------------------


def test_below_minimum_sample_is_waiting_for_data() -> None:
    protocol = _protocol()
    rows = _rows_spaced(29, net_move=5.0)
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.completed_non_overlapping_trades == 29
    assert result.outcome is ProspectiveOutcome.WAITING_FOR_DATA


def test_at_or_above_minimum_sample_uses_frozen_criterion_pass() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, net_move=5.0)  # all WIN, positive avg_net_atr.
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.completed_non_overlapping_trades == 30
    assert result.avg_net_atr == pytest.approx(5.0)
    assert result.outcome is ProspectiveOutcome.PASS


def test_at_or_above_minimum_sample_uses_frozen_criterion_fail() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, net_move=-5.0, outcome="LOSS")
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.completed_non_overlapping_trades == 30
    assert result.avg_net_atr == pytest.approx(-5.0)
    assert result.outcome is ProspectiveOutcome.FAIL


def test_exactly_at_threshold_boundary_passes() -> None:
    protocol = _protocol()
    rows = _rows_spaced(30, net_move=0.0, outcome="FLAT")  # avg_net_atr exactly 0.0
    result = evaluate_prospective_snapshot(protocol, rows)
    assert result.avg_net_atr == pytest.approx(0.0)
    assert result.outcome is ProspectiveOutcome.PASS  # >= is inclusive.


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_evaluation_is_deterministic_across_repeated_calls() -> None:
    protocol = _protocol()
    rows = _rows_spaced(40, net_move=3.0)
    first = evaluate_prospective_snapshot(protocol, rows)
    second = evaluate_prospective_snapshot(protocol, list(rows))
    assert first == second


def test_evaluation_deterministic_after_protocol_reload(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    protocol = _protocol()
    artifact = persist_prospective_confirmation_protocol_v1(protocol, artifact_store=store)
    reloaded = load_prospective_confirmation_protocol_v1(artifact.hash_ref, artifact_store=store)

    rows = _rows_spaced(35, net_move=1.5)
    direct = evaluate_prospective_snapshot(protocol, rows)
    via_reload = evaluate_prospective_snapshot(reloaded, rows)
    assert direct == via_reload


# ---------------------------------------------------------------------------
# Row-level fail-closed checks
# ---------------------------------------------------------------------------


def test_naive_timestamp_row_fails_closed() -> None:
    protocol = _protocol()
    bad_row = _row(1.0)
    bad_row["signal_time"] = "2026-08-01T00:00:00"  # no timezone.
    with pytest.raises(ProspectiveConfirmationError, match="timezone-aware"):
        evaluate_prospective_snapshot(protocol, [bad_row])


def test_empty_timestamp_row_fails_closed() -> None:
    protocol = _protocol()
    bad_row = _row(1.0)
    bad_row["signal_time"] = ""
    with pytest.raises(ProspectiveConfirmationError, match="empty signal_time"):
        evaluate_prospective_snapshot(protocol, [bad_row])
