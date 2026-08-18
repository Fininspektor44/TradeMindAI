"""Tests for scripts/run_ser8_real_demo_pipeline.py.

The full end-to-end fixture (``_full_real_chain``) builds a GENUINE
hypothesis all the way to ACCEPTED using nothing but real, unmodified,
public production APIs -- the SAME authoritative lifecycle chain
``tests/discovery/test_final_verdict_control.py`` already exercises
(ResearchProposalIntake -> ... -> ExperimentManifestV2 -> FROZEN -> Bridge
V2 submission -> TrainTestExecutionControl -> TRAIN_TESTED ->
ValidationExecutionControl -> VALIDATION_PASSED -> FinalHoldoutSealer +
mark_isolated -> HoldoutTriggerBridge -> HOLDOUT_CONSUMED ->
FinalVerdictAcceptanceControl -> ACCEPTED), then writes a genuine
candidate-journal line and genuine MT5 export CSVs into a directory
structured exactly like the script's own auto-discovery convention, and
finally drives the script's own ``run_pipeline``/``advance_research_state``
functions against that real state. Nothing in this fixture is a shortcut,
mock, or invented trading parameter -- it is the same real construction
technique this repository's own discovery test suite already uses.
"""

from __future__ import annotations

import ast
import importlib
import io
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

pipeline_module = importlib.import_module("run_ser8_real_demo_pipeline")

from trademind.discovery.final_verdict_control import FinalVerdictAcceptanceControl  # noqa: E402
from trademind.discovery.holdout_sealer import FinalHoldoutSealer  # noqa: E402
from trademind.discovery.holdout_store import HoldoutSealStore  # noqa: E402
from trademind.discovery.holdout_trigger_bridge import HoldoutTriggerBridge  # noqa: E402
from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState  # noqa: E402
from trademind.discovery.manifest import (  # noqa: E402
    CriteriaMode,
    CriterionOperator,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    FinalHoldoutCriteriaV1,
    ProposalIntakeProvenanceV1,
    build_experiment_manifest_v2,
    persist_experiment_manifest_v2,
)
from trademind.discovery.orchestrator_bridge import DiscoveryOrchestratorBridge  # noqa: E402
from trademind.discovery.split_engine import chronological_split  # noqa: E402
from trademind.discovery.train_test_execution import TrainTestExecutionControl  # noqa: E402
from trademind.discovery.validation_execution import ValidationExecutionControl  # noqa: E402
from trademind.orchestrator.artifact_store import ArtifactStore  # noqa: E402
from trademind.orchestrator.budget import BudgetManager  # noqa: E402
from trademind.orchestrator.control_plane import ControlPlane  # noqa: E402
from trademind.research_execution import ResearchExecutionControl  # noqa: E402
from trademind.research_experiment_specification import ResearchExperimentSpecificationControl  # noqa: E402
from trademind.research_proposal_intake import ResearchProposalIntakeControl  # noqa: E402
from trademind.research_proposal_response import (  # noqa: E402
    RESEARCH_PROPOSAL_RESPONSE_KIND,
    RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
    ResearchProposalResponseV1,
)
from trademind.ser8_mt5_demo_order_send import (  # noqa: E402
    DemoOrderTransportResult,
    FakeDemoOrderTransport,
)
from trademind.signal_statistics_agent_packet import (  # noqa: E402
    build_packet_v2_from_artifact,
    persist_packet_v2,
)
from trademind.signal_statistics_orchestrator_bridge import register_verified_packet_v2_task  # noqa: E402
from trademind.signal_statistics_provenance import (  # noqa: E402
    CandidateContentV2,
    CandidateDefinitionV2,
    CodeProvenance,
)
from trademind.signal_statistics_report import build_report_v2, persist_report_v2  # noqa: E402

_POLICY_HASH = f"sha256:{'2' * 64}"
_SOURCE_HASH = f"sha256:{'3' * 64}"
_DISCOVERY_ROLE = "market-data"
_VALIDATION_ROLE = "market-data-validation"
_TEST_FAMILY = "deterministic_aggregate_v1"
_METRIC = "avg_net_atr"
_HOLDOUT_METRIC = "row_count"
_KEY_ID = "ser8-real-demo-pipeline-test-key-v1"
_KEY_ENV = "SER8_REAL_DEMO_PIPELINE_TEST_HOLDOUT_KEY"
_ACCOUNT = "67206924"
_SYMBOL = "XAUUSD"


def _code_provenance() -> CodeProvenance:
    return CodeProvenance(
        producer_name="trademind",
        producer_version="ser8-real-demo-pipeline-test",
        git_commit="f" * 40,
        revision_source="git_worktree",
    )


def _candidate_v2(symbol: str = _SYMBOL) -> CandidateContentV2:
    return CandidateContentV2(
        candidate_definition=CandidateDefinitionV2(
            source_kind="signal_journal",
            source_namespace="trademind_signal_journal",
            symbol=symbol,
            timeframe="M5",
            feature="spread_pressure",
            horizon=3,
            action_scope="BUY_SELL_DIRECTIONAL",
            evaluation_method_version="signal-statistics-v2",
        ),
        evaluation_policy_hash=_POLICY_HASH,
        metrics={"trades": 24, "win_rate": 0.5},
        status="RESEARCH_CANDIDATE",
        reason_codes=("BELOW_RESEARCH_MINIMUM",),
    )


def _response(packet) -> ResearchProposalResponseV1:
    candidate_id = packet.candidate_bindings[0]["candidate_id"]
    return ResearchProposalResponseV1.from_payload(
        {
            "schema_version": RESEARCH_PROPOSAL_RESPONSE_SCHEMA_VERSION,
            "response_kind": RESEARCH_PROPOSAL_RESPONSE_KIND,
            "proposals": [
                {
                    "candidate_id": candidate_id,
                    "title": "Regime-conditioned continuation",
                    "rationale": "The candidate may depend on volatility regime.",
                    "falsifiable_claim": "The effect remains positive in a predefined regime.",
                    "proposed_test": "Compare predefined public-data subsets for the regime.",
                    "rejection_condition": "Reject if the regime effect is non-positive.",
                    "confidence": "HIGH",
                }
            ],
        }
    )


def _manager(path: Path) -> BudgetManager:
    return BudgetManager(
        path,
        daily_cost_ceiling=100.0,
        monthly_cost_ceiling=100.0,
        daily_token_ceiling=100_000,
        monthly_token_ceiling=100_000,
        per_task_call_limit=8,
        per_role_call_limit=32,
    )


def _timestamps(count: int = 12) -> list[datetime]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [start + timedelta(hours=i) for i in range(count)]


def _csv_bytes(rows: list[datetime], *, base: float = 10.0) -> bytes:
    lines = [f"time,{_METRIC}"] + [f"{t.isoformat()},{base + i}" for i, t in enumerate(rows)]
    return ("\n".join(lines) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class _Chain:
    hypothesis_id: str
    db_path: Path
    orchestrator_db_path: Path
    artifact_root: Path
    research_source_csv: Path
    sealed_holdout_path: Path
    data_root: Path


def _full_real_chain(tmp_path: Path) -> _Chain:
    os.environ[_KEY_ENV] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="  # 32 zero bytes, valid base64

    # ResearchProposalIntakeControl requires ResearchExecutionControl and
    # HypothesisRegistry to share one database file (see
    # research_proposal_intake.py's own constructor check) -- this fixture
    # therefore uses the SAME path for both, exactly like
    # tests/discovery/test_final_verdict_control.py's own _setup() does.
    # The script itself does not require this; --db and --orchestrator-db
    # remain independently configurable for the real research pipeline
    # object graph (ControlPlane/ArtifactStore are separate concerns from
    # the proposal-intake chain this fixture also has to exercise).
    db_path = tmp_path / "registry.db"
    orchestrator_db_path = db_path
    artifact_root = tmp_path / "artifacts"
    data_root = tmp_path / "data"

    store = ArtifactStore(artifact_root)
    control = ControlPlane(orchestrator_db_path)
    budget = _manager(orchestrator_db_path)

    report = build_report_v2(
        (_candidate_v2(),),
        source_snapshot_hash_ref=_SOURCE_HASH,
        source_schema_version="1.1",
        code_provenance=_code_provenance(),
        journal_rows=24,
        generated_at="2026-08-18T12:00:00+00:00",
    )
    report_ref = persist_report_v2(report, artifact_store=store)
    packet = build_packet_v2_from_artifact(report_ref.hash_ref, artifact_store=store)
    packet_ref = persist_packet_v2(packet, artifact_store=store)
    task = register_verified_packet_v2_task(packet_ref.hash_ref, control_plane=control, artifact_store=store)

    execution_control = ResearchExecutionControl(control_plane=control, budget_manager=budget, artifact_store=store)
    authorization = execution_control.create_authorization(
        task_id=task.task_id, task_revision=task.revision, reserved_cost=1.0, reserved_tokens=100,
        authorized_by="operator:execution",
    )
    execution = execution_control.claim_execution(authorization.authorization_id)
    execution = execution_control.mark_call_in_flight(execution.request_hash)
    execution = execution_control.finalize_success(
        execution.request_hash, response=_response(packet), actual_cost=0.5, actual_tokens=50,
    )

    registry = HypothesisRegistry(db_path)
    intake_control = ResearchProposalIntakeControl(execution_control=execution_control, hypothesis_registry=registry)
    spec_control = ResearchExperimentSpecificationControl(intake_control=intake_control, hypothesis_registry=registry)
    pending = intake_control.ingest_succeeded_research_execution_v1(execution.request_hash)[0]
    accepted, _hyp = intake_control.accept_for_hypothesis(pending.intake_id, reviewer_id="operator:reviewer")

    from trademind.discovery.manifest import DatasetArtifact as DatasetArtifactV1

    dataset_file = tmp_path / "spec_dataset.csv"
    dataset_file.write_text("time,symbol,close\n1,XAUUSD,2000.0\n", encoding="utf-8")
    v1_dataset = DatasetArtifactV1.from_path(dataset_file)
    spec = spec_control.create_specification(
        accepted.intake_id, reviewer_id="operator:spec-reviewer", test_family=_TEST_FAMILY,
        primary_metric=_METRIC, alpha=0.05, q=0.10, minimum_effect_size=0.0, max_hypotheses_tests=1,
        datasets=(v1_dataset,), parameters={"horizon": 12},
    )

    # Only what is needed to freeze a manifest and seal a holdout is built
    # here. This fixture deliberately stops at FROZEN(+sealed) -- it never
    # calls TrainTestExecutionControl/ValidationExecutionControl/
    # HoldoutTriggerBridge/FinalVerdictAcceptanceControl itself. Advancing
    # from FROZEN all the way to ACCEPTED is exactly what the script's own
    # ``advance_research_state`` is exercised against in the tests below --
    # proving the SCRIPT does the real advancement, not this fixture.
    holdout_seals = HoldoutSealStore(registry)
    sealer = FinalHoldoutSealer(registry=registry, seals=holdout_seals, keys=pipeline_module.EnvironmentKeyProvider(
        key_id=_KEY_ID, environment_variable=_KEY_ENV
    ))

    rows = _timestamps()
    plan = chronological_split(rows)
    research_source_csv = tmp_path / "full_source.csv"
    research_source_csv.write_bytes(_csv_bytes(rows))

    discovery_rows = rows[: plan.discovery_count]
    validation_rows = rows[plan.discovery_count : plan.discovery_count + plan.validation_count]
    discovery_artifact = store.import_snapshot(io.BytesIO(_csv_bytes(discovery_rows)), media_type="text/csv")
    validation_artifact = store.import_snapshot(io.BytesIO(_csv_bytes(validation_rows)), media_type="text/csv")
    discovery_dataset = DatasetArtifactV2(
        role=_DISCOVERY_ROLE, artifact_hash_ref=discovery_artifact.hash_ref,
        media_type=discovery_artifact.media_type, size_bytes=discovery_artifact.size_bytes,
    )
    validation_dataset = DatasetArtifactV2(
        role=_VALIDATION_ROLE, artifact_hash_ref=validation_artifact.hash_ref,
        media_type=validation_artifact.media_type, size_bytes=validation_artifact.size_bytes,
    )

    provenance = ProposalIntakeProvenanceV1(
        intake_id=spec.intake_id, execution_request_hash=spec.request_hash,
        authorization_id=spec.authorization_id, task_id=spec.task_id, task_revision=spec.task_revision,
        packet_artifact_hash_ref=spec.packet_artifact_hash_ref, packet_semantic_hash=spec.packet_semantic_hash,
        result_artifact_hash_ref=spec.result_artifact_hash_ref, proposal_index=spec.proposal_index,
        candidate_id=spec.candidate_id,
    )
    criteria = EvaluationCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(EvaluationCriterionV1(metric=_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=0.0),),
    )
    final_holdout_criteria = FinalHoldoutCriteriaV1(
        mode=CriteriaMode.ALL,
        criteria=(EvaluationCriterionV1(metric=_HOLDOUT_METRIC, operator=CriterionOperator.GREATER_THAN_OR_EQUAL, threshold=1),),
    )
    manifest = build_experiment_manifest_v2(
        artifact_store=store, hypothesis_id=spec.hypothesis_id, hypothesis_family_id=spec.hypothesis_family_id,
        bound_hypothesis_content_hash=spec.hypothesis_content_hash, proposal_provenance=provenance,
        datasets=(discovery_dataset, validation_dataset), split_plan=plan, split_dataset_role=_DISCOVERY_ROLE,
        test_family=_TEST_FAMILY, primary_metric=_METRIC, evaluation_criteria=criteria, alpha=spec.alpha,
        q=spec.q, minimum_effect_size=spec.minimum_effect_size, max_hypotheses_tests=spec.max_hypotheses_tests,
        trading_friction=None, deterministic_seed=None, code_provenance=_code_provenance(),
        semantic_parameters=spec.parameters, created_at="2026-08-18T00:00:00+00:00",
        created_by="operator:ser8-real-demo-pipeline-test", final_holdout_criteria=final_holdout_criteria,
    )
    artifact = persist_experiment_manifest_v2(manifest, artifact_store=store)
    db = sqlite3.connect(db_path, timeout=30.0)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    try:
        db.execute("BEGIN IMMEDIATE")
        registry.freeze_manifest_v2_in_transaction(db, manifest_artifact_hash_ref=artifact.hash_ref, artifact_store=store)
        db.commit()
    finally:
        db.close()

    sealed_holdout_path = tmp_path / "final.holdout.json"
    plaintext_path = tmp_path / "final-plaintext.csv"
    plaintext_path.write_text(f"time,{_METRIC}\n2026-01-03T00:00:00+00:00,5.0\n2026-01-03T06:00:00+00:00,7.0\n", encoding="utf-8")
    evaluator = pipeline_module._DeterministicAggregateHoldoutEvaluator(primary_metric=_METRIC, parameters={})
    sealer.seal_file(
        hypothesis_id=spec.hypothesis_id, plaintext_path=plaintext_path, destination_path=sealed_holdout_path,
        key_id=_KEY_ID, evaluator_id=evaluator.evaluator_id,
        evaluator_artifact_path=Path(pipeline_module.__file__).resolve(),
    )
    holdout_seals.mark_isolated(
        spec.hypothesis_id, isolation_receipt_hash="c" * 64, public_max_time="2026-01-02T12:00:00+00:00",
        holdout_start_time="2026-01-03T00:00:00+00:00", holdout_end_time="2026-01-04T00:00:00+00:00",
        public_row_count=2, holdout_row_count=2,
    )

    return _Chain(
        hypothesis_id=spec.hypothesis_id, db_path=db_path, orchestrator_db_path=orchestrator_db_path,
        artifact_root=artifact_root, research_source_csv=research_source_csv,
        sealed_holdout_path=sealed_holdout_path, data_root=data_root,
    )


def _write_candidate_journal(
    data_root: Path, *, signal_id: str = "sig-1", runtime_root: Path | None = None
) -> Path:
    """Write a genuine live-candidate journal at the ONE canonical live
    runtime-root location (default: <data_root>/live_signal_runtime_v1),
    the SAME default scripts/run_ser8_real_demo_pipeline.py's own
    discover_inputs() now uses -- never the historical/research
    data/signal_intelligence_v1_16/ archive (see
    test_historical_research_journal_not_silently_used_for_live_execution
    in tests/test_ser8_live_candidate_runtime_entrypoint.py for the
    dedicated proof of that separation)."""
    candidates_dir = (runtime_root or (data_root / "live_signal_runtime_v1"))
    candidates_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "signal_id": signal_id,
        "observed_at": now,
        "created_at": now,
        "symbol": _SYMBOL,
        "timeframe": "M5",
        "setup_family": "spread_pressure",
        "scenario": "continuation",
        "plan": {
            "action": "BUY",
            "entries": [{"price": 2000.0, "allocation": 1.0, "rationale": "test", "order_type": "MARKET"}],
            "stop_price": 1990.0,
            "targets": [2020.0],
            "invalidation": "close below stop",
            "target_rationale": ["r1"],
        },
        "market_features": {},
        "factor_scores": {},
        "factor_reasons": {},
        "provenance": ["test"],
        "generated_from_market_data": True,
        "robot_context_only": {},
    }
    journal_path = candidates_dir / "candidates.jsonl"
    journal_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    return journal_path


def _msc(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, object]]) -> None:
    import csv as _csv

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = _csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_mt5_exports(data_root: Path, *, account: str = _ACCOUNT) -> None:
    mt5_dir = data_root / "mt5"
    captured = datetime.now(timezone.utc) - timedelta(seconds=5)

    account_fields = [
        "time_msc", "account_login", "server", "currency", "balance", "equity", "margin",
        "free_margin", "margin_level", "leverage", "open_positions", "trade_allowed",
        "terminal_connected",
    ]
    account_rows = [{
        "time_msc": _msc(captured), "account_login": account, "server": "Demo-Server",
        "currency": "USD", "balance": 10_000.0, "equity": 10_000.0, "margin": 0.0,
        "free_margin": 10_000.0, "margin_level": 0, "leverage": 100, "open_positions": 0,
        "trade_allowed": 1, "terminal_connected": 1,
    }]
    _write_csv(mt5_dir / f"mt5_risk_account_utc_{account}.csv", account_fields, account_rows)

    position_fields = [
        "time_msc", "account_login", "server", "currency", "position_ticket", "position_id",
        "position_time_msc", "symbol", "magic", "side", "volume", "open_price",
        "current_price", "sl", "tp", "profit", "swap", "comment",
    ]
    _write_csv(mt5_dir / f"mt5_risk_positions_utc_{account}.csv", position_fields, [])

    symbol_fields = [
        "time_msc", "account_login", "server", "currency", "symbol", "digits", "trade_mode",
        "bid", "ask", "tick_size", "tick_value", "tick_value_profit", "tick_value_loss",
        "volume_min", "volume_max", "volume_step", "contract_size", "margin_initial",
        "margin_maintenance", "margin_buy_per_volume", "margin_sell_per_volume", "leverage",
    ]
    symbol_rows = [{
        "time_msc": _msc(captured), "account_login": account, "server": "Demo-Server",
        "currency": "USD", "symbol": _SYMBOL, "digits": 2, "trade_mode": "FULL",
        "bid": 1999.9, "ask": 2000.1, "tick_size": 0.01, "tick_value": 1.0,
        "tick_value_profit": 1.0, "tick_value_loss": 1.0, "volume_min": 0.01,
        "volume_max": 100.0, "volume_step": 0.01, "contract_size": 100.0,
        "margin_initial": 0.0, "margin_maintenance": 0.0, "margin_buy_per_volume": 20.0,
        "margin_sell_per_volume": 20.0, "leverage": 100,
    }]
    _write_csv(mt5_dir / f"mt5_risk_symbols_utc_{account}.csv", symbol_fields, symbol_rows)


# ---------------------------------------------------------------------------
# 1. Authoritative lifecycle only.
# ---------------------------------------------------------------------------


def test_script_never_imports_retired_lineage() -> None:
    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
    forbidden = {
        "trademind.final_holdout_evaluation",
        "trademind.final_holdout_evaluation.FinalHoldoutEvaluationControlV1",
        "trademind.experiment_execution_runtime",
        "trademind.experiment_evidence",
        "trademind.validation_decision",
        "trademind.smc_pattern_journal_evaluator",
        "run_real_discovery_pilot_v1",
    }
    assert not (imported & forbidden), imported & forbidden


def test_retired_control_unreachable_even_if_imported() -> None:
    # The module's own docstring/tests already prove .evaluate() fails closed
    # unconditionally (see tests/test_smc_pattern_journal_evaluator.py). This
    # script's prose (module and function docstrings) legitimately explains
    # that boundary by name -- the "negative assertion, not the thing
    # itself" pattern already established elsewhere in this codebase -- so
    # the meaningful, non-fragile proof that the retired control is never
    # actually CALLED is the AST-based import scan in
    # test_script_never_imports_retired_lineage above: nothing that is
    # never imported can be called.
    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "evaluate":
            pytest.fail("script must never call .evaluate() directly (the retired entry point's name)")


# ---------------------------------------------------------------------------
# 2. Fake state forbidden.
# ---------------------------------------------------------------------------


def test_no_test_fixture_imports_in_executable_path() -> None:
    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = [alias.name for alias in node.names] if hasattr(node, "names") else []
            assert "tests" not in module
            assert "conftest" not in module
            assert not any("Fake" in name or "Stub" in name or "_Static" in name for name in names)


def test_no_hand_constructed_claim_or_authorization_in_source() -> None:
    """The script must never construct ExecutionAuthorizationV1 /
    ExecutionAuthorizationClaimV1 by hand -- only via the real .authorize()/
    .claim() control methods."""
    source = Path(pipeline_module.__file__).read_text(encoding="utf-8")
    assert "ExecutionAuthorizationV1(" not in source
    assert "ExecutionAuthorizationClaimV1(" not in source
    assert "_claim_from_row" not in source
    assert "_authorization_from_row" not in source


# ---------------------------------------------------------------------------
# 3-6: end-to-end preview / execute gating against a genuine ACCEPTED chain.
# ---------------------------------------------------------------------------


def _write_test_risk_profile(tmp_path: Path) -> Path:
    """The repository's real default risk profile
    (config/risk_profiles/standard_v1.json) deliberately restricts
    allowed_signal_states to PUBLISHABLE only, which blocks the SER8
    APPROVED_MANUAL channel by design unless an operator explicitly
    reconfigures it (see ser8_research_risk_gate.py's own docstring). This
    test-scoped profile is that explicit operator reconfiguration --
    identical to the real default in every other field -- so these tests
    can reach a genuine ALLOW decision without weakening the shipped
    default.
    """
    payload = {
        "name": "SER8_TEST_V1", "risk_basis": "MIN_BALANCE_EQUITY", "default_trade_risk_pct": 0.5,
        "maximum_trade_risk_pct": 1.0, "maximum_portfolio_risk_pct": 3.0, "maximum_symbol_risk_pct": 1.5,
        "maximum_correlation_risk_pct": 2.0, "maximum_daily_loss_pct": 2.0,
        "maximum_account_drawdown_pct": 10.0, "maximum_margin_usage_pct": 50.0,
        "minimum_free_margin_pct": 25.0, "maximum_open_trades": 8,
        "maximum_account_snapshot_age_seconds": 120, "maximum_signal_age_seconds": 900,
        "maximum_clock_skew_seconds": 30, "adverse_slippage_ticks": 2.0,
        "minimum_risk_utilization_pct": 0.0, "require_margin_check": True,
        "require_complete_portfolio_risk": True, "allowed_signal_states": ["PUBLISHABLE", "APPROVED_MANUAL"],
    }
    path = tmp_path / "ser8_test_risk_profile.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_args(chain: _Chain, **overrides):
    parser = pipeline_module.build_arg_parser()
    argv = [
        "--data-root", str(chain.data_root),
        "--db", str(chain.db_path),
        "--orchestrator-db", str(chain.orchestrator_db_path),
        "--artifact-root", str(chain.artifact_root),
        "--hypothesis-id", chain.hypothesis_id,
        "--account", _ACCOUNT,
        "--demo-account-allowlist", _ACCOUNT,
        "--research-source-csv", str(chain.research_source_csv),
        "--sealed-holdout-path", str(chain.sealed_holdout_path),
        "--holdout-key-env", _KEY_ENV,
        "--holdout-key-id", _KEY_ID,
        "--holdout-primary-metric", _METRIC,
        "--risk-profile", str(_write_test_risk_profile(chain.data_root.parent)),
    ]
    args = parser.parse_args(argv)
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def test_preview_default_sends_zero_orders(tmp_path: Path) -> None:
    chain = _full_real_chain(tmp_path)
    _write_candidate_journal(chain.data_root)
    _write_mt5_exports(chain.data_root)

    args = _base_args(chain)
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 0

    registry = HypothesisRegistry(chain.db_path)
    assert registry.get(chain.hypothesis_id).state is HypothesisState.ACCEPTED

    # Preview mode never constructs SER8DemoOrderSendControl at all, so the
    # receipts table it owns does not even exist yet -- the strongest
    # possible proof that zero orders were sent.
    db = sqlite3.connect(chain.db_path)
    db.row_factory = sqlite3.Row
    table = db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ser8_mt5_demo_order_receipts'"
    ).fetchall()
    db.close()
    assert table == []


def test_missing_runtime_inputs_reported_together(tmp_path: Path) -> None:
    chain = _full_real_chain(tmp_path)
    # Deliberately do NOT write candidate journal or MT5 exports.
    args = _base_args(chain)
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 2


def test_execute_without_confirmation_denied(tmp_path: Path, capsys) -> None:
    chain = _full_real_chain(tmp_path)
    _write_candidate_journal(chain.data_root)
    _write_mt5_exports(chain.data_root)
    args = _base_args(chain, execute=True, confirm_demo_login=None)
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 4
    captured = capsys.readouterr()
    assert "confirm-demo-login" in captured.err


def test_wrong_account_confirmation_denied(tmp_path: Path) -> None:
    chain = _full_real_chain(tmp_path)
    _write_candidate_journal(chain.data_root)
    _write_mt5_exports(chain.data_root)
    args = _base_args(chain, execute=True, confirm_demo_login="99999999")
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 4


def test_execute_with_fake_transport_sends_exactly_once(tmp_path: Path, monkeypatch) -> None:
    chain = _full_real_chain(tmp_path)
    _write_candidate_journal(chain.data_root)
    _write_mt5_exports(chain.data_root)

    fake = FakeDemoOrderTransport(
        result_factory=lambda request: DemoOrderTransportResult(
            claim_id=request.claim_id, demo_account_id=request.demo_account_id, symbol=request.symbol,
            retcode=10009, retcode_description="TRADE_RETCODE_DONE", order_ticket="1", deal_ticket="2",
            position_ticket="3", filled_volume=request.volume, filled_price=request.price or 2000.0,
        )
    )
    monkeypatch.setattr(pipeline_module, "FileBridgeDemoOrderTransport", lambda **kwargs: fake)

    args = _base_args(chain, execute=True, confirm_demo_login=_ACCOUNT)
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 0
    assert len(fake.calls) == 1

    # A second, independent run for the SAME claim must not resend.
    exit_code_2 = pipeline_module.run_pipeline(args)
    assert exit_code_2 != 0
    assert len(fake.calls) == 1


# ---------------------------------------------------------------------------
# 7. Authoritative advance -- state-machine boundaries.
# ---------------------------------------------------------------------------


def test_advance_fails_closed_on_proposed_state(tmp_path: Path) -> None:
    registry = HypothesisRegistry(tmp_path / "reg.db")
    registry.register(
        hypothesis_id="rpi-v1:sha256:" + "a" * 64 + ":0",
        family_definition={"strategy": "x"},
        content_definition={"family_definition": {"strategy": "x"}, "proposal": {}, "provenance": {}},
    )
    control = ControlPlane(tmp_path / "orc.db")
    store = ArtifactStore(tmp_path / "art")
    train_test = TrainTestExecutionControl(registry=registry, control=control, artifacts=store)
    validator = ValidationExecutionControl(registry=registry, control=control, artifacts=store, train_test=train_test)
    holdout_seals = HoldoutSealStore(registry)
    from trademind.discovery.result_ledger import ResultLedger

    ledger = ResultLedger(tmp_path / "ledger.jsonl")
    keys = pipeline_module.EnvironmentKeyProvider(key_id=_KEY_ID, environment_variable=_KEY_ENV)
    evaluator = pipeline_module._DeterministicAggregateHoldoutEvaluator(primary_metric=_METRIC, parameters={})
    from trademind.discovery.holdout_runner import FinalHoldoutRunner

    runner = FinalHoldoutRunner(
        registry=registry, seals=holdout_seals, keys=keys, ledger=ledger, evaluator=evaluator,
        evaluator_artifact_path=Path(pipeline_module.__file__).resolve(),
    )
    trigger_bridge = HoldoutTriggerBridge(registry=registry, control=control, artifacts=store, validator=validator, runner=runner)
    final_verdict = FinalVerdictAcceptanceControl(registry=registry, control=control, artifacts=store, validator=validator, trigger_bridge=trigger_bridge)
    bridge = DiscoveryOrchestratorBridge(registry=registry, holdout_seals=holdout_seals, control=control, artifacts=store)
    pipeline = pipeline_module.ResearchPipeline(
        registry=registry, control=control, artifacts=store, train_test=train_test, validator=validator,
        holdout_seals=holdout_seals, ledger=ledger, runner=runner, trigger_bridge=trigger_bridge,
        final_verdict=final_verdict, bridge=bridge,
    )
    with pytest.raises(pipeline_module.PipelineGapError):
        pipeline_module.advance_research_state(
            pipeline, "rpi-v1:sha256:" + "a" * 64 + ":0", research_source_csv=None, sealed_holdout_path=None
        )


def test_advance_is_noop_for_already_accepted(tmp_path: Path) -> None:
    chain = _full_real_chain(tmp_path)
    pipeline = pipeline_module.build_research_pipeline(
        db_path=chain.db_path, orchestrator_db_path=chain.orchestrator_db_path,
        artifact_root=chain.artifact_root, holdout_key_env=_KEY_ENV, holdout_key_id=_KEY_ID,
        holdout_primary_metric=_METRIC, holdout_parameters={},
    )
    pipeline_module.advance_research_state(
        pipeline, chain.hypothesis_id, research_source_csv=chain.research_source_csv,
        sealed_holdout_path=chain.sealed_holdout_path,
    )
    assert pipeline.registry.get(chain.hypothesis_id).state is HypothesisState.ACCEPTED
    # Calling again is a genuine no-op (idempotent authoritative calls).
    pipeline_module.advance_research_state(
        pipeline, chain.hypothesis_id, research_source_csv=chain.research_source_csv,
        sealed_holdout_path=chain.sealed_holdout_path,
    )
    assert pipeline.registry.get(chain.hypothesis_id).state is HypothesisState.ACCEPTED


# ---------------------------------------------------------------------------
# SER8 SUPERVISED DEMO RISK PROFILE + BLOCK DIAGNOSTICS V1 -- integration
# tests against the REAL, checked-in profile files.
# ---------------------------------------------------------------------------

_REAL_STANDARD_PROFILE = REPO_ROOT / "config" / "risk_profiles" / "standard_v1.json"
_REAL_SUPERVISED_DEMO_PROFILE = REPO_ROOT / "config" / "risk_profiles" / "ser8_supervised_demo_v1.json"


def test_real_default_standard_profile_blocks_with_full_diagnostics_no_authorization(
    tmp_path: Path, capsys
) -> None:
    """The real, unmodified default (config/risk_profiles/standard_v1.json)
    must never silently become usable for the SER8 APPROVED_MANUAL channel
    -- and a BLOCK must never be a bare one-liner, and must never reach
    authorization/claim/order-send."""
    chain = _full_real_chain(tmp_path)
    _write_candidate_journal(chain.data_root)
    _write_mt5_exports(chain.data_root)

    args = _base_args(chain, risk_profile=_REAL_STANDARD_PROFILE)
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 3
    captured = capsys.readouterr()
    assert "SIGNAL_NOT_APPROVED" in captured.out
    assert "failed checks" in captured.out
    assert "reasons (" in captured.out
    assert captured.err.strip() != ""  # the short refusal line is still printed to stderr too.

    db = sqlite3.connect(chain.db_path)
    db.row_factory = sqlite3.Row
    for table in ("ser8_execution_authorizations", "ser8_execution_authorization_claims", "ser8_mt5_demo_order_receipts"):
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchall()
        assert rows == [], table
    db.close()


def test_real_supervised_demo_profile_reaches_allow_and_preview(tmp_path: Path, capsys) -> None:
    """The real, checked-in supervised-demo profile, selected EXPLICITLY,
    reaches a genuine ALLOW and a full preview -- proving the new profile
    file is the one that actually unblocks the SER8 demo path, not a
    weakening of the default."""
    chain = _full_real_chain(tmp_path)
    _write_candidate_journal(chain.data_root)
    _write_mt5_exports(chain.data_root)

    args = _base_args(chain, risk_profile=_REAL_SUPERVISED_DEMO_PROFILE)
    exit_code = pipeline_module.run_pipeline(args)
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "PREVIEW (NO ORDER SENT)" in captured.out
    assert "RiskDecision.state   = ALLOW" in captured.out
