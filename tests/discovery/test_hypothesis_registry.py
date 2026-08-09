from trademind.discovery.hypothesis_registry import (
    DuplicateHypothesis,
    HypothesisRegistry,
    HypothesisState,
    RegistryError,
)


def test_strict_state_machine_and_family_holdout_lock(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = registry.register(
        hypothesis_id="H1",
        hypothesis_family_id="F1",
        content_hash="content-a",
    )
    assert record.state is HypothesisState.PROPOSED
    registry.freeze("H1", manifest_hash="manifest-a")
    registry.transition("H1", HypothesisState.TRAIN_TESTED)
    registry.transition("H1", HypothesisState.VALIDATION_PASSED)
    consumed = registry.transition("H1", HypothesisState.HOLDOUT_CONSUMED)
    assert consumed.state is HypothesisState.HOLDOUT_CONSUMED
    assert registry.family_status("F1")["holdout_consumed"] is True

    try:
        registry.register(
            hypothesis_id="H2",
            hypothesis_family_id="F1",
            content_hash="content-b",
        )
    except RegistryError:
        pass
    else:
        raise AssertionError("consumed family must not reopen final holdout")


def test_duplicate_content_and_illegal_transition_are_rejected(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    registry.register(
        hypothesis_id="H1",
        hypothesis_family_id="F1",
        content_hash="same",
    )
    try:
        registry.register(
            hypothesis_id="H2",
            hypothesis_family_id="F2",
            content_hash="same",
        )
    except DuplicateHypothesis:
        pass
    else:
        raise AssertionError("rebranding identical content must be rejected")

    try:
        registry.transition("H1", HypothesisState.TRAIN_TESTED)
    except RegistryError:
        pass
    else:
        raise AssertionError("PROPOSED cannot skip FROZEN")


def test_validation_rejection_closes_family(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    registry.register(
        hypothesis_id="H1",
        hypothesis_family_id="F1",
        content_hash="content-a",
    )
    registry.freeze("H1", manifest_hash="manifest-a")
    registry.transition("H1", HypothesisState.TRAIN_TESTED)
    registry.transition("H1", HypothesisState.VALIDATION_REJECTED)
    assert registry.family_status("F1")["terminal_state"] == "VALIDATION_REJECTED"

    try:
        registry.register(
            hypothesis_id="H2",
            hypothesis_family_id="F1",
            content_hash="content-b",
        )
    except RegistryError:
        pass
    else:
        raise AssertionError("validation-rejected family must remain closed")
