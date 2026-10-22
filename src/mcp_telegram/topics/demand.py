"""One page of authorized topic acquisition through the shared demand executor."""

from __future__ import annotations

import math
import sqlite3
import time

from ..dialog_identity import read_dialog_identities
from ..flood import TelegramRpcThrottled
from ..models import DialogType
from ..sqlite_writer import SQLiteWriter
from ..telegram_demand import AcquisitionKind, DemandStatus, RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from ..telegram_rpc_consumers import DemandKind, TelegramRpcSource
from ..telegram_rpc_scheduler import rpc_attempt_budget, rpc_scope
from .catalog import TopicCatalogRepository, TopicScan
from .contracts import TopicSourceUnavailableError, is_topic_capable
from .ports import TelegramTopicPageGateway


class TopicSnapshotDemandAdapter:
    demand_kind = DemandKind.TOPIC_SNAPSHOT

    def __init__(self, gateway: TelegramTopicPageGateway, conn: sqlite3.Connection, *, writer: SQLiteWriter) -> None:
        self._gateway = gateway
        self._conn = conn
        self._writer = writer

    def status(self, now: float) -> DemandStatus | None:
        pending = TopicCatalogRepository(self._conn).next_pending()
        return DemandStatus(release_at=float(pending[1])) if pending is not None else None

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        pending = TopicCatalogRepository(self._conn).next_pending()
        if pending is None or pending[1] > time.time():
            return
        scan = await self._writer.run(lambda conn: TopicCatalogRepository(conn).begin_page(pending[0]))
        if scan is None:
            return
        try:
            await self._refresh_page(scan, budget)
        except TelegramRpcThrottled as exc:
            if exc.latched:
                await self._writer.run(lambda conn: TopicCatalogRepository(conn).fail(scan, "account_protection", None))
            else:
                assert exc.retry_after_seconds is not None
                await self._fail(scan, "flood_wait", exc.retry_after_seconds)
            raise
        except RpcAttemptBudgetExhaustedError:
            await self._fail(scan, "attempt_budget_exhausted", 1)
        except TopicSourceUnavailableError as exc:
            reason = str(exc)
            if reason == "source_access_unavailable":
                await self._writer.run(lambda conn: TopicCatalogRepository(conn).fail(scan, reason, None))
            else:
                await self._fail(scan, reason, 300)

    async def _refresh_page(self, scan: TopicScan, budget: RpcAttemptBudget) -> None:
        if self._known_non_topic_user(scan.dialog_id):
            await self._writer.run(lambda conn: TopicCatalogRepository(conn).mark_not_applicable(scan))
            return
        with (
            rpc_scope(TelegramRpcSource.TOPIC_RECONCILIATION, acquisition_kind=AcquisitionKind.TOPIC_SNAPSHOT),
            rpc_attempt_budget(budget),
        ):
            entity = await self._gateway.fetch_entity(scan.dialog_id)
            if not is_topic_capable(entity):
                await self._writer.run(lambda conn: TopicCatalogRepository(conn).mark_not_applicable(scan))
                return
            page = await self._gateway.fetch_page(entity, scan.cursor)
        await self._writer.run(lambda conn: TopicCatalogRepository(conn).apply_page(scan, page))

    def _known_non_topic_user(self, dialog_id: int) -> bool:
        identity = read_dialog_identities(self._conn, (dialog_id,))[dialog_id]
        return (
            identity.dialog_type is DialogType.USER
            and identity.complete
            and identity.observed_at is not None
            and identity.source in {"directory", "profile", "realtime", "mixed"}
        )

    async def _fail(self, scan: TopicScan, reason: str, seconds: float) -> None:
        retry_at = math.ceil(time.time() + seconds)
        await self._writer.run(lambda conn: TopicCatalogRepository(conn).fail(scan, reason, retry_at))
