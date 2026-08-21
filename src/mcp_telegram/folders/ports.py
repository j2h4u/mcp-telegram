"""Variable I/O boundaries used by folder refresh."""

from __future__ import annotations

from typing import Protocol

from .contracts import FolderSourceSnapshot


class TelegramFolderGateway(Protocol):
    async def fetch_snapshot(self) -> FolderSourceSnapshot: ...


class FolderSnapshotRepository(Protocol):
    def read_consecutive_failures(self) -> int: ...

    def read_last_outcome(self) -> str | None: ...

    def read_last_success_at(self) -> int | None: ...

    def read_next_retry_at(self) -> int | None: ...

    def replace_snapshot(
        self,
        snapshot: FolderSourceSnapshot,
        memberships: tuple[tuple[int, int], ...],
        *,
        completed_at: int,
    ) -> int: ...

    def record_attempt(
        self,
        *,
        attempted_at: int,
        outcome: str,
        next_retry_at: int | None,
        consecutive_failures: int,
    ) -> None: ...
