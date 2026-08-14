"""Protective research infrastructure for TradeMindAI Discovery Engine."""

from .data_layer import (
    DatasetIntegrityError,
    ImmutableHistoricalDatasetReader,
    PointInTimeError,
    PointInTimeMarketData,
)
from .holdout_crypto import HoldoutCryptoError
from .holdout_keys import (
    EnvironmentKeyProvider,
    HoldoutKeyError,
    HoldoutKeyProvider,
    decode_aes256_key,
)
from .holdout_runner import (
    FinalHoldoutRunner,
    HoldoutEvaluator,
    HoldoutRunError,
    HoldoutRunReceipt,
)
from .holdout_sealer import FinalHoldoutSealer, HoldoutSealReceipt, HoldoutSealerError
from .holdout_store import HoldoutSealError, HoldoutSealRecord, HoldoutSealStore
from .hypothesis_registry import (
    DuplicateHypothesis,
    HypothesisRecord,
    HypothesisRegistry,
    HypothesisState,
    ManifestV2FreezeResult,
    RegistryError,
    derive_content_hash,
    derive_hypothesis_family_id,
)
from .manifest import (
    CriteriaMode,
    CriterionOperator,
    DatasetArtifact,
    DatasetArtifactV2,
    EvaluationCriteriaV1,
    EvaluationCriterionV1,
    ExperimentManifest,
    ExperimentManifestV2,
    ManifestIntegrityError,
    ManifestV2PersistenceError,
    ManifestV2ValidationError,
    ProposalIntakeProvenanceV1,
    TradingFrictionV1,
    build_experiment_manifest_v2,
    load_experiment_manifest_v2,
    persist_experiment_manifest_v2,
    verify_experiment_manifest_v2,
)
from .orchestrator_bridge import (
    DiscoveryBridgeError,
    DiscoveryOrchestratorBridge,
    DiscoveryTaskBinding,
)
from .result_ledger import LedgerIntegrityError, ResultLedger
from .split_engine import SplitPlan, chronological_split

__all__ = [
    "DatasetArtifact",
    "DatasetArtifactV2",
    "DatasetIntegrityError",
    "DiscoveryBridgeError",
    "DiscoveryOrchestratorBridge",
    "DiscoveryTaskBinding",
    "DuplicateHypothesis",
    "EnvironmentKeyProvider",
    "ExperimentManifest",
    "ExperimentManifestV2",
    "EvaluationCriteriaV1",
    "EvaluationCriterionV1",
    "FinalHoldoutRunner",
    "FinalHoldoutSealer",
    "HoldoutCryptoError",
    "HoldoutEvaluator",
    "HoldoutKeyError",
    "HoldoutKeyProvider",
    "HoldoutRunError",
    "HoldoutRunReceipt",
    "HoldoutSealError",
    "HoldoutSealReceipt",
    "HoldoutSealRecord",
    "HoldoutSealStore",
    "HoldoutSealerError",
    "HypothesisRecord",
    "HypothesisRegistry",
    "HypothesisState",
    "ImmutableHistoricalDatasetReader",
    "LedgerIntegrityError",
    "ManifestIntegrityError",
    "ManifestV2FreezeResult",
    "ManifestV2PersistenceError",
    "ManifestV2ValidationError",
    "PointInTimeError",
    "PointInTimeMarketData",
    "ProposalIntakeProvenanceV1",
    "RegistryError",
    "ResultLedger",
    "SplitPlan",
    "TradingFrictionV1",
    "CriteriaMode",
    "CriterionOperator",
    "build_experiment_manifest_v2",
    "chronological_split",
    "decode_aes256_key",
    "derive_content_hash",
    "derive_hypothesis_family_id",
    "load_experiment_manifest_v2",
    "persist_experiment_manifest_v2",
    "verify_experiment_manifest_v2",
]
