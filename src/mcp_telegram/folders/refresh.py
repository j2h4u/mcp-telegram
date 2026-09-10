"""Application use case for refreshing the local folder snapshot."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import cast

from ..telegram_demand import AcquisitionKind, RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope
from .contracts import FOLDER_DIALOG_PAGE_SIZE, DialogFacts, FolderSourceSnapshot, FolderStagingSnapshot
from .membership import matches
from .ports import FolderSnapshotRepository, LegacyTelegramFolderGateway, TelegramFolderGateway


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


@dataclass(frozen=True, slots=True)
class FolderAcquisitionSlice:
    """Result of one bounded source traversal."""

    projection: FolderProjection | None
    complete: bool


class FolderRefresher:
    def __init__(
        self,
        gateway: TelegramFolderGateway | LegacyTelegramFolderGateway,
        repository: FolderSnapshotRepository,
    ) -> None:
        self._gateway = gateway
        self._repository = repository

    @property
    def supports_bounded_acquisition(self) -> bool:
        return hasattr(self._gateway, "fetch_folders") and hasattr(self._gateway, "iter_dialogs")

    @staticmethod
    def _memberships(source: FolderSourceSnapshot) -> tuple[tuple[int, int], ...]:
        return tuple(
            (folder.folder_id, dialog.dialog_id)
            for folder in source.folders
            for dialog in source.dialogs
            if matches(folder, dialog)
        )

    @staticmethod
    def _normalize_dialogs(dialogs: tuple[DialogFacts, ...] | list[DialogFacts]) -> tuple[DialogFacts, ...]:
        """Keep first positions while letting later observations replace facts."""
        positions: dict[int, int] = {}
        normalized: list[DialogFacts] = []
        for dialog in dialogs:
            dialog_id = dialog.dialog_id
            position = positions.get(dialog_id)
            if position is None:
                positions[dialog_id] = len(normalized)
                normalized.append(dialog)
            else:
                normalized[position] = dialog
        return tuple(normalized)

    def _read_current_staging(self) -> tuple[FolderStagingSnapshot | None, int]:
        staging = self._repository.read_staging()
        generation = self._repository.read_generation()
        comparable_generation = 0 if generation is None else generation
        if staging is not None and staging.base_generation not in {None, comparable_generation}:
            self._repository.clear_staging()
            return None, comparable_generation
        if staging is not None:
            normalized_dialogs = self._normalize_dialogs(staging.dialogs)
            if normalized_dialogs != staging.dialogs:
                staging = FolderStagingSnapshot(
                    folders=staging.folders,
                    dialogs=normalized_dialogs,
                    cursor=staging.cursor,
                    started_at=staging.started_at,
                    base_generation=staging.base_generation,
                )
                self._repository.save_staging(staging)
        return staging, comparable_generation

    async def _start_staging(self, budget: RpcAttemptBudget, base_generation: int) -> FolderStagingSnapshot | None:
        if budget.exhausted:
            return None
        try:
            gateway = cast(TelegramFolderGateway, self._gateway)
            with rpc_scope(
                TelegramRpcSource.FOLDER_RECONCILIATION,
                acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT,
            ):
                folders = await gateway.fetch_folders()
        except RpcAttemptBudgetExhaustedError:
            return None
        staging = FolderStagingSnapshot(
            folders=folders,
            dialogs=(),
            cursor=None,
            started_at=int(time.time()),
            base_generation=base_generation,
        )
        self._repository.save_staging(staging)
        return staging

    async def _acquire_dialog_page(
        self,
        staging: FolderStagingSnapshot,
        budget: RpcAttemptBudget,
    ) -> tuple[FolderStagingSnapshot, int] | None:
        dialogs = list(staging.dialogs)
        positions = {dialog.dialog_id: index for index, dialog in enumerate(dialogs)}
        cursor = staging.cursor
        page_count = 0
        try:
            gateway = cast(TelegramFolderGateway, self._gateway)
            with rpc_scope(
                TelegramRpcSource.FOLDER_RECONCILIATION,
                acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT,
            ):
                async for item in gateway.iter_dialogs(cursor):
                    position = positions.get(item.facts.dialog_id)
                    if position is None:
                        positions[item.facts.dialog_id] = len(dialogs)
                        dialogs.append(item.facts)
                    else:
                        dialogs[position] = item.facts
                    cursor = item.cursor
                    page_count += 1
                    staging = FolderStagingSnapshot(
                        folders=staging.folders,
                        dialogs=tuple(dialogs),
                        cursor=cursor,
                        started_at=staging.started_at,
                        base_generation=staging.base_generation,
                    )
                    self._repository.save_staging(staging)
                    if budget.exhausted:
                        return None
        except RpcAttemptBudgetExhaustedError:
            return None
        return staging, page_count

    async def acquire_slice(self, budget: RpcAttemptBudget) -> FolderAcquisitionSlice:
        """Acquire at most one bounded dialog page and publish only at EOF.

        The source cursor and all facts collected so far are written to staging
        after each yielded dialog.  A budget rejection therefore leaves a
        restartable acquisition without changing the published tables.
        """
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")

        staging, generation = self._read_current_staging()
        if staging is None:
            staging = await self._start_staging(budget, generation)
            if staging is None:
                return FolderAcquisitionSlice(None, False)

        if budget.exhausted:
            return FolderAcquisitionSlice(None, False)

        page = await self._acquire_dialog_page(staging, budget)
        if page is None:
            return FolderAcquisitionSlice(None, False)
        staging, page_count = page

        if page_count >= FOLDER_DIALOG_PAGE_SIZE:
            return FolderAcquisitionSlice(None, False)

        source = FolderSourceSnapshot(folders=staging.folders, dialogs=staging.dialogs)
        return FolderAcquisitionSlice(
            FolderProjection(source=source, memberships=self._memberships(source)),
            True,
        )

    async def acquire(self) -> FolderProjection:
        # Keep the small direct-test/maintenance gateway contract working. The
        # daemon gateway implements the bounded fetch_folders/iter_dialogs API.
        if not hasattr(self._gateway, "fetch_folders"):
            gateway = cast(LegacyTelegramFolderGateway, self._gateway)
            with rpc_scope(
                TelegramRpcSource.FOLDER_RECONCILIATION,
                acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT,
            ):
                source = await gateway.fetch_snapshot()
            source = FolderSourceSnapshot(
                folders=source.folders,
                dialogs=self._normalize_dialogs(source.dialogs),
            )
            return FolderProjection(source=source, memberships=self._memberships(source))

        # Direct callers without the demand coordinator still get a complete
        # acquisition; coordinator slices use acquire_slice above.
        while True:
            budget = RpcAttemptBudget(limit=2)
            with rpc_attempt_budget(budget):
                result = await self.acquire_slice(budget)
            if result.complete and result.projection is not None:
                return result.projection

    def persist(self, projection: FolderProjection, *, completed_at: int) -> FolderRefreshResult:
        staging = self._repository.read_staging()
        generation = self._repository.replace_snapshot(
            projection.source,
            projection.memberships,
            completed_at=completed_at,
            expected_generation=None if staging is None else staging.base_generation,
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
