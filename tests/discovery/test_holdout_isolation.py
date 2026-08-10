import base64
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from trademind.discovery.holdout_crypto import (
    HoldoutCryptoError,
    decrypt_bytes,
    seal_bytes,
    verify_envelope,
    verify_key,
)
from trademind.discovery.holdout_keys import (
    EnvironmentKeyProvider,
    HoldoutKeyError,
    decode_aes256_key,
)
from trademind.discovery.holdout_runner import FinalHoldoutRunner, HoldoutRunError
from trademind.discovery.holdout_sealer import FinalHoldoutSealer, HoldoutSealerError
from trademind.discovery.holdout_store import HoldoutSealError, HoldoutSealStore
from trademind.discovery.hypothesis_registry import (
    HypothesisRegistry,
    HypothesisState,
    RegistryError,
)
from trademind.discovery.manifest import DatasetArtifact, ExperimentManifest
from trademind.discovery.result_ledger import ResultLedger
from trademind.orchestrator.models import PolicyDecision
from trademind.orchestrator.policy import classify_action


KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
MANIFEST_HASH = hashlib.sha256(b"manifest-v1").hexdigest()
EVALUATOR_ARTIFACT = Path(__file__).resolve()
EVALUATOR_HASH = hashlib.sha256(EVALUATOR_ARTIFACT.read_bytes()).hexdigest()
PUBLIC_TEXT = (
    "time,return\n"
    "2026-01-01T00:00:00+00:00,0.01\n"
    "2026-01-02T00:00:00+00:00,-0.02\n"
)
PLAINTEXT_TEXT = (
    "time,return\n"
    "2026-01-03T00:00:00+00:00,0.10\n"
    "2026-01-04T00:00:00+00:00,-0.20\n"
)


class StaticKeys:
    def __init__(self, key=KEY, key_id="holdout-key-v1"):
        self.key = key
        self.key_id = key_id

    def load_key(self, key_id):
        if key_id != self.key_id:
            raise HoldoutKeyError("unknown key")
        return self.key


class CountingEvaluator:
    evaluator_id = "aggregate-v1"

    def evaluate(self, plaintext):
        rows = max(0, plaintext.count(b"\n") - 1)
        return {"trades": rows, "ev_r": 0.125, "passed": rows > 0}


class FailingEvaluator(CountingEvaluator):
    def evaluate(self, plaintext):
        raise RuntimeError("synthetic evaluator failure")


class LeakingEvaluator(CountingEvaluator):
    def evaluate(self, plaintext):
        return {"raw": plaintext.decode("utf-8")}


def _family():
    return {"event": "synthetic", "direction": "both"}


def _frozen_registry(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = registry.register(
        hypothesis_id="H-FINAL",
        family_definition=_family(),
        content_definition={"event": "synthetic", "window": 20},
    )
    registry.freeze(record.hypothesis_id, manifest_hash=MANIFEST_HASH)
    return registry


def _frozen_manifest_case(tmp_path, *, public_text=PUBLIC_TEXT):
    research_root = tmp_path / "research"
    research_root.mkdir()
    public_dataset = research_root / "public.csv"
    public_dataset.write_text(public_text, encoding="utf-8")
    holdout = research_root / "final.csv"
    holdout.write_text(PLAINTEXT_TEXT, encoding="utf-8")

    manifest = ExperimentManifest.new(
        hypothesis_id="H-FINAL",
        family_definition=_family(),
        test_family="holdout-isolation-v1",
        primary_metric="mean_net_r",
        alpha=0.05,
        q=0.10,
        minimum_effect_size=0.05,
        max_hypotheses_tests=10,
        schema_version="discovery-manifest-v1",
        git_commit="test-commit",
        datasets=(DatasetArtifact.from_path(public_dataset),),
        parameters={"window": 20},
    )
    manifest_path = research_root / "manifest.json"
    manifest_hash = manifest.freeze(manifest_path)

    registry = HypothesisRegistry(tmp_path / "registry.db")
    registry.register(
        hypothesis_id=manifest.hypothesis_id,
        family_definition=manifest.family_definition,
        content_definition=manifest.content_definition,
    )
    registry.freeze(manifest.hypothesis_id, manifest_hash=manifest_hash)
    return registry, research_root, public_dataset, holdout, manifest_path


def _sealed_case(tmp_path):
    registry, research_root, _, plaintext, manifest_path = _frozen_manifest_case(tmp_path)
    seals = HoldoutSealStore(registry)
    keys = StaticKeys()
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)
    sealed = research_root / "protected" / "final.holdout.json"
    quarantine = tmp_path / "quarantine"
    receipt = sealer.seal_and_quarantine(
        hypothesis_id="H-FINAL",
        plaintext_path=plaintext,
        destination_path=sealed,
        manifest_path=manifest_path,
        research_root=research_root,
        quarantine_directory=quarantine,
        key_id="holdout-key-v1",
        evaluator_id="aggregate-v1",
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )
    return registry, seals, keys, plaintext, sealed, quarantine, receipt


def _validation_passed(registry):
    registry.transition("H-FINAL", HypothesisState.TRAIN_TESTED)
    registry.transition("H-FINAL", HypothesisState.VALIDATION_PASSED)


def _runner(registry, seals, keys, ledger, evaluator):
    return FinalHoldoutRunner(
        registry=registry,
        seals=seals,
        keys=keys,
        ledger=ledger,
        evaluator=evaluator,
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )


def test_aes_gcm_envelope_round_trip_and_wrong_key_rejected():
    plaintext = b"secret final holdout rows\n"
    document = seal_bytes(
        plaintext,
        key=KEY,
        key_id="k1",
        hypothesis_family_id="hf_test",
        manifest_hash=MANIFEST_HASH,
        evaluator_id="aggregate-v1",
        evaluator_hash=EVALUATOR_HASH,
    )

    assert verify_envelope(document) is document
    verify_key(document, KEY)
    assert decrypt_bytes(document, KEY) == plaintext
    assert base64.b64encode(plaintext).decode("ascii") not in document["ciphertext_b64"]

    with pytest.raises(HoldoutCryptoError):
        verify_key(document, OTHER_KEY)


def test_envelope_tamper_is_detected_before_secret_use():
    document = seal_bytes(
        b"abc",
        key=KEY,
        key_id="k1",
        hypothesis_family_id="hf_test",
        manifest_hash=MANIFEST_HASH,
        evaluator_id="aggregate-v1",
        evaluator_hash=EVALUATOR_HASH,
    )
    document["header"]["evaluator_id"] = "changed-after-seal"

    with pytest.raises(HoldoutCryptoError):
        verify_envelope(document)


def test_environment_key_provider_is_explicit_and_strict(monkeypatch):
    encoded = base64.b64encode(KEY).decode("ascii")
    monkeypatch.setenv("TMAI_TEST_HOLDOUT_KEY", encoded)
    provider = EnvironmentKeyProvider(
        key_id="k1",
        environment_variable="TMAI_TEST_HOLDOUT_KEY",
    )

    assert provider.load_key("k1") == KEY
    assert decode_aes256_key(encoded) == KEY
    with pytest.raises(HoldoutKeyError):
        provider.load_key("other")
    with pytest.raises(HoldoutKeyError):
        decode_aes256_key(base64.b64encode(b"short").decode("ascii"))


def test_orchestrator_policy_forbids_protected_holdout_read():
    result = classify_action("READ_PROTECTED_FINAL_HOLDOUT")
    assert result.decision is PolicyDecision.FORBIDDEN


def test_sealer_encrypts_quarantines_and_attests_without_path_leak(tmp_path):
    registry, seals, _, plaintext, sealed, quarantine, receipt = _sealed_case(tmp_path)

    document_text = sealed.read_text(encoding="utf-8")
    assert PLAINTEXT_TEXT not in document_text
    assert str(plaintext) not in document_text
    assert not plaintext.exists()
    quarantined = list(quarantine.glob("*.holdout-source"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == PLAINTEXT_TEXT
    assert receipt.isolated is True
    assert receipt.isolation_receipt_hash is not None
    assert receipt.hypothesis_family_id == registry.get("H-FINAL").hypothesis_family_id
    assert receipt.evaluator_id == "aggregate-v1"
    assert receipt.evaluator_hash == EVALUATOR_HASH
    stored = seals.get("H-FINAL")
    assert stored.envelope_hash == receipt.envelope_hash
    assert stored.key_id == "holdout-key-v1"
    assert stored.isolated is True


def test_manifest_dataset_may_not_overlap_final_holdout_time_range(tmp_path):
    overlapping_public = (
        "time,return\n"
        "2026-01-01T00:00:00+00:00,0.01\n"
        "2026-01-03T00:00:00+00:00,0.02\n"
    )
    registry, research_root, _, holdout, manifest_path = _frozen_manifest_case(
        tmp_path,
        public_text=overlapping_public,
    )
    seals = HoldoutSealStore(registry)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=StaticKeys())

    with pytest.raises(HoldoutSealerError, match="overlaps or reaches"):
        sealer.seal_and_quarantine(
            hypothesis_id="H-FINAL",
            plaintext_path=holdout,
            destination_path=research_root / "protected.json",
            manifest_path=manifest_path,
            research_root=research_root,
            quarantine_directory=tmp_path / "quarantine",
            key_id="holdout-key-v1",
            evaluator_id="aggregate-v1",
            evaluator_artifact_path=EVALUATOR_ARTIFACT,
        )
    assert holdout.exists()
    assert registry.get("H-FINAL").state is HypothesisState.FROZEN


def test_quarantine_must_be_disjoint_from_research_root(tmp_path):
    registry, research_root, _, plaintext, manifest_path = _frozen_manifest_case(tmp_path)
    seals = HoldoutSealStore(registry)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=StaticKeys())

    with pytest.raises(HoldoutSealerError, match="disjoint"):
        sealer.seal_and_quarantine(
            hypothesis_id="H-FINAL",
            plaintext_path=plaintext,
            destination_path=research_root / "protected.json",
            manifest_path=manifest_path,
            research_root=research_root,
            quarantine_directory=research_root / "quarantine",
            key_id="holdout-key-v1",
            evaluator_id="aggregate-v1",
            evaluator_artifact_path=EVALUATOR_ARTIFACT,
        )
    assert plaintext.exists()


def test_sealer_refuses_duplicate_or_post_freeze_redefinition(tmp_path):
    registry, seals, keys, _, _, _, _ = _sealed_case(tmp_path)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)
    duplicate_plaintext = tmp_path / "duplicate-source.csv"
    duplicate_plaintext.write_text(PLAINTEXT_TEXT, encoding="utf-8")

    with pytest.raises(HoldoutSealError):
        sealer.seal_file(
            hypothesis_id="H-FINAL",
            plaintext_path=duplicate_plaintext,
            destination_path=tmp_path / "duplicate.json",
            key_id="holdout-key-v1",
            evaluator_id="aggregate-v2",
            evaluator_artifact_path=EVALUATOR_ARTIFACT,
        )
    assert not (tmp_path / "duplicate.json").exists()

    registry.transition("H-FINAL", HypothesisState.TRAIN_TESTED)
    late_plaintext = tmp_path / "late-source.csv"
    late_plaintext.write_text(PLAINTEXT_TEXT, encoding="utf-8")
    with pytest.raises(HoldoutSealerError):
        sealer.seal_file(
            hypothesis_id="H-FINAL",
            plaintext_path=late_plaintext,
            destination_path=tmp_path / "late.json",
            key_id="holdout-key-v1",
            evaluator_id="aggregate-v1",
            evaluator_artifact_path=EVALUATOR_ARTIFACT,
        )


def test_low_level_seal_without_quarantine_blocks_registry_progress(tmp_path):
    registry = _frozen_registry(tmp_path)
    seals = HoldoutSealStore(registry)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=StaticKeys())
    plaintext = tmp_path / "final.csv"
    plaintext.write_text(PLAINTEXT_TEXT, encoding="utf-8")
    sealed = tmp_path / "final.holdout.json"
    sealer.seal_file(
        hypothesis_id="H-FINAL",
        plaintext_path=plaintext,
        destination_path=sealed,
        key_id="holdout-key-v1",
        evaluator_id="aggregate-v1",
        evaluator_artifact_path=EVALUATOR_ARTIFACT,
    )

    with pytest.raises(RegistryError, match="isolation"):
        registry.transition("H-FINAL", HypothesisState.TRAIN_TESTED)
    assert registry.get("H-FINAL").state is HypothesisState.FROZEN
    assert plaintext.exists()


def test_runner_success_consumes_once_and_emits_only_aggregate_metrics(tmp_path):
    registry, seals, keys, _, sealed, _, receipt = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = _runner(registry, seals, keys, ledger, CountingEvaluator())

    result = runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)

    assert result.envelope_hash == receipt.envelope_hash
    assert result.evaluator_hash == EVALUATOR_HASH
    assert result.aggregate_metrics == {"trades": 2, "ev_r": 0.125, "passed": True}
    assert registry.get("H-FINAL").state is HypothesisState.HOLDOUT_CONSUMED
    assert registry.family_status(receipt.hypothesis_family_id)["holdout_consumed"] is True
    assert ledger.verify()
    ledger_text = (tmp_path / "results.jsonl").read_text(encoding="utf-8")
    assert "time,return" not in ledger_text
    assert "FINAL_HOLDOUT_CLAIM" in ledger_text
    assert "FINAL_HOLDOUT_RESULT" in ledger_text

    with pytest.raises(HoldoutRunError):
        runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)


def test_wrong_key_or_evaluator_fails_preflight_without_consuming_holdout(tmp_path):
    registry, seals, _, _, sealed, _, _ = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger = ResultLedger(tmp_path / "results.jsonl")

    wrong_key_runner = _runner(
        registry,
        seals,
        StaticKeys(key=OTHER_KEY),
        ledger,
        CountingEvaluator(),
    )
    with pytest.raises(HoldoutRunError):
        wrong_key_runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)
    assert registry.get("H-FINAL").state is HypothesisState.VALIDATION_PASSED

    class WrongEvaluator(CountingEvaluator):
        evaluator_id = "other-evaluator"

    wrong_evaluator_runner = _runner(
        registry,
        seals,
        StaticKeys(),
        ledger,
        WrongEvaluator(),
    )
    with pytest.raises(HoldoutRunError):
        wrong_evaluator_runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)
    assert registry.get("H-FINAL").state is HypothesisState.VALIDATION_PASSED


def test_evaluator_source_hash_is_computed_not_trusted_from_object(tmp_path):
    registry, seals, keys, _, _, _, _ = _sealed_case(tmp_path)
    ledger = ResultLedger(tmp_path / "results.jsonl")
    fake_artifact = tmp_path / "fake_evaluator.py"
    fake_artifact.write_text("# not the evaluator source\n", encoding="utf-8")

    with pytest.raises(HoldoutRunError):
        FinalHoldoutRunner(
            registry=registry,
            seals=seals,
            keys=keys,
            ledger=ledger,
            evaluator=CountingEvaluator(),
            evaluator_artifact_path=fake_artifact,
        )


def test_evaluator_failure_burns_one_shot_and_is_recorded_without_error_text(tmp_path):
    registry, seals, keys, _, sealed, _, receipt = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger_path = tmp_path / "results.jsonl"
    ledger = ResultLedger(ledger_path)
    runner = _runner(registry, seals, keys, ledger, FailingEvaluator())

    with pytest.raises(HoldoutRunError):
        runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)

    assert registry.get("H-FINAL").state is HypothesisState.HOLDOUT_CONSUMED
    assert registry.family_status(receipt.hypothesis_family_id)["holdout_consumed"] is True
    ledger_text = ledger_path.read_text(encoding="utf-8")
    assert "FINAL_HOLDOUT_CLAIM" in ledger_text
    assert "FINAL_HOLDOUT_RUN_FAILED" in ledger_text
    assert "synthetic evaluator failure" not in ledger_text


def test_non_scalar_evaluator_output_is_blocked_after_one_shot_claim(tmp_path):
    registry, seals, keys, _, sealed, _, _ = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = _runner(registry, seals, keys, ledger, LeakingEvaluator())

    with pytest.raises(HoldoutRunError):
        runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)
    assert registry.get("H-FINAL").state is HypothesisState.HOLDOUT_CONSUMED


def test_ledger_claim_blocks_rerun_even_after_adversarial_sqlite_reset(tmp_path):
    registry, seals, keys, _, sealed, _, receipt = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = _runner(registry, seals, keys, ledger, CountingEvaluator())
    runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)

    with sqlite3.connect(registry.path) as db:
        db.execute(
            "UPDATE hypotheses SET state=? WHERE hypothesis_id=?",
            (HypothesisState.VALIDATION_PASSED.value, "H-FINAL"),
        )
        db.execute(
            "UPDATE hypothesis_families SET holdout_consumed=0 WHERE family_id=?",
            (receipt.hypothesis_family_id,),
        )

    assert registry.get("H-FINAL").state is HypothesisState.VALIDATION_PASSED
    assert registry.family_status(receipt.hypothesis_family_id)["holdout_consumed"] is False
    with pytest.raises(HoldoutRunError, match="ledger already contains"):
        runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)


def test_existing_runner_lock_blocks_concurrent_run_without_consuming(tmp_path):
    registry, seals, keys, _, sealed, _, _ = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = _runner(registry, seals, keys, ledger, CountingEvaluator())
    lock_path = ledger.path.with_suffix(ledger.path.suffix + ".holdout-run.lock")
    lock_path.write_text("stale-or-active\n", encoding="utf-8")

    with pytest.raises(HoldoutRunError, match="runner lock already exists"):
        runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)
    assert registry.get("H-FINAL").state is HypothesisState.VALIDATION_PASSED
