"""Variable I/O boundaries used by folder refresh."""

from __future__ import annotations

from typing import Protocol

from .contracts import FolderSourceSnapshot


class TelegramFolderGateway(Protocol):
    async def fetch_snapshot(self) -> FolderSourceSnapshot: ...


class FolderSnapshotRepository(Protocol):
    def replace_snapshot(self, snapshot: FolderSourceSnapshot, memberships: tuple[tuple[int, int], ...]) -> None: ...
