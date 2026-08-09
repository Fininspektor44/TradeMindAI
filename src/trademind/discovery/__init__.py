"""Protective research infrastructure for TradeMindAI Discovery Engine."""

from .data_layer import (
    DatasetIntegrityError,
    ImmutableHistoricalDatasetReader,
    PointInTimeError,
    PointInTimeMarketData,
)
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
    "ExperimentManifest",
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
    "derive_content_hash",
    "derive_hypothesis_family_id",
]
