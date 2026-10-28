"""Narrow persistence and Telegram boundary contracts for draft ownership."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from mcp_telegram.drafts.contracts import (
    DraftApplyResult,
    DraftObservation,
    SnapshotCoverage,
)


class DraftProjectionRepository(Protocol):
    """Single-writer projection contract supplied by the persistence worker."""

    def for_connection(self, conn: sqlite3.Connection) -> DraftProjectionRepository: ...

    def bind_account(self, account_id: int) -> None: ...

    def apply_realtime(self, observation: DraftObservation) -> DraftApplyResult: ...

    def apply_snapshot(
        self,
        observations: Sequence[DraftObservation],
        coverage: SnapshotCoverage,
        *,
        claim_token: int,
    ) -> DraftApplyResult: ...

    def mark_recovery_needed(self, *, reason: str, observed_at: datetime) -> None: ...

    def recovery_due_at(self) -> float | None: ...

    def recovery_revision(self, account_id: int) -> tuple[str, int] | None: ...

    def claim_recovery(self, *, now: float) -> int | None: ...

    def release_recovery_claim(self, *, claim_token: int, due_at: float) -> bool: ...


class DraftRecoveryScheduling(Protocol):
    """Injected retry schedule for durable draft recovery."""

    @property
    def retry_delays_seconds(self) -> tuple[int, ...]: ...


class DraftSnapshotGateway(Protocol):
    """One classified scalar acquisition of all account draft observations."""

    async def fetch_all_drafts(self) -> tuple[SnapshotCoverage, tuple[DraftObservation, ...]]: ...


__all__ = ["DraftProjectionRepository", "DraftRecoveryScheduling", "DraftSnapshotGateway"]
