"""Application use case for refreshing the local folder snapshot."""

from __future__ import annotations

from .membership import matches
from .ports import FolderSnapshotRepository, TelegramFolderGateway


class FolderRefresher:
    def __init__(self, gateway: TelegramFolderGateway, repository: FolderSnapshotRepository) -> None:
        self._gateway = gateway
        self._repository = repository

    async def refresh(self) -> None:
        source = await self._gateway.fetch_snapshot()
        memberships = tuple(
            (folder.folder_id, dialog.dialog_id)
            for folder in source.folders
            for dialog in source.dialogs
            if matches(folder, dialog)
        )
        self._repository.replace_snapshot(source, memberships)
