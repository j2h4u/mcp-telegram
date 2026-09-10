"""Variable I/O boundaries used by folder refresh."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol

from .contracts import (
    FolderDialogCursor,
    FolderDialogItem,
    FolderRule,
    FolderSourceSnapshot,
    FolderStagingSnapshot,
)


class TelegramFolderGateway(Protocol):
    async def fetch_folders(self) -> tuple[FolderRule, ...]: ...

    def iter_dialogs(self, cursor: FolderDialogCursor | None) -> AsyncIterator[FolderDialogItem]: ...


class FolderSnapshotRepository(Protocol):
    def read_generation(self) -> int | None: ...

    def read_consecutive_failures(self) -> int: ...

    def read_last_outcome(self) -> str | None: ...

    def read_last_success_at(self) -> int | None: ...

    def read_next_retry_at(self) -> int | None: ...

    def read_staging(self) -> FolderStagingSnapshot | None: ...

    def save_staging(self, snapshot: FolderStagingSnapshot) -> None: ...

    def clear_staging(self) -> None: ...

    def replace_snapshot(
        self,
        snapshot: FolderSourceSnapshot,
        memberships: tuple[tuple[int, int], ...],
        *,
        completed_at: int,
        expected_generation: int | None = None,
    ) -> int: ...

    def record_attempt(
        self,
        *,
        attempted_at: int,
        outcome: str,
        next_retry_at: int | None,
        consecutive_failures: int,
    ) -> None: ...
