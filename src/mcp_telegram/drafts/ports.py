"""Narrow persistence and Telegram boundary contracts for draft ownership."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from mcp_telegram.drafts.contracts import (
    DraftApplyResult,
    DraftObservation,
    DraftScope,
    SnapshotCoverage,
)


class DraftProjectionRepository(Protocol):
    """Single-writer projection contract supplied by the persistence worker."""

    def bind_account(self, account_id: int) -> None: ...

    def apply_realtime(self, observation: DraftObservation) -> DraftApplyResult: ...

    def snapshot_baselines(self, account_id: int) -> Mapping[DraftScope, int]: ...

    def apply_snapshot(
        self,
        observations: Sequence[DraftObservation],
        coverage: SnapshotCoverage,
        baselines: Mapping[DraftScope, int],
    ) -> DraftApplyResult: ...

    def mark_recovery_needed(self, *, reason: str, observed_at: datetime) -> None: ...

    def recovery_due_at(self) -> float | None: ...

    def claim_recovery(self, *, now: float) -> bool: ...

    def rearm_recovery(self, *, reason: str, now: float) -> None: ...


class DraftRecoveryScheduling(Protocol):
    """Injected retry schedule for durable draft recovery."""

    @property
    def retry_delays_seconds(self) -> tuple[int, ...]: ...


class DraftSnapshotGateway(Protocol):
    """One classified scalar acquisition of all account draft observations."""

    async def fetch_all_drafts(self) -> tuple[SnapshotCoverage, tuple[DraftObservation, ...]]: ...


__all__ = ["DraftProjectionRepository", "DraftRecoveryScheduling", "DraftSnapshotGateway"]
