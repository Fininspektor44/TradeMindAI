from __future__ import annotations

import copy
import hashlib
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trademind.orchestrator.artifact_store import (
    ArtifactIntegrityError,
    ArtifactMediaTypeError,
    ArtifactStore,
)
from trademind.signal_statistics_provenance import (
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
    ProvenanceError,
    canonical_json_bytes,
)
from trademind.signal_statistics_report import (
    REPORT_SCHEMA_VERSION,
    REPORT_V2_MEDIA_TYPE,
    REPORT_V2_SCHEMA_VERSION,
    build_report,
    build_report_v2,
    load_report_v2,
    persist_report_v2,
    verify_report_v2,
)

_SOURCE_HASH = f"sha256:{'3' * 64}"
_POLICY_HASH = f"sha256:{'2' * 64}"


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="report-v2-test",
        git_commit="1" * 40,
        revision_source="git_worktree",
    )


def _candidate(
    *,
    feature: str = "BULLISH_BOS",
    trades: int = 4,
) -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol="XAUUSD",
            timeframe="M5",
            feature=feature,
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": trades, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _report_v2(**changes: object):
    values: dict[str, object] = {
        "candidates": (_candidate(),),
        "source_snapshot_hash_ref": _SOURCE_HASH,
        "source_schema_version": "1.1",
        "code_provenance": _code_provenance(),
        "journal_rows": 4,
        "generated_at": "2026-08-13T12:00:00+00:00",
        "source_path": "/diagnostic/source/signals.csv",
    }
    values.update(changes)
    return build_report_v2(**values)


def _rows(values: list[float], horizon: int = 3) -> list[dict[str, str]]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows: list[dict[str, str]] = []
    for index, value in enumerate(values):
        rows.append(
            {
                "schema_version": "1.1",
                "signal_time": (start + timedelta(minutes=15 * index)).isoformat(),
                "symbol": "XAUUSD",
                "timeframe": "M5",
                "action": "BUY",
                "internal_break": "BULLISH_BOS",
                f"outcome_{horizon}": "WIN" if value > 0 else "LOSS" if value < 0 else "FLAT",
                f"progress_atr_{horizon}": str(value),
                "atr": "1.0",
            }
        )
    return rows


def test_report_is_json_ready_and_read_only() -> None:
    generated_at = datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc)
    report = build_report(
        _rows([0.5, -0.1] * 30),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
        generated_at=generated_at,
    )

    assert report["schema_version"] == REPORT_SCHEMA_VERSION
    assert report["read_only"] is True
    assert report["orders_enabled"] is False
    assert report["symbols"] == ["XAUUSD"]
    assert report["horizons"] == [3]
    assert report["journal_rows"] == 60
    json.dumps(report)


def test_report_exposes_agent_useful_candidate_metrics_and_reasons() -> None:
    report = build_report(
        _rows([0.5, -0.1] * 30),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
    )

    candidates = [item for item in report["patterns"] if item["status"] == "RESEARCH_CANDIDATE"]

    assert candidates
    candidate = candidates[0]
    assert candidate["symbol"] == "XAUUSD"
    assert candidate["horizon"] == 3
    assert candidate["trades"] == 60
    assert candidate["profit_factor_atr"] > 1.0
    assert candidate["avg_net_atr"] > 0.0
    assert candidate["early"]["avg_net_atr"] > 0.0
    assert candidate["late"]["avg_net_atr"] > 0.0
    assert len(candidate["mean_ci95"]) == 2
    assert "research threshold is 300 trades" in candidate["reasons"]


def test_report_exposes_validated_patterns() -> None:
    report = build_report(
        _rows([0.4, -0.1] * 150),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
    )

    assert report["status_counts"].get("VALIDATED", 0) >= 1
    validated = [item for item in report["patterns"] if item["status"] == "VALIDATED"]
    assert validated
    assert validated[0]["mean_ci95"][0] > 0.0


def test_report_v2_build_is_strict_deterministic_and_machine_readable() -> None:
    candidate_a = _candidate(feature="BULLISH_BOS")
    candidate_b = _candidate(feature="BEARISH_BOS")

    first = _report_v2(candidates=(candidate_a, candidate_b))
    second = _report_v2(candidates=(candidate_b, candidate_a))

    assert first.schema_version == REPORT_V2_SCHEMA_VERSION
    assert first.to_payload()["read_only"] is True
    assert first.to_payload()["orders_enabled"] is False
    assert first.report_semantic_hash == second.report_semantic_hash
    assert first.canonical_bytes() == second.canonical_bytes()
    assert verify_report_v2(first.canonical_bytes()) == first


def test_report_v2_diagnostics_do_not_change_semantic_identity() -> None:
    first = _report_v2(
        generated_at="2026-08-13T12:00:00+00:00",
        source_path="/host-a/private/signals.csv",
    )
    second = _report_v2(
        generated_at="2027-01-01T00:00:00+00:00",
        source_path="C:\\host-b\\signals.csv",
    )

    assert first.report_semantic_hash == second.report_semantic_hash
    assert first.canonical_bytes() != second.canonical_bytes()
    assert verify_report_v2(first.canonical_bytes()).report_semantic_hash == (
        verify_report_v2(second.canonical_bytes()).report_semantic_hash
    )
    assert (
        hashlib.sha256(first.canonical_bytes()).digest()
        != hashlib.sha256(second.canonical_bytes()).digest()
    )


def test_report_v2_accepts_only_canonical_utc_generated_at() -> None:
    report = _report_v2(generated_at="2026-08-13T12:00:00+00:00")

    assert report.generated_at == "2026-08-13T12:00:00+00:00"


@pytest.mark.parametrize(
    "generated_at",
    [
        "not-a-timestamp",
        "2026-02-30T12:00:00+00:00",
        "2026-08-13T12:00:00",
        "2026-08-13T15:00:00+03:00",
        "2026-08-13T12:00:00Z",
        " 2026-08-13T12:00:00+00:00",
        "2026-08-13T12:00:00+00:00 ",
        "2026-08-13T12:00:00+00:00\n",
        None,
        True,
        1,
    ],
)
def test_report_v2_rejects_invalid_generated_at(generated_at: object) -> None:
    with pytest.raises(ProvenanceError, match="generated_at"):
        _report_v2(generated_at=generated_at)


def test_report_v2_wire_uses_the_same_generated_at_contract() -> None:
    payload = copy.deepcopy(_report_v2().to_payload())
    payload["diagnostics"]["generated_at"] = "2026-02-30T12:00:00+00:00"

    with pytest.raises(ProvenanceError, match="generated_at"):
        verify_report_v2(canonical_json_bytes(payload))


def test_report_v2_semantic_statistics_change_identity() -> None:
    first = _report_v2(candidates=(_candidate(trades=4),))
    changed = _report_v2(candidates=(_candidate(trades=5),), journal_rows=5)

    assert first.report_semantic_hash != changed.report_semantic_hash


def test_report_v2_source_schema_and_code_claim_change_semantic_identity() -> None:
    original = _report_v2()
    source_changed = _report_v2(source_schema_version="1.2")
    code_changed = _report_v2(
        code_provenance=CodeProvenance(
            producer_name="trademind",
            producer_version="report-v2-test-2",
            git_commit="1" * 40,
            revision_source="git_worktree",
        )
    )

    assert original.report_semantic_hash != source_changed.report_semantic_hash
    assert original.report_semantic_hash != code_changed.report_semantic_hash


def test_report_v2_persist_is_idempotent_and_restart_verifies_exact_bytes(
    tmp_path: Path,
) -> None:
    root = tmp_path / "artifacts"
    report = _report_v2()
    store = ArtifactStore(root)

    first = persist_report_v2(report, artifact_store=store)
    second = persist_report_v2(report, artifact_store=store)
    restarted = ArtifactStore(root)
    resolved = restarted.resolve_verified(
        first.hash_ref,
        expected_media_type=REPORT_V2_MEDIA_TYPE,
    )
    exact_bytes = restarted.read_verified(
        first.hash_ref,
        expected_media_type=REPORT_V2_MEDIA_TYPE,
    )

    assert first == second
    assert first.hash_ref == f"sha256:{hashlib.sha256(report.canonical_bytes()).hexdigest()}"
    assert resolved.hash_ref == first.hash_ref
    assert exact_bytes == report.canonical_bytes()
    assert load_report_v2(first.hash_ref, artifact_store=restarted) == report


def test_report_v2_does_not_reopen_or_rehash_mutable_source_path(tmp_path: Path) -> None:
    source = tmp_path / "signals.csv"
    source.write_bytes(b"captured source")
    source_store = ArtifactStore(tmp_path / "source-artifacts")
    snapshot = source_store.import_snapshot(
        io.BytesIO(source.read_bytes()),
        media_type="text/csv",
    )
    report = _report_v2(
        source_snapshot_hash_ref=snapshot.hash_ref,
        source_path=str(source.resolve()),
    )
    report_store = ArtifactStore(tmp_path / "report-artifacts")
    artifact = persist_report_v2(report, artifact_store=report_store)

    source.write_bytes(b"mutated after snapshot")
    source.unlink()

    assert load_report_v2(artifact.hash_ref, artifact_store=report_store) == report


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.__setitem__("journal_rows", 5), "semantic hash mismatch"),
        (
            lambda payload: payload.__setitem__(
                "report_semantic_hash",
                f"sha256:{'0' * 64}",
            ),
            "semantic hash mismatch",
        ),
        (
            lambda payload: payload["candidates"][0].__setitem__(
                "candidate_id",
                "ssc-v2-" + "0" * 64,
            ),
            "candidate definition identity mismatch",
        ),
        (
            lambda payload: payload["candidates"][0].__setitem__(
                "candidate_content_hash",
                f"sha256:{'0' * 64}",
            ),
            "candidate content identity mismatch",
        ),
        (
            lambda payload: payload["source"].__setitem__(
                "snapshot_hash_ref",
                f"sha256:{'4' * 64}",
            ),
            "semantic hash mismatch",
        ),
    ],
)
def test_report_v2_rejects_semantic_and_upstream_identity_tamper(
    mutation: object,
    message: str,
) -> None:
    payload = copy.deepcopy(_report_v2().to_payload())
    mutation(payload)

    with pytest.raises(ProvenanceError, match=message):
        verify_report_v2(canonical_json_bytes(payload))


def test_report_v2_rejects_candidate_semantic_field_tamper() -> None:
    payload = copy.deepcopy(_report_v2().to_payload())
    payload["candidates"][0]["content"]["metrics"]["trades"] = 999

    with pytest.raises(ProvenanceError, match="candidate content identity mismatch"):
        verify_report_v2(canonical_json_bytes(payload))


def test_report_v2_rejects_malformed_upstream_claims() -> None:
    malformed_source = copy.deepcopy(_report_v2().to_payload())
    malformed_source["source"]["snapshot_hash_ref"] = "sha256:" + "A" * 64
    malformed_code = copy.deepcopy(_report_v2().to_payload())
    malformed_code["code_provenance"]["git_commit"] = "not-a-commit"

    with pytest.raises(ProvenanceError, match="sha256"):
        verify_report_v2(canonical_json_bytes(malformed_source))
    with pytest.raises(ProvenanceError, match="git_commit"):
        verify_report_v2(canonical_json_bytes(malformed_code))


def test_report_v2_rejects_artifact_tamper_and_wrong_media_type(tmp_path: Path) -> None:
    report = _report_v2()
    store = ArtifactStore(tmp_path / "tampered")
    artifact = persist_report_v2(report, artifact_store=store)
    artifact_path = Path(artifact.path)
    artifact_path.write_bytes(report.canonical_bytes()[:-1] + b" ")

    with pytest.raises(ArtifactIntegrityError):
        load_report_v2(artifact.hash_ref, artifact_store=store)

    wrong_store = ArtifactStore(tmp_path / "wrong-media")
    wrong = wrong_store.import_snapshot(
        io.BytesIO(report.canonical_bytes()),
        media_type="application/json",
    )
    with pytest.raises(ArtifactMediaTypeError):
        load_report_v2(wrong.hash_ref, artifact_store=wrong_store)


def test_report_v2_persisted_diagnostic_tamper_fails_old_cas_identity(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "diagnostic-tamper")
    report = _report_v2()
    artifact = persist_report_v2(report, artifact_store=store)
    tampered = copy.deepcopy(report.to_payload())
    tampered["diagnostics"]["source_path"] = "/changed/source.csv"
    tampered_bytes = canonical_json_bytes(tampered)

    assert verify_report_v2(tampered_bytes).report_semantic_hash == report.report_semantic_hash
    assert (
        hashlib.sha256(tampered_bytes).digest() != hashlib.sha256(report.canonical_bytes()).digest()
    )
    Path(artifact.path).write_bytes(tampered_bytes)
    with pytest.raises(ArtifactIntegrityError):
        load_report_v2(artifact.hash_ref, artifact_store=store)


def test_report_v2_rejects_semantic_tamper_even_with_consistent_new_artifact_hash(
    tmp_path: Path,
) -> None:
    report = _report_v2()
    tampered = copy.deepcopy(report.to_payload())
    tampered["candidates"][0]["content"]["metrics"]["trades"] = 999
    tampered_bytes = canonical_json_bytes(tampered)
    recomputed_artifact_hash = f"sha256:{hashlib.sha256(tampered_bytes).hexdigest()}"
    store = ArtifactStore(tmp_path / "semantic-tamper")
    artifact = store.import_snapshot(
        io.BytesIO(tampered_bytes),
        media_type=REPORT_V2_MEDIA_TYPE,
    )

    assert (
        recomputed_artifact_hash != f"sha256:{hashlib.sha256(report.canonical_bytes()).hexdigest()}"
    )
    assert artifact.hash_ref == recomputed_artifact_hash
    with pytest.raises(ProvenanceError, match="candidate content identity mismatch"):
        load_report_v2(artifact.hash_ref, artifact_store=store)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("schema_version", "signal-statistics-report-v99"),
        lambda payload: payload.__setitem__("unknown", True),
        lambda payload: payload.pop("source"),
    ],
)
def test_report_v2_rejects_unknown_version_fields_and_missing_fields(mutation: object) -> None:
    payload = copy.deepcopy(_report_v2().to_payload())
    mutation(payload)

    with pytest.raises(ProvenanceError):
        verify_report_v2(canonical_json_bytes(payload))


def test_report_v2_wire_rejects_duplicate_fields() -> None:
    encoded = _report_v2().canonical_bytes()
    duplicate = encoded.replace(
        b'{"candidates":',
        b'{"schema_version":"signal-statistics-report-v2","candidates":',
        1,
    )

    with pytest.raises(ProvenanceError, match="duplicate JSON key"):
        verify_report_v2(duplicate)


@pytest.mark.parametrize("extension", [b"NaN", b"Infinity", b"-Infinity"])
def test_report_v2_wire_rejects_non_finite_numbers(extension: bytes) -> None:
    encoded = _report_v2().canonical_bytes()
    malformed = encoded.replace(b'"journal_rows":4', b'"journal_rows":' + extension, 1)

    with pytest.raises(ProvenanceError, match="non-standard JSON constant"):
        verify_report_v2(malformed)


@pytest.mark.parametrize("value", [True, "4"])
def test_report_v2_does_not_coerce_bool_or_string_to_integer(value: object) -> None:
    payload = copy.deepcopy(_report_v2().to_payload())
    payload["journal_rows"] = value

    with pytest.raises(ProvenanceError, match="journal_rows"):
        verify_report_v2(canonical_json_bytes(payload))


def test_report_v2_requires_exact_canonical_wire_bytes() -> None:
    report = _report_v2()
    noncanonical = json.dumps(report.to_payload(), indent=2, ensure_ascii=False).encode()

    with pytest.raises(ProvenanceError, match="canonical JSON"):
        verify_report_v2(noncanonical)


def test_report_v1_contract_remains_additive_and_unchanged() -> None:
    report = build_report(
        _rows([0.5, -0.1] * 30),
        horizons=[3],
        candidate_minimum=30,
        research_minimum=300,
        generated_at=datetime(2026, 8, 10, 18, 0, tzinfo=timezone.utc),
    )

    assert REPORT_SCHEMA_VERSION == "signal-statistics-report-v1"
    assert set(report) == {
        "schema_version",
        "generated_at",
        "source_schema_version",
        "read_only",
        "orders_enabled",
        "journal_rows",
        "symbols",
        "horizons",
        "thresholds",
        "status_counts",
        "patterns",
    }
    assert report["schema_version"] == REPORT_SCHEMA_VERSION


def test_report_v2_golden_semantic_and_artifact_identities() -> None:
    report = _report_v2()

    expected_semantic_hash = (
        "sha256:715f4fe3b78ee245fbb88ff7607937725661b9e88cd6200fc93d4cedd7d33191"
    )
    expected_artifact_hash = (
        "sha256:8ec4f6be20854b9034f5c1d3362eda1a828b9eb06b39faedcb973ff35b980daf"
    )

    assert report.report_semantic_hash == expected_semantic_hash
    assert f"sha256:{hashlib.sha256(report.canonical_bytes()).hexdigest()}" == (
        expected_artifact_hash
    )
