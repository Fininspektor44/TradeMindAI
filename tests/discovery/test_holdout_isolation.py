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
from trademind.discovery.hypothesis_registry import HypothesisRegistry, HypothesisState
from trademind.discovery.result_ledger import ResultLedger
from trademind.orchestrator.models import PolicyDecision
from trademind.orchestrator.policy import classify_action


KEY = bytes(range(32))
OTHER_KEY = bytes(reversed(range(32)))
MANIFEST_HASH = hashlib.sha256(b"manifest-v1").hexdigest()
EVALUATOR_ARTIFACT = Path(__file__).resolve()
EVALUATOR_HASH = hashlib.sha256(EVALUATOR_ARTIFACT.read_bytes()).hexdigest()


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


def _frozen_registry(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = registry.register(
        hypothesis_id="H-FINAL",
        family_definition={"event": "synthetic", "direction": "both"},
        content_definition={"event": "synthetic", "window": 20},
    )
    registry.freeze(record.hypothesis_id, manifest_hash=MANIFEST_HASH)
    return registry


def _sealed_case(tmp_path):
    registry = _frozen_registry(tmp_path)
    seals = HoldoutSealStore(registry)
    keys = StaticKeys()
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)
    plaintext = tmp_path / "final.csv"
    plaintext.write_text("time,return\n1,0.1\n2,-0.2\n", encoding="utf-8")
    sealed = tmp_path / "protected" / "final.holdout.json"
    receipt = sealer.seal_file(
        hypothesis_id="H-FINAL",
        plaintext_path=plaintext,
        destination_path=sealed,
        key_id="holdout-key-v1",
        evaluator_id="aggregate-v1",
        evaluator_hash=EVALUATOR_HASH,
    )
    return registry, seals, keys, plaintext, sealed, receipt


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


def test_sealer_encrypts_and_registers_without_embedding_plaintext_path(tmp_path):
    registry, seals, _, plaintext, sealed, receipt = _sealed_case(tmp_path)

    document_text = sealed.read_text(encoding="utf-8")
    assert plaintext.read_text(encoding="utf-8") not in document_text
    assert str(plaintext) not in document_text
    assert receipt.hypothesis_family_id == registry.get("H-FINAL").hypothesis_family_id
    assert receipt.manifest_hash == MANIFEST_HASH
    assert receipt.evaluator_id == "aggregate-v1"
    assert receipt.evaluator_hash == EVALUATOR_HASH
    stored = seals.get("H-FINAL")
    assert stored.envelope_hash == receipt.envelope_hash
    assert stored.key_id == "holdout-key-v1"


def test_sealer_refuses_duplicate_or_post_freeze_redefinition(tmp_path):
    registry, seals, keys, plaintext, _, _ = _sealed_case(tmp_path)
    sealer = FinalHoldoutSealer(registry=registry, seals=seals, keys=keys)

    with pytest.raises(HoldoutSealError):
        sealer.seal_file(
            hypothesis_id="H-FINAL",
            plaintext_path=plaintext,
            destination_path=tmp_path / "duplicate.json",
            key_id="holdout-key-v1",
            evaluator_id="aggregate-v2",
            evaluator_hash=hashlib.sha256(b"other-evaluator").hexdigest(),
        )
    assert not (tmp_path / "duplicate.json").exists()

    registry.transition("H-FINAL", HypothesisState.TRAIN_TESTED)
    with pytest.raises(HoldoutSealerError):
        sealer.seal_file(
            hypothesis_id="H-FINAL",
            plaintext_path=plaintext,
            destination_path=tmp_path / "late.json",
            key_id="holdout-key-v1",
            evaluator_id="aggregate-v1",
            evaluator_hash=EVALUATOR_HASH,
        )


def test_runner_success_consumes_once_and_emits_only_aggregate_metrics(tmp_path):
    registry, seals, keys, _, sealed, receipt = _sealed_case(tmp_path)
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
    registry, seals, _, _, sealed, _ = _sealed_case(tmp_path)
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
    registry, seals, keys, _, _, _ = _sealed_case(tmp_path)
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
    registry, seals, keys, _, sealed, receipt = _sealed_case(tmp_path)
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
    registry, seals, keys, _, sealed, _ = _sealed_case(tmp_path)
    _validation_passed(registry)
    ledger = ResultLedger(tmp_path / "results.jsonl")
    runner = _runner(registry, seals, keys, ledger, LeakingEvaluator())

    with pytest.raises(HoldoutRunError):
        runner.run_once(hypothesis_id="H-FINAL", sealed_path=sealed)
    assert registry.get("H-FINAL").state is HypothesisState.HOLDOUT_CONSUMED


def test_ledger_claim_blocks_rerun_even_after_adversarial_sqlite_reset(tmp_path):
    registry, seals, keys, _, sealed, receipt = _sealed_case(tmp_path)
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
