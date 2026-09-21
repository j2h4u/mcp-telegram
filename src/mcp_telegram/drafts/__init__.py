"""Account-fenced Telegram draft projection domain."""

from mcp_telegram.drafts.contracts import (
    CompositionCompleteness,
    DraftApplyResult,
    DraftComposition,
    DraftCoverageFreshness,
    DraftCoveragePresence,
    DraftDisposition,
    DraftEntity,
    DraftObservation,
    DraftObservationSource,
    DraftReference,
    DraftScope,
    SnapshotCoverage,
)
from mcp_telegram.drafts.owner import DraftMessageOwner
from mcp_telegram.drafts.ports import DraftProjectionRepository, DraftSnapshotGateway

__all__ = [
    "CompositionCompleteness",
    "DraftApplyResult",
    "DraftComposition",
    "DraftCoverageFreshness",
    "DraftCoveragePresence",
    "DraftDisposition",
    "DraftEntity",
    "DraftMessageOwner",
    "DraftObservation",
    "DraftObservationSource",
    "DraftProjectionRepository",
    "DraftReference",
    "DraftScope",
    "DraftSnapshotGateway",
    "SnapshotCoverage",
]
