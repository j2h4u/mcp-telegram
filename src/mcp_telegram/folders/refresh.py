"""One-RPC folder rule acquisition and local canonical projection."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass

from ..sqlite_writer import SQLiteWriter
from ..sync_transactions import write_transaction
from ..telegram_demand import AcquisitionKind
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .contracts import FolderRuleObservation
from .ports import FolderSnapshotRepository, TelegramFolderGateway


@dataclass(frozen=True, slots=True)
class FolderRefreshResult:
    dialog_count: int
    generation: int | None
    token: str
    started_at: int
    committed_revision: str | None = None
    accepted: bool = True


class FolderRefresher:
    """Observe rules once, then project entirely from the published catalog."""

    def __init__(
        self, gateway: TelegramFolderGateway, repository: FolderSnapshotRepository, *, writer: SQLiteWriter
    ) -> None:
        self._gateway = gateway
        self._repository = repository
        self._writer = writer

    async def acquire(self, *, started_at: int | None = None) -> FolderRuleObservation:
        observation_started_at = int(time.time()) if started_at is None else started_at
        with rpc_scope(TelegramRpcSource.FOLDER_RECONCILIATION, acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT):
            return await self._gateway.fetch_rules(started_at=observation_started_at)

    async def refresh(
        self, *, completed_at: int | None = None, expected_revision: str | None = None
    ) -> FolderRefreshResult:
        completion = int(time.time()) if completed_at is None else completed_at
        observation = await self.acquire(started_at=completion)

        def publish(conn: sqlite3.Connection) -> FolderRefreshResult:
            with write_transaction(conn):
                repository = self._repository.for_connection(conn)
                if expected_revision is not None and repr(repository.read_snapshot_revision()) != expected_revision:
                    return FolderRefreshResult(0, None, observation.token, observation.started_at, accepted=False)
                generation = repository.project_observation(observation, completed_at=completion)
                revision = None if generation is None else repr(repository.read_snapshot_revision())
                return FolderRefreshResult(0, generation, observation.token, observation.started_at, revision)

        return await self._writer.run(publish)

    async def reproject_current_rules(self, *, now: int) -> int | None:
        return await self._writer.run(
            lambda conn: self._repository.for_connection(conn).reproject_current_rules(now=now)
        )
