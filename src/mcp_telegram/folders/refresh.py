"""Application use case for refreshing the local folder snapshot."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..telegram_demand import AcquisitionKind, RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope
from .contracts import FolderDialogCursor, FolderSourceSnapshot, FolderStagingSnapshot
from .membership import matches
from .ports import FolderSnapshotRepository, TelegramFolderGateway
from .telegram_adapter import FOLDER_DIALOG_PAGE_SIZE


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
    def __init__(self, gateway: TelegramFolderGateway, repository: FolderSnapshotRepository) -> None:
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

    async def acquire_slice(self, budget: RpcAttemptBudget) -> FolderAcquisitionSlice:  # noqa: PLR0911
        """Acquire at most one bounded dialog page and publish only at EOF.

        The source cursor and all facts collected so far are written to staging
        after each yielded dialog.  A budget rejection therefore leaves a
        restartable acquisition without changing the published tables.
        """
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")

        staging = self._repository.read_staging()
        current_generation = self._repository.read_generation()
        comparable_generation = 0 if current_generation is None else current_generation
        if (
            staging is not None
            and staging.base_generation is not None
            and staging.base_generation != comparable_generation
        ):
            self._repository.clear_staging()
            staging = None

        if staging is None:
            if budget.exhausted:
                return FolderAcquisitionSlice(None, False)
            try:
                with rpc_scope(
                    TelegramRpcSource.FOLDER_RECONCILIATION,
                    acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT,
                ):
                    folders = await self._gateway.fetch_folders()
            except RpcAttemptBudgetExhaustedError:
                return FolderAcquisitionSlice(None, False)
            staging = FolderStagingSnapshot(
                folders=folders,
                dialogs=(),
                cursor=None,
                started_at=int(time.time()),
                base_generation=comparable_generation,
            )
            self._repository.save_staging(staging)

        if budget.exhausted:
            return FolderAcquisitionSlice(None, False)

        dialogs = list(staging.dialogs)
        cursor: FolderDialogCursor | None = staging.cursor
        page_count = 0
        try:
            with rpc_scope(
                TelegramRpcSource.FOLDER_RECONCILIATION,
                acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT,
            ):
                async for item in self._gateway.iter_dialogs(cursor):
                    dialogs.append(item.facts)
                    cursor = item.cursor
                    page_count += 1
                    self._repository.save_staging(
                        FolderStagingSnapshot(
                            folders=staging.folders,
                            dialogs=tuple(dialogs),
                            cursor=cursor,
                            started_at=staging.started_at,
                            base_generation=staging.base_generation,
                        )
                    )
                    if budget.exhausted:
                        return FolderAcquisitionSlice(None, False)
        except RpcAttemptBudgetExhaustedError:
            return FolderAcquisitionSlice(None, False)

        if page_count >= FOLDER_DIALOG_PAGE_SIZE:
            return FolderAcquisitionSlice(None, False)

        source = FolderSourceSnapshot(folders=staging.folders, dialogs=tuple(dialogs))
        return FolderAcquisitionSlice(
            FolderProjection(source=source, memberships=self._memberships(source)),
            True,
        )

    async def acquire(self) -> FolderProjection:
        # Keep the small direct-test/maintenance gateway contract working. The
        # daemon gateway implements the bounded fetch_folders/iter_dialogs API.
        if not hasattr(self._gateway, "fetch_folders"):
            with rpc_scope(
                TelegramRpcSource.FOLDER_RECONCILIATION,
                acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT,
            ):
                source = await self._gateway.fetch_snapshot()  # type: ignore[attr-defined]
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
