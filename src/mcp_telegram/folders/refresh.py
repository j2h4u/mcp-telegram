"""One-RPC folder rule acquisition and local canonical projection."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..telegram_demand import AcquisitionKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .contracts import FolderRuleObservation
from .ports import FolderSnapshotRepository, TelegramFolderGateway


@dataclass(frozen=True, slots=True)
class FolderRefreshResult:
    folder_count: int
    dialog_count: int
    membership_count: int
    generation: int | None
    coverage_available: bool


class FolderRefresher:
    """Observe rules once, then project entirely from the published catalog."""

    def __init__(self, gateway: TelegramFolderGateway, repository: FolderSnapshotRepository) -> None:
        self._gateway = gateway
        self._repository = repository

    @property
    def supports_bounded_acquisition(self) -> bool:
        return True

    async def acquire(self, *, started_at: int | None = None) -> FolderRuleObservation:
        observation_started_at = int(time.time()) if started_at is None else started_at
        with rpc_scope(TelegramRpcSource.FOLDER_RECONCILIATION, acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT):
            return await self._gateway.fetch_rules(started_at=observation_started_at)

    async def refresh(self, *, completed_at: int | None = None) -> FolderRefreshResult:
        completion = int(time.time()) if completed_at is None else completed_at
        observation = await self.acquire(started_at=completion)
        generation = self._repository.project_observation(observation, completed_at=completion)
        return FolderRefreshResult(len(observation.rules), 0, 0, generation, generation is not None)

    def reproject_current_rules(self, *, now: int) -> int | None:
        return self._repository.reproject_current_rules(now=now)
