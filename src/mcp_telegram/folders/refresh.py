"""Application use case for refreshing the local folder snapshot."""

from __future__ import annotations

import time
from dataclasses import dataclass

from .membership import matches
from .ports import FolderSnapshotRepository, FolderSourceSnapshot, TelegramFolderGateway


@dataclass(frozen=True, slots=True)
class FolderRefreshResult:
    """Counts from one complete source acquisition and projection."""

    folder_count: int
    dialog_count: int
    membership_count: int
    generation: int


@dataclass(frozen=True, slots=True)
class FolderProjection:
    """Completed source acquisition and local membership projection."""

    source: FolderSourceSnapshot
    memberships: tuple[tuple[int, int], ...]


class FolderRefresher:
    def __init__(self, gateway: TelegramFolderGateway, repository: FolderSnapshotRepository) -> None:
        self._gateway = gateway
        self._repository = repository

    async def acquire(self) -> FolderProjection:
        source = await self._gateway.fetch_snapshot()
        memberships = tuple(
            (folder.folder_id, dialog.dialog_id)
            for folder in source.folders
            for dialog in source.dialogs
            if matches(folder, dialog)
        )
        return FolderProjection(source=source, memberships=memberships)

    def persist(self, projection: FolderProjection, *, completed_at: int) -> FolderRefreshResult:
        generation = self._repository.replace_snapshot(
            projection.source,
            projection.memberships,
            completed_at=completed_at,
        )
        return FolderRefreshResult(
            folder_count=len(projection.source.folders),
            dialog_count=len(projection.source.dialogs),
            membership_count=len(projection.memberships),
            generation=generation,
        )

    async def refresh(self, *, completed_at: int | None = None) -> FolderRefreshResult:
        projection = await self.acquire()
        return self.persist(projection, completed_at=int(time.time()) if completed_at is None else completed_at)
