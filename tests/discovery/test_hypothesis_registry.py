from trademind.discovery.hypothesis_registry import (
    DuplicateHypothesis,
    HypothesisRegistry,
    HypothesisState,
    RegistryError,
    derive_hypothesis_family_id,
)


def _family():
    return {
        "event_type": "breakout",
        "features": ["h1_bias", "m5_volume"],
        "direction": "trend",
        "outcome": "forward_r",
    }


def _content(window: int):
    return {
        "family_definition": _family(),
        "parameters": {"window": window},
        "primary_metric": "mean_net_r",
    }


def _manifest_hash(char: str = "a") -> str:
    return char * 64


def test_strict_state_machine_and_family_holdout_lock(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = registry.register(
        hypothesis_id="H1",
        family_definition=_family(),
        content_definition=_content(20),
    )
    family_id = derive_hypothesis_family_id(_family())
    assert record.state is HypothesisState.PROPOSED
    assert record.hypothesis_family_id == family_id
    registry.freeze("H1", manifest_hash=_manifest_hash())
    registry.transition("H1", HypothesisState.TRAIN_TESTED)
    registry.transition("H1", HypothesisState.VALIDATION_PASSED)
    consumed = registry.transition("H1", HypothesisState.HOLDOUT_CONSUMED)
    assert consumed.state is HypothesisState.HOLDOUT_CONSUMED
    assert registry.family_status(family_id)["holdout_consumed"] is True

    try:
        registry.register(
            hypothesis_id="H2",
            family_definition=_family(),
            content_definition=_content(21),
        )
    except RegistryError:
        pass
    else:
        raise AssertionError("parameter tweak must not reopen a consumed family holdout")


def test_duplicate_content_and_illegal_transition_are_rejected(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    registry.register(
        hypothesis_id="H1",
        family_definition=_family(),
        content_definition=_content(20),
    )
    try:
        registry.register(
            hypothesis_id="H2",
            family_definition=_family(),
            content_definition=_content(20),
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


def test_family_id_is_derived_from_canonical_semantic_definition():
    first = {
        "event_type": "breakout",
        "features": ["h1_bias", "m5_volume"],
        "direction": "trend",
        "outcome": "forward_r",
    }
    reordered = {
        "outcome": "forward_r",
        "direction": "trend",
        "features": ["h1_bias", "m5_volume"],
        "event_type": "breakout",
    }
    assert derive_hypothesis_family_id(first) == derive_hypothesis_family_id(reordered)


def test_validation_rejection_closes_family(tmp_path):
    registry = HypothesisRegistry(tmp_path / "registry.db")
    record = registry.register(
        hypothesis_id="H1",
        family_definition=_family(),
        content_definition=_content(20),
    )
    registry.freeze("H1", manifest_hash=_manifest_hash("b"))
    registry.transition("H1", HypothesisState.TRAIN_TESTED)
    registry.transition("H1", HypothesisState.VALIDATION_REJECTED)
    assert registry.family_status(record.hypothesis_family_id)["terminal_state"] == (
        "VALIDATION_REJECTED"
    )

    try:
        registry.register(
            hypothesis_id="H2",
            family_definition=_family(),
            content_definition=_content(30),
        )
    except RegistryError:
        pass
    else:
        raise AssertionError("validation-rejected family must remain closed")
