"""SER8 EXECUTION GEOMETRY EXPERIMENT REPORTING HARDENING + RESUME — proofs.

The real Windows 28-symbol x 4-variant run terminated with:

    payload exceeds maximum canonical JSON bytes 262144

at the FINAL report-hashing step, AFTER every symbol had already been fully
evaluated -- a report-CAPACITY failure, not evidence that execution-geometry
evaluation itself failed. These tests prove:

- the exact legacy 262_144-byte failure scenario reproduces under the
  untouched DEFAULT_JSON_SAFETY_BUDGET, and is fixed by the new, named,
  finite, artifact-specific EXECUTION_GEOMETRY_REPORT_JSON_BUDGET;
- a deliberately excessive report still fails closed at a finite ceiling
  (never unlimited);
- report hash create/verify/write/load all use that SAME budget
  consistently, and the experiment's semantic hash stays deterministic;
- every UNRELATED canonical_json_bytes caller keeps its original,
  untouched 262_144-byte default ceiling;
- per-symbol checkpoints round-trip, are reused only on an EXACT identity
  match, are rejected/recomputed on tampering or any identity-relevant
  input change, let an interrupted run resume and compute only the
  remaining symbols, and never change the final semantic report/hash
  between a fresh run and a resumed run;
- a genuine CONTROL reproduction failure is checkpointed only as the
  actual resolved (failed) symbol result -- never silently upgraded to a
  pass by the checkpoint layer.

No live MT5 calls, no network data acquisition, no broker mutation, no
historical data reacquisition, no protected-holdout access, and no change
to trading logic, signal generation, candidate population, the shadow
evaluator, CONTROL reproduction semantics, the cost model, or ranking
metrics -- build_symbol_geometry_experiment itself is never modified or
monkeypatched to behave differently; only whether/when it is CALLED is
observed.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import trademind.ser8_execution_geometry_experiment as geometry_experiment
from trademind.ser8_execution_geometry_checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    EXECUTION_GEOMETRY_REPORT_JSON_BUDGET,
    build_symbol_checkpoint_identity,
    checkpoint_path_for,
    load_verified_symbol_checkpoint,
    write_symbol_checkpoint,
)
from trademind.ser8_execution_geometry_experiment import (
    ALL_VARIANTS,
    EXPERIMENT_SCHEMA_VERSION,
    STATUS_CONTROL_REPRODUCTION_FAILED,
    VARIANT_CONTROL,
    _EXPERIMENT_HASH_DOMAIN,
    build_multisymbol_geometry_experiment_report,
    build_symbol_geometry_experiment,
    load_verified_multisymbol_geometry_experiment_report,
    write_multisymbol_geometry_experiment_report,
)
from trademind.ser8_historical_data import (
    HistoricalBarV1,
    INVENTORY_SCHEMA_VERSION,
    READ_ONLY_MT5_OPERATIONS,
    BrokerSymbolV1,
    build_canonical_execution_universe,
    build_dataset_manifest,
    load_inventory,
    publish_dataset,
    write_inventory_artifacts,
)
from trademind.ser8_historical_replay import build_research_readiness_inventory, load_research_policy
from trademind.signal_statistics_provenance import (
    DEFAULT_JSON_SAFETY_BUDGET,
    ProvenanceError,
    canonical_json_bytes,
    sha256_bytes,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "67206924"
MARKET_DATA_ACCOUNT = "77053345"
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
POLICY_PATH = REPO_ROOT / "config" / "research" / "ser8_historical_research_policy_v1.json"


def _recompute_experiment_hash(payload: dict) -> str:
    """Reproduce the module's own report-hash formula exactly, using the
    SAME artifact-specific budget it uses -- purely to build synthetic
    fixtures for these tests; not a redefinition of the hashing rule."""
    import hashlib

    semantic = dict(payload)
    semantic.pop("experiment_report_sha256", None)
    return hashlib.sha256(
        _EXPERIMENT_HASH_DOMAIN
        + b"\x00"
        + canonical_json_bytes(semantic, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
    ).hexdigest()


# ---------------------------------------------------------------------------
# Real end-to-end fixtures (mirrors tests/test_ser8_execution_geometry_experiment.py)
# ---------------------------------------------------------------------------


def _bars(count: int, *, symbol: str = "EURUSD", start: datetime = NOW - timedelta(days=10)):
    rows = []
    previous = 1.1000
    for index in range(count):
        close = 1.1000 + index * 0.00003 + math.sin(index / 7.0) * 0.00012
        open_price = previous
        rows.append(
            HistoricalBarV1(
                time_utc=start + timedelta(minutes=5 * index),
                symbol=symbol,
                timeframe="M5",
                open=open_price,
                high=max(open_price, close) + 0.00015,
                low=min(open_price, close) - 0.00015,
                close=close,
                tick_volume=100 + index % 17,
                spread=10,
                real_volume=0,
            )
        )
        previous = close
    return tuple(rows)


def _symbol_row(symbol: str, *, trade_mode: str = "FULL") -> dict[str, str]:
    return {
        "account_login": ACCOUNT,
        "server": "RoboForex-Demo",
        "currency": "USD",
        "symbol": symbol,
        "digits": "5",
        "trade_mode": trade_mode,
        "tick_size": "0.00001",
        "tick_value": "1",
        "tick_value_profit": "1",
        "tick_value_loss": "1",
        "volume_min": "0.01",
        "volume_max": "100",
        "volume_step": "0.01",
        "contract_size": "100000",
        "margin_initial": "0",
        "margin_maintenance": "0",
        "margin_buy_per_volume": "1",
        "margin_sell_per_volume": "1",
        "leverage": "100",
        "expiration_mode_flags": "15",
    }


def _proof() -> dict[str, object]:
    return {
        "schema_version": "ser8-mt5-history-source-proof-v1",
        "source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "market_data_account_server": "Broker-ECN",
        "market_data_account_company": "Broker",
        "market_data_account_currency": "USD",
        "terminal_company": "Broker",
        "authenticated_market_data_account_verified": True,
        "utc_contract": "UTC",
        "read_only_operations": list(READ_ONLY_MT5_OPERATIONS),
    }


def _entry(*, symbol: str, bars, dataset_root: Path) -> dict[str, object]:
    row = _symbol_row(symbol)
    universe = build_canonical_execution_universe(
        [row], account_login=ACCOUNT, raw_sha256=sha256_bytes(b"universe raw")
    )
    broker = BrokerSymbolV1(
        symbol=symbol, trade_mode="FULL", source_row=row,
        asset_class="FX", risk_model_supported=True, risk_model_reason="",
    )
    manifest, bars_bytes = build_dataset_manifest(
        bars=bars,
        source_proof=_proof(),
        symbol_metadata={"name": symbol, "point": 0.00001, "digits": 5, "visible": True, "trade_tick_size": 0.00001},
        broker_symbol=broker,
        execution_account_login=ACCOUNT,
        execution_universe_source=f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        execution_universe=universe,
        timeframe="M5",
        requested_from_utc=NOW - timedelta(days=10),
        requested_to_utc=NOW,
        expected_interval_seconds=300,
        source_capture_utc=NOW,
        collector_code_sha256="sha256:" + "c" * 64,
    )
    dataset_dir, _, _ = publish_dataset(dataset_root, manifest, bars_bytes)
    return {
        "symbol": symbol, "asset_class": "FX", "broker_trade_mode": "FULL",
        "risk_model_supported": True, "row_count": len(bars),
        "accepted_historical_data": True, "dataset_sha256": manifest["dataset_sha256"],
        "dataset_dir": str(dataset_dir), "status": "HISTORICAL_DATA_READY",
        "status_reason": "geometry checkpoint fixture",
    }


def _inventory_identity() -> dict[str, object]:
    universe = build_canonical_execution_universe(
        [_symbol_row("EURUSD")], account_login=ACCOUNT, raw_sha256=sha256_bytes(b"universe raw")
    )
    return {
        "execution_account_login": ACCOUNT,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "execution_universe_source": f"mt5_risk_symbols_utc_{ACCOUNT}.csv",
        "execution_universe_sha256": universe.canonical_sha256,
        "execution_universe_raw_sha256": universe.raw_sha256,
        "execution_universe_canonical_sha256": universe.canonical_sha256,
        "execution_universe_canonical_schema_version": universe.canonical_snapshot["schema_version"],
        "execution_universe_canonical_snapshot": dict(universe.canonical_snapshot),
        "broker_universe_raw_sha256": universe.raw_sha256,
        "market_data_source_type": "MT5_PYTHON_COPY_RATES_RANGE",
        "market_data_account_server": "Broker-ECN",
        "market_data_account_company": "Broker",
        "market_data_account_currency": "USD",
        "source_proof": _proof(),
    }


def _build_real_report(tmp_path: Path, *, symbols: dict[str, tuple]):
    dataset_root = tmp_path / "historical"
    inventory_path = dataset_root / "historical_inventory.json"
    compatibility_path = dataset_root / "historical_rows.csv"
    entries = [_entry(symbol=symbol, bars=bars, dataset_root=dataset_root) for symbol, bars in symbols.items()]
    write_inventory_artifacts(
        inventory_path=inventory_path,
        compatibility_path=compatibility_path,
        payload={
            **_inventory_identity(),
            "schema_version": INVENTORY_SCHEMA_VERSION,
            "captured_at_utc": NOW.isoformat(),
            "total_broker_symbols": len(entries),
            "accepted_dataset_count": len(entries),
            "entries": entries,
        },
    )
    historical_inventory = load_inventory(inventory_path)
    policy = load_research_policy(POLICY_PATH)
    replay_root = tmp_path / "ser8_historical_replay"
    readiness_payload = build_research_readiness_inventory(
        historical_inventory_path=inventory_path,
        replay_root=replay_root,
        policy=policy,
        output_path=replay_root / "research_readiness.json",
        captured_at=NOW,
    )
    return historical_inventory, readiness_payload


# ---------------------------------------------------------------------------
# A/B/C/D: JSON safety budget hardening
# ---------------------------------------------------------------------------


def _synthetic_symbol_entry(symbol: str) -> dict[str, object]:
    """A schema-plausible (not necessarily semantically valid) symbol
    report entry, used only to synthesize oversized payloads for budget
    tests -- never fed through the real evaluation pipeline."""
    metrics = {
        "trade_count": 512, "long_count": 256, "short_count": 256,
        "wins": 300, "losses": 200, "flats": 12, "win_rate": 60.0,
        "gross_profit_r": 400.0, "gross_loss_r": 200.0, "net_r_total": 200.0,
        "profit_factor": 2.0, "expectancy_r": 0.39, "average_winner_r": 1.33,
        "average_loser_r": -1.0, "payoff_ratio": 1.33, "max_drawdown_r": 5.0,
        "cost_r_per_trade_applied": 0.04, "net_r_total_before_cost": 220.0,
        "expectancy_r_before_cost": 0.43, "profitable_only_before_costs": False,
        "chronological_stability": {
            "window_count": 3,
            "windows": [
                {"trade_count": 170, "win_rate": 60.0, "expectancy_r": 0.4,
                 "net_r_total": 68.0, "first_completed_at": "2026-01-01T00:00:00Z",
                 "last_completed_at": "2026-03-01T00:00:00Z"}
                for _ in range(3)
            ],
            "first_to_last_expectancy_delta_r": 0.0,
            "degraded": False,
        },
    }
    variants = {}
    for variant in ALL_VARIANTS:
        variants[variant] = {
            "symbol": symbol, "variant": variant, "candidate_count": 512,
            "evaluated_candidate_count": 512, "skipped_count": 0, "skipped": [],
            "metrics": metrics,
            "comparative": None if variant == VARIANT_CONTROL else {
                "expectancy_delta_r": 0.1, "profit_factor_delta": 0.1,
                "payoff_delta": 0.1, "drawdown_delta_r": -0.5,
                "win_rate_delta": 2.0, "changes_negative_expectancy_to_positive": False,
            },
        }
    return {
        "symbol": symbol,
        "control_reproduction_verified": True,
        "control_reproduction_detail": "CONTROL_BASKET exactly reproduces the published replay outcomes",
        "variants": variants,
    }


def _synthetic_report_payload(symbol_count: int) -> dict[str, object]:
    symbols = [f"SYMTEST{index:04d}" for index in range(symbol_count)]
    symbol_reports = [_synthetic_symbol_entry(symbol) for symbol in symbols]
    payload: dict[str, object] = {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "captured_at_utc": "2026-08-24T12:00:00Z",
        "execution_account_login": ACCOUNT,
        "market_data_account_login": MARKET_DATA_ACCOUNT,
        "historical_inventory_sha256": "a" * 64,
        "readiness_inventory_sha256": "b" * 64,
        "stability_window_count": 3,
        "ready_symbol_count": symbol_count,
        "experiment_valid": True,
        "variants": list(ALL_VARIANTS),
        "symbols": symbol_reports,
        "summary_by_variant": {},
        "screening_authority": "SCREENING_ONLY_NOT_ACCEPTANCE",
        "execution_authority_granted": False,
        "hypotheses_created": 0,
        "hypotheses_accepted": 0,
        "protected_holdout_accessed": False,
    }
    payload["experiment_report_sha256"] = _recompute_experiment_hash(payload)
    return payload


def test_A_legacy_262144_byte_failure_fails_under_default_succeeds_under_experiment_budget() -> None:
    payload = _synthetic_report_payload(symbol_count=40)
    semantic = dict(payload)
    semantic.pop("experiment_report_sha256", None)

    # The exact real-world failure reproduces under the untouched DEFAULT budget.
    with pytest.raises(ProvenanceError, match="262144"):
        canonical_json_bytes(semantic, budget=DEFAULT_JSON_SAFETY_BUDGET)

    # ...but succeeds with the artifact-specific budget, proving this payload
    # is genuinely oversized for DEFAULT while still within the new ceiling.
    encoded = canonical_json_bytes(semantic, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)
    assert len(encoded) > DEFAULT_JSON_SAFETY_BUDGET.max_canonical_bytes


def test_B_deliberately_excessive_report_still_fails_closed_at_a_finite_ceiling() -> None:
    """The new budget is finite, not unlimited: a deliberately excessive
    payload -- far beyond the supported 128-symbol envelope -- still fails
    closed under EXECUTION_GEOMETRY_REPORT_JSON_BUDGET."""
    # Individually well under max_string_length (65_536) and
    # max_sequence_length (4_096) per field, but aggregate string bytes and
    # canonical bytes are pushed far beyond this budget's still-finite
    # ceiling (~18.75 MiB string / ~37.5 MiB canonical).
    malicious = {f"symbol_{index:05d}": "x" * 60_000 for index in range(800)}
    with pytest.raises(ProvenanceError):
        canonical_json_bytes(malicious, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)


def test_C_hash_create_verify_write_load_roundtrip_uses_the_same_budget(tmp_path: Path) -> None:
    payload = _synthetic_report_payload(symbol_count=40)
    semantic = dict(payload)
    semantic.pop("experiment_report_sha256", None)
    # Precondition: genuinely oversized for the untouched default ceiling.
    assert len(canonical_json_bytes(semantic, budget=EXECUTION_GEOMETRY_REPORT_JSON_BUDGET)) > 262_144

    output = tmp_path / "experiment_report.json"
    write_multisymbol_geometry_experiment_report(output, payload)  # create + verify + write
    reloaded = load_verified_multisymbol_geometry_experiment_report(output)  # load + verify
    assert reloaded == payload
    assert reloaded["experiment_report_sha256"] == payload["experiment_report_sha256"]
    # The semantic hash is fully deterministic: rebuilding it independently
    # (same domain, same budget) reproduces the identical digest.
    assert _recompute_experiment_hash(payload) == payload["experiment_report_sha256"]


def test_D_default_json_safety_budget_is_unchanged_for_unrelated_callers() -> None:
    assert DEFAULT_JSON_SAFETY_BUDGET.max_canonical_bytes == 262_144
    assert DEFAULT_JSON_SAFETY_BUDGET.max_total_string_bytes == 196_608
    assert DEFAULT_JSON_SAFETY_BUDGET.max_nodes == 10_000
    assert DEFAULT_JSON_SAFETY_BUDGET.max_mapping_entries == 1_024
    assert DEFAULT_JSON_SAFETY_BUDGET.max_sequence_length == 4_096
    assert DEFAULT_JSON_SAFETY_BUDGET.max_string_length == 65_536

    # An unrelated oversized artifact (no budget kwarg -> default) still
    # fails exactly as it did before this change.
    oversized_unrelated_artifact = {f"k{index}": "v" * 3_000 for index in range(100)}
    with pytest.raises(ProvenanceError, match="196608"):
        canonical_json_bytes(oversized_unrelated_artifact)

    assert EXECUTION_GEOMETRY_REPORT_JSON_BUDGET.max_canonical_bytes > (
        DEFAULT_JSON_SAFETY_BUDGET.max_canonical_bytes
    )
    assert EXECUTION_GEOMETRY_REPORT_JSON_BUDGET is not DEFAULT_JSON_SAFETY_BUDGET


# ---------------------------------------------------------------------------
# E: per-symbol checkpoint write/read roundtrip
# ---------------------------------------------------------------------------


def _identity(**overrides: object) -> dict[str, object]:
    base = dict(
        experiment_schema_version=EXPERIMENT_SCHEMA_VERSION,
        symbol="EURUSD",
        dataset_sha256="a" * 64,
        replay_sha256="b" * 64,
        candidates_sha256="c" * 64,
        outcomes_sha256="d" * 64,
        shadow_max_bars=72,
        shadow_cost_r=0.04,
        stability_window_count=3,
        variants=ALL_VARIANTS,
    )
    base.update(overrides)
    return build_symbol_checkpoint_identity(**base)


def test_E_symbol_checkpoint_write_read_roundtrip(tmp_path: Path) -> None:
    identity = _identity()
    symbol_report = _synthetic_symbol_entry("EURUSD")
    path = checkpoint_path_for(tmp_path, "EURUSD")
    write_symbol_checkpoint(path, identity=identity, symbol_report=symbol_report)
    assert path.is_file()

    loaded = load_verified_symbol_checkpoint(path, expected_identity=identity)
    assert loaded == symbol_report

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["checkpoint_schema_version"] == CHECKPOINT_SCHEMA_VERSION
    assert on_disk["identity"] == identity


def test_E_checkpoint_write_is_atomic_no_leftover_temp_file(tmp_path: Path) -> None:
    identity = _identity()
    path = checkpoint_path_for(tmp_path, "EURUSD")
    write_symbol_checkpoint(path, identity=identity, symbol_report=_synthetic_symbol_entry("EURUSD"))
    assert not (path.parent / f".{path.name}.tmp").exists()
    assert list(path.parent.iterdir()) == [path]


# ---------------------------------------------------------------------------
# F/G/H/I/J/K: end-to-end resume behavior through
# build_multisymbol_geometry_experiment_report, spying on
# build_symbol_geometry_experiment ONLY to observe whether it was called --
# never modifying or monkeypatching its actual behavior.
# ---------------------------------------------------------------------------


def _counting_wrapper(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    original = build_symbol_geometry_experiment

    def _spy(*, symbol: str, **kwargs: object):
        calls.append(symbol)
        return original(symbol=symbol, **kwargs)

    monkeypatch.setattr(geometry_experiment, "build_symbol_geometry_experiment", _spy)
    return calls


def test_F_valid_checkpoint_skips_recomputation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    checkpoint_dir = tmp_path / "checkpoints"
    calls = _counting_wrapper(monkeypatch)

    first = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    assert calls == ["EURUSD"]

    second = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    # No new call: EURUSD was resumed from its verified checkpoint.
    assert calls == ["EURUSD"]
    assert second["experiment_report_sha256"] == first["experiment_report_sha256"]


def test_G_tampered_checkpoint_is_rejected_and_recomputed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    checkpoint_dir = tmp_path / "checkpoints"
    calls = _counting_wrapper(monkeypatch)

    first = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    assert calls == ["EURUSD"]

    checkpoint_file = checkpoint_path_for(checkpoint_dir, "EURUSD")
    raw = checkpoint_file.read_bytes()
    tampered = raw.replace(b'"EURUSD"', b'"TAMPERED"', 1)
    assert tampered != raw
    checkpoint_file.write_bytes(tampered)

    second = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    # Tampering forced a genuine recomputation.
    assert calls == ["EURUSD", "EURUSD"]
    assert second["experiment_report_sha256"] == first["experiment_report_sha256"]
    # The tampered checkpoint was overwritten with a fresh, valid one.
    reloaded = load_verified_symbol_checkpoint(
        checkpoint_file,
        expected_identity=json.loads(checkpoint_file.read_text(encoding="utf-8"))["identity"],
    )
    assert reloaded is not None
    assert reloaded["symbol"] == "EURUSD"


def test_G_truncated_and_non_json_checkpoint_files_are_rejected(tmp_path: Path) -> None:
    identity = _identity()
    valid_path = checkpoint_path_for(tmp_path, "EURUSD")
    write_symbol_checkpoint(valid_path, identity=identity, symbol_report=_synthetic_symbol_entry("EURUSD"))
    truncated = valid_path.read_bytes()[:-5]
    valid_path.write_bytes(truncated)
    assert load_verified_symbol_checkpoint(valid_path, expected_identity=identity) is None

    not_json_path = checkpoint_path_for(tmp_path, "GBPUSD")
    not_json_path.write_text("not json at all {{{", encoding="utf-8")
    assert load_verified_symbol_checkpoint(not_json_path, expected_identity=identity) is None

    missing_path = checkpoint_path_for(tmp_path, "USDJPY")
    assert load_verified_symbol_checkpoint(missing_path, expected_identity=identity) is None


@pytest.mark.parametrize(
    "override",
    [
        {"dataset_sha256": "z" * 64},
        {"replay_sha256": "z" * 64},
        {"candidates_sha256": "z" * 64},
        {"outcomes_sha256": "z" * 64},
        {"shadow_max_bars": 999},
        {"shadow_cost_r": 0.99},
        {"stability_window_count": 7},
        {"variants": (VARIANT_CONTROL,)},
        {"experiment_schema_version": "ser8-execution-geometry-experiment-v2"},
    ],
)
def test_H_any_identity_relevant_input_change_invalidates_the_checkpoint(
    tmp_path: Path, override: dict[str, object]
) -> None:
    original_identity = _identity()
    path = checkpoint_path_for(tmp_path, "EURUSD")
    write_symbol_checkpoint(path, identity=original_identity, symbol_report=_synthetic_symbol_entry("EURUSD"))

    changed_identity = _identity(**override)
    assert changed_identity != original_identity
    assert load_verified_symbol_checkpoint(path, expected_identity=changed_identity) is None
    # The original identity still verifies -- only the changed one is rejected.
    assert load_verified_symbol_checkpoint(path, expected_identity=original_identity) is not None


def test_H_end_to_end_stability_window_count_change_forces_recomputation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    checkpoint_dir = tmp_path / "checkpoints"
    calls = _counting_wrapper(monkeypatch)

    build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    assert calls == ["EURUSD"]

    build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=5,  # different report-level parameter
        captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    assert calls == ["EURUSD", "EURUSD"]


def test_I_interrupted_run_resumes_and_computes_only_remaining_symbols(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path,
        symbols={"EURUSD": _bars(430, symbol="EURUSD"), "GBPUSD": _bars(430, symbol="GBPUSD")},
    )
    checkpoint_dir = tmp_path / "checkpoints"
    calls = _counting_wrapper(monkeypatch)

    full_run = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    assert sorted(calls) == ["EURUSD", "GBPUSD"]

    # Simulate an interruption: EURUSD's checkpoint survives (it completed
    # before the crash); GBPUSD's checkpoint never got written.
    (checkpoint_dir / "GBPUSD.json").unlink()
    calls.clear()
    resume_report: dict[str, list[str]] = {}

    resumed_run = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
        resume_report=resume_report,
    )
    assert calls == ["GBPUSD"]  # only the missing symbol was recomputed
    assert resume_report["resumed"] == ["EURUSD"]
    assert resume_report["recomputed"] == ["GBPUSD"]
    assert resumed_run["experiment_report_sha256"] == full_run["experiment_report_sha256"]


def test_J_fresh_run_and_resumed_run_produce_identical_semantic_report_and_hash(
    tmp_path: Path,
) -> None:
    historical_inventory, readiness_payload = _build_real_report(
        tmp_path,
        symbols={"EURUSD": _bars(430, symbol="EURUSD"), "GBPUSD": _bars(430, symbol="GBPUSD")},
    )
    fresh = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW,
    )  # no checkpoint_dir at all -- identical to pre-checkpointing behavior

    checkpoint_dir = tmp_path / "checkpoints"
    populated = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )
    resumed = build_multisymbol_geometry_experiment_report(
        historical_inventory=historical_inventory, readiness_payload=readiness_payload,
        stability_window_count=3, captured_at=NOW, checkpoint_dir=checkpoint_dir,
    )

    assert fresh["experiment_report_sha256"] == populated["experiment_report_sha256"]
    assert fresh["experiment_report_sha256"] == resumed["experiment_report_sha256"]
    assert fresh == populated == resumed


def test_K_control_reproduction_failure_is_checkpointed_only_as_the_genuine_failed_result(
    tmp_path: Path,
) -> None:
    from trademind.ser8_historical_data import load_canonical_bars, verify_dataset
    from trademind.ser8_historical_multisymbol_screening import load_verified_replay_rows
    from trademind.signal_shadow import load_candidates

    historical_inventory, readiness_payload = _build_real_report(
        tmp_path, symbols={"EURUSD": _bars(430, symbol="EURUSD")}
    )
    readiness_entry = next(e for e in readiness_payload["entries"] if e["symbol"] == "EURUSD")
    replay_dir = Path(readiness_entry["replay_dir"])
    _raw_candidates, published_outcomes, manifest = load_verified_replay_rows(replay_dir)
    candidates = load_candidates(replay_dir / "candidates.jsonl")
    verify_dataset(Path(readiness_entry["dataset_dir"]))
    bars = load_canonical_bars(Path(readiness_entry["dataset_dir"]) / "bars.csv")

    tampered_published = [dict(row) for row in published_outcomes]
    tampered_published[0]["net_r"] = tampered_published[0]["net_r"] + 999.0

    failed_result = build_symbol_geometry_experiment(
        symbol="EURUSD",
        candidates=candidates,
        published_outcome_rows=tampered_published,
        bars=bars,
        max_bars=int(manifest["shadow_max_bars"]),
        cost_r=float(manifest["shadow_cost_r"]),
    )
    assert failed_result["control_reproduction_verified"] is False

    identity = _identity(
        dataset_sha256=str(readiness_entry["dataset_sha256"]),
        replay_sha256=manifest.get("replay_sha256"),
        candidates_sha256=manifest.get("candidates_sha256"),
        outcomes_sha256=manifest.get("outcomes_sha256"),
        shadow_max_bars=int(manifest["shadow_max_bars"]),
        shadow_cost_r=float(manifest["shadow_cost_r"]),
    )
    path = checkpoint_path_for(tmp_path / "checkpoints", "EURUSD")
    write_symbol_checkpoint(path, identity=identity, symbol_report=failed_result)

    reloaded = load_verified_symbol_checkpoint(path, expected_identity=identity)
    assert reloaded is not None
    # The checkpoint layer never upgrades a genuine failure into a pass.
    assert reloaded["control_reproduction_verified"] is False
    assert reloaded == failed_result
    for variant in ALL_VARIANTS[1:]:
        assert reloaded["variants"][variant]["status"] == STATUS_CONTROL_REPRODUCTION_FAILED
        assert reloaded["variants"][variant]["metrics"] is None
