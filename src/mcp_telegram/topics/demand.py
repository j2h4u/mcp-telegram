"""One page of authorized topic acquisition through the shared demand executor."""

from __future__ import annotations

import sqlite3
import time
from typing import cast

from ..dialog_identity import read_dialog_identities
from ..flood import TelegramRpcThrottled
from ..models import DialogType
from ..sqlite_writer import SQLiteWriter
from ..telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    durable_demand_context,
)
from ..telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from ..telegram_rpc_consumers import DemandKind, TelegramRpcSource
from ..telegram_rpc_scheduler import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    rpc_attempt_budget,
    rpc_scope,
)
from .catalog import TopicCatalogRepository, TopicScan
from .contracts import TopicSourceUnavailableError, is_topic_capable
from .ports import TelegramTopicPageGateway


class TopicSnapshotDemandAdapter:
    demand_kind = DemandKind.TOPIC_SNAPSHOT

    def __init__(
        self, gateway: TelegramTopicPageGateway, conn: sqlite3.Connection, *, writer: SQLiteWriter, account_id: int
    ) -> None:
        self._gateway = gateway
        self._conn = conn
        self._writer = writer
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id

    def _selected(
        self, excluded: frozenset[WorkRevision] = frozenset(), *, target: str | None = None
    ) -> WorkRevision | None:
        repository = TopicCatalogRepository(self._conn)
        for dialog_id, _retry_at in repository.pending_rows():
            if target is not None and str(dialog_id) != target:
                continue
            row = cast(
                tuple[object, object, object, object, object, object] | None,
                self._conn.execute(
                    "SELECT generation,scan_revision,revision,offset_date,offset_id,offset_topic "
                    "FROM topic_catalog_state WHERE dialog_id=?",
                    (dialog_id,),
                ).fetchone(),
            )
            if row is None:
                row = (None, None, 0, None, 0, 0)
            selected = WorkRevision(WorkKey(self.demand_kind, self._account_id, str(dialog_id)), repr(tuple(row)))
            if selected not in excluded:
                return selected
        return None

    def status(
        self, now: float, *, excluded: frozenset[WorkRevision] = frozenset(), target: str | None = None
    ) -> DemandStatus | None:
        repository = TopicCatalogRepository(self._conn)
        selected = self._selected(excluded, target=target)
        if selected is None:
            return None
        pending = next((row for row in repository.pending_rows() if str(row[0]) == selected.key.target), None)
        return DemandStatus(release_at=float(pending[1]), selected=selected) if pending is not None else None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        selected = budget.selected
        if selected is None:
            raise ValueError("topic demand requires a selected revision")
        preflight = self._slice_preflight(selected, budget)
        if preflight is not None:
            return preflight
        pending = next(
            (row for row in TopicCatalogRepository(self._conn).pending_rows() if str(row[0]) == selected.key.target),
            None,
        )
        if pending is None or pending[1] > time.time():
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        scan = await self._writer.run(lambda conn: TopicCatalogRepository(conn).begin_page(pending[0]))
        if scan is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return await self._run_selected_scan(selected, scan, budget)

    def _slice_preflight(self, selected: WorkRevision, budget: RpcAttemptBudget) -> SliceOutcome | None:
        status = self.status(time.time(), target=selected.key.target)
        if status is None or status.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if budget.exhausted and not budget.can_prepare_retry:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return None

    async def _run_selected_scan(
        self, selected: WorkRevision, scan: TopicScan, budget: RpcAttemptBudget
    ) -> SliceOutcome:
        accepted, committed_revision = False, None
        try:
            with durable_demand_context(self.demand_kind):
                accepted, committed_revision = await self._refresh_page(scan, budget)
        except TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending:
            raise
        except RpcAttemptBudgetExhaustedError:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        except TelegramRpcThrottled:
            raise
        except TopicSourceUnavailableError as exc:
            reason = str(exc)
            await self._writer.run(lambda conn: TopicCatalogRepository(conn).fail(scan, reason, None))
        if accepted:
            return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, committed_revision)
        after = self._selected(target=selected.key.target)
        return SliceOutcome(selected, SliceOutcomeKind.STALE if after != selected else SliceOutcomeKind.NO_PROGRESS)

    async def _refresh_page(self, scan: TopicScan, budget: RpcAttemptBudget) -> tuple[bool, str | None]:
        if self._known_non_topic_user(scan.dialog_id):
            return await self._writer.run(lambda conn: TopicCatalogRepository(conn).mark_not_applicable_outcome(scan))
        with (
            rpc_scope(TelegramRpcSource.TOPIC_RECONCILIATION, acquisition_kind=AcquisitionKind.TOPIC_SNAPSHOT),
            rpc_attempt_budget(budget),
        ):
            entity = await self._gateway.fetch_entity(scan.dialog_id)
            if not is_topic_capable(entity):
                return await self._writer.run(
                    lambda conn: TopicCatalogRepository(conn).mark_not_applicable_outcome(scan)
                )
            page = await self._gateway.fetch_page(entity, scan.cursor)
        accepted, _complete, revision = await self._writer.run(
            lambda conn: TopicCatalogRepository(conn).apply_page_outcome(scan, page)
        )
        return accepted, revision

    def _known_non_topic_user(self, dialog_id: int) -> bool:
        identity = read_dialog_identities(self._conn, (dialog_id,))[dialog_id]
        return (
            identity.dialog_type is DialogType.USER
            and identity.complete
            and identity.observed_at is not None
            and identity.source in {"directory", "profile", "realtime", "mixed"}
        )
