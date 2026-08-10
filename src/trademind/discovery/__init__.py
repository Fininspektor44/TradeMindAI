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
    RegistryError,
    derive_content_hash,
    derive_hypothesis_family_id,
)
from .manifest import DatasetArtifact, ExperimentManifest, ManifestIntegrityError
from .orchestrator_bridge import (
    DiscoveryBridgeError,
    DiscoveryOrchestratorBridge,
    DiscoveryTaskBinding,
)
from .result_ledger import LedgerIntegrityError, ResultLedger
from .split_engine import SplitPlan, chronological_split

__all__ = [
    "DatasetArtifact",
    "DatasetIntegrityError",
    "DiscoveryBridgeError",
    "DiscoveryOrchestratorBridge",
    "DiscoveryTaskBinding",
    "DuplicateHypothesis",
    "EnvironmentKeyProvider",
    "ExperimentManifest",
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
    "PointInTimeError",
    "PointInTimeMarketData",
    "RegistryError",
    "ResultLedger",
    "SplitPlan",
    "chronological_split",
    "decode_aes256_key",
    "derive_content_hash",
    "derive_hypothesis_family_id",
]
