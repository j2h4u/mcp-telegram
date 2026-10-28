"""Entity and topic reconciliation for the canonical dialog directory."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from functools import wraps
from typing import Protocol, cast

from telethon.errors import PeerIdInvalidError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .dialog_fact_observations import claim_dialog_fact
from .dialog_identity import capture_identity_baseline, publish_dialog_identity
from .dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation
from .flood import TelegramRpcThrottled
from .maintenance_logging import log_maintenance_cycle
from .observation_order import allocate_observation_order
from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)
from .telethon_dialog import observe_dialog_identity
from .topics.contracts import TopicSourceUnavailableError, is_topic_capable
from .topics.refresh import TopicRefresher

logger = logging.getLogger(__name__)


@contextmanager
def _dialog_demand_scope(kind: DemandKind, acquisition_kind: AcquisitionKind) -> Iterator[None]:
    """Install a precise root for direct calls while preserving an adapter root."""
    with durable_demand_context(kind):
        with acquisition_context(acquisition_kind):
            yield


def _dialog_sync_rpc_scope[**P, R](
    kind: DemandKind,
    acquisition_kind: AcquisitionKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Give reconciliation RPCs precise demand identity."""

    def decorate(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with _dialog_demand_scope(kind, acquisition_kind):
                with rpc_scope(TelegramRpcSource.DIALOG_SYNC):
                    return await func(*args, **kwargs)

        return wrapped

    return decorate


class _EntityLike(Protocol):
    id: int
    title: str | None
    first_name: str | None
    last_name: str | None
    username: str | None
    access_hash: int | None
    bot: bool
    broadcast: bool
    participants_count: int | None
    date: datetime | None


class _DialogSyncClient(Protocol):
    async def get_entity(self, _peer: object) -> _EntityLike: ...


_EntityFields = dict[str, object]


_SELECT_DIRTY_DIALOGS_SQL = "SELECT dialog_id FROM dialogs WHERE needs_refresh = 1 AND hidden = 0"
_SELECT_DIRTY_DIALOG_EXISTS_SQL = "SELECT 1 FROM dialogs WHERE needs_refresh = 1 AND hidden = 0 LIMIT 1"
_UPDATE_DIALOG_ENTITY_SQL = (
    "UPDATE dialogs SET members=?, created=COALESCE(?,created), needs_refresh=0, snapshot_at=? "
    "WHERE dialog_id=? AND revision=?"
)


def _extract_entity_fields(entity: _EntityLike) -> _EntityFields:
    """Return non-identity metadata from the fetched entity."""
    if isinstance(entity, (types.Chat, types.Channel)):
        members = entity.participants_count
        created = int(entity.date.timestamp()) if entity.date else None
    else:
        members = None
        created = None
    return {"members": members, "created": created}


class DialogReconciliationWorker:
    """Refresh dirty dialog entities and optional forum topics."""

    def __init__(
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        topic_refresher: TopicRefresher | None = None,
        *,
        writer: SQLiteWriter,
    ) -> None:
        self._client = cast(_DialogSyncClient, client)
        self._conn = conn
        self._writer = writer
        self._shutdown_event = shutdown_event
        self._topic_refresher = topic_refresher

    async def _handle_light_throttling(self, exc: TelegramRpcThrottled, dialog_id: int) -> bool:
        del dialog_id
        raise exc

    def _persist_light_dialog(  # noqa: PLR0913, PLR0917
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        entity: _EntityLike,
        dialog_revision: int,
        identity_revision: int | None,
        observation_order: int | None = None,
    ) -> tuple[bool, str | None]:
        fields = _extract_entity_fields(entity)
        snapshot_at = int(time.time())
        observation = observe_dialog_identity(entity, dialog_id=dialog_id, source="profile", observed_at=snapshot_at)
        with write_transaction(conn):
            cursor = conn.execute(
                _UPDATE_DIALOG_ENTITY_SQL,
                (fields["members"], fields["created"], snapshot_at, dialog_id, dialog_revision),
            )
            if cursor.rowcount != 1:
                return False, None
            identity_published = _publish_light_identity(
                conn, dialog_id, observation, identity_revision, observation_order
            )
            current = cast(
                tuple[int, int] | None,
                conn.execute(
                    "SELECT revision,identity_revision FROM dialogs WHERE dialog_id=?", (dialog_id,)
                ).fetchone(),
            )
            successor = None if current is None else f"{current[0]}:{current[1]}"
        return cursor.rowcount == 1 or identity_published, successor

    async def _refresh_light_dialog(
        self,
        dialog_id: int,
        *,
        refresh_topics: bool = True,
        expected_revision: tuple[int, int] | None = None,
    ) -> tuple[bool | None, str | None]:
        """Refresh one dirty dialog; None means shutdown interrupted a wait."""
        revision = await self._writer.run(
            lambda db: cast(
                tuple[int, int] | None,
                db.execute(
                    "SELECT revision,identity_revision FROM dialogs WHERE dialog_id=? AND needs_refresh=1 AND hidden=0",
                    (dialog_id,),
                ).fetchone(),
            )
        )
        if revision is None:
            return False, None
        dialog_revision, identity_revision = revision
        if expected_revision is not None and revision != expected_revision:
            return False, None
        observation_order = await self._writer.run(allocate_observation_order)
        try:
            entity = await self._client.get_entity(dialog_id)
            accepted, successor = await self._writer.run(
                lambda db: self._persist_light_dialog(
                    db, dialog_id, entity, dialog_revision, identity_revision, observation_order
                )
            )
            if refresh_topics and self._topic_refresher is not None and is_topic_capable(entity):
                topic_count = await self._refresh_forum_topics(dialog_id, entity)
                logger.debug(
                    "recon_light_pass_forum_topics dialog_id=%d count=%d",
                    dialog_id,
                    topic_count,
                )
            return accepted, successor
        except (
            TelegramRpcThrottled,
            RpcAdmissionSaturatedError,
            RpcAdmissionExpiredError,
            PeerIdInvalidError,
            RPCError,
            *ACCESS_LOST_ERRORS,
        ) as exc:
            if await self._handle_light_error(exc, dialog_id):
                return None, None
        return False, None

    async def _handle_light_error(self, exc: Exception, dialog_id: int) -> bool:
        if isinstance(exc, TelegramRpcThrottled):
            return await self._handle_light_throttling(exc, dialog_id)
        if isinstance(exc, (RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
            logger.info(
                "recon_light admission_deferred dialog_id=%d error_type=%s — preserving refresh flag",
                dialog_id,
                type(exc).__name__,
            )
            return False
        if isinstance(exc, ACCESS_LOST_ERRORS):
            reason = type(exc).__name__
            await self._writer.run(lambda db: _record_access_lost(db, dialog_id, reason))
            return False
        if isinstance(exc, PeerIdInvalidError):
            logger.warning(
                "recon_light_pass_peer_invalid dialog_id=%s (session cache miss; will retry next cycle)", dialog_id
            )
            return False
        if isinstance(exc, RPCError):
            logger.warning("recon_light_rpc_error dialog_id=%d error=%s", dialog_id, exc)
            return False
        raise exc

    @_dialog_sync_rpc_scope(DemandKind.DIALOG_LIGHT_RECONCILIATION, AcquisitionKind.ENTITY_LOOKUP)
    async def run_light_pass(
        self,
        *,
        refresh_topics: bool = True,
        dialog_id: int | None = None,
        expected_revision: tuple[int, int] | None = None,
        accepted_revisions: list[str] | None = None,
    ) -> int:
        """Refresh dialogs flagged with ``needs_refresh=1``."""
        rows = (
            [(dialog_id,)]
            if dialog_id is not None
            else cast(list[tuple[int]], self._conn.execute(_SELECT_DIRTY_DIALOGS_SQL).fetchall())
        )
        count = 0
        for (dialog_id,) in rows:
            if self._shutdown_event.is_set():
                logger.info("recon_light_pass_complete count=%d (shutdown)", count)
                return count
            refreshed, successor = await self._refresh_light_dialog(
                dialog_id, refresh_topics=refresh_topics, expected_revision=expected_revision
            )
            if refreshed is None:
                return count
            count += int(refreshed)
            if refreshed and successor is not None and accepted_revisions is not None:
                accepted_revisions.append(successor)
        log_maintenance_cycle(logger, count > 0, "recon_light_pass_complete count=%d", count)
        return count

    @_dialog_sync_rpc_scope(DemandKind.DIALOG_LIGHT_RECONCILIATION, AcquisitionKind.TOPIC_SNAPSHOT)
    async def _refresh_forum_topics(self, dialog_id: int, entity: _EntityLike) -> int:
        """Refresh one topic-capable dialog's canonical topic snapshot."""
        if self._topic_refresher is None:
            return 0
        try:
            count = await self._topic_refresher.refresh(dialog_id, entity)
        except TelegramRpcThrottled as exc:
            raise exc
        except TopicSourceUnavailableError as exc:
            logger.warning(
                "recon_forum_topics_fetch_failed dialog_id=%d error=%s",
                dialog_id,
                exc,
            )
            return 0
        logger.debug("recon_topics_complete dialog_id=%d count=%d", dialog_id, count)
        return count


def _record_access_lost(conn: sqlite3.Connection, dialog_id: int, reason: str) -> None:
    with write_transaction(conn):
        set_access_lost(conn, dialog_id, int(time.time()), reason=reason)


def _publish_light_identity(
    conn: sqlite3.Connection,
    dialog_id: int,
    observation: DialogIdentityObservation | None,
    identity_revision: int | None,
    observation_order: int | None,
) -> bool:
    if observation is None or capture_identity_baseline(conn, dialog_id) != identity_revision:
        return False
    if observation_order is not None:
        name = observation.name
        username = observation.username
        if name is not IDENTITY_OMITTED and not claim_dialog_fact(conn, dialog_id, "name", observation_order):
            name = IDENTITY_OMITTED
        if username is not IDENTITY_OMITTED and not claim_dialog_fact(conn, dialog_id, "username", observation_order):
            username = IDENTITY_OMITTED
        observation = replace(
            observation,
            name=name,
            username=username,
            complete=observation.complete and name is not IDENTITY_OMITTED and username is not IDENTITY_OMITTED,
        )
    return publish_dialog_identity(conn, dialog_id, observation, identity_revision)


class DialogLightReconciliationDemandAdapter:
    """Bounded dirty-dialog adapter over ``dialogs.needs_refresh``."""

    demand_kind = DemandKind.DIALOG_LIGHT_RECONCILIATION

    def __init__(self, worker: DialogReconciliationWorker, *, account_id: int) -> None:
        self._worker = worker
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Report dirty visible dialogs without mutating their flags."""
        del now
        rows = cast(
            list[tuple[int, int, int]],
            self._worker._conn.execute(
                "SELECT dialog_id,revision,identity_revision FROM dialogs WHERE needs_refresh=1 AND hidden=0 ORDER BY dialog_id"
            ).fetchall(),
        )
        for dialog_id, revision, identity_revision in rows:
            selected = WorkRevision(
                WorkKey(self.demand_kind, self._account_id, str(dialog_id)), f"{revision}:{identity_revision}"
            )
            if selected not in excluded:
                return DemandStatus(release_at=0.0, selected=selected)
        return None

    def _selected_for(self, dialog_id: int) -> WorkRevision | None:
        row = cast(
            tuple[int, int, int, int] | None,
            self._worker._conn.execute(
                "SELECT revision,identity_revision,needs_refresh,hidden FROM dialogs WHERE dialog_id=?", (dialog_id,)
            ).fetchone(),
        )
        if row is None or not row[2] or row[3]:
            return None
        return WorkRevision(WorkKey(self.demand_kind, self._account_id, str(dialog_id)), f"{row[0]}:{row[1]}")

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Refresh dirty entity rows until the attempt budget yields."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise ValueError("dialog-light demand requires a selected revision")
        dialog_id = int(selected.key.target)
        current = self._selected_for(dialog_id)
        if current != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if budget.exhausted and not budget.can_prepare_retry:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        revision_parts = selected.revision.split(":", 1)
        expected_revision = (int(revision_parts[0]), int(revision_parts[1]))
        accepted_revisions: list[str] = []
        with durable_demand_context(DemandKind.DIALOG_LIGHT_RECONCILIATION):
            with rpc_attempt_budget(budget):
                try:
                    accepted = await self._worker.run_light_pass(
                        refresh_topics=False,
                        dialog_id=dialog_id,
                        expected_revision=expected_revision,
                        accepted_revisions=accepted_revisions,
                    )
                except RpcAttemptBudgetExhaustedError:
                    return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if accepted and accepted_revisions:
            return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, accepted_revisions[-1])
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
