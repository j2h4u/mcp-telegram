"""Entity and topic reconciliation for the canonical dialog directory."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import Protocol, TypeVar, cast

from telethon.errors import PeerIdInvalidError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .dialog_classification import EntityKind, classify_dialog_type
from .flood import TelegramRpcThrottled, sleep_through_flood
from .maintenance_logging import log_maintenance_cycle
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)
from .topics.contracts import TopicSourceUnavailableError, is_topic_capable
from .topics.refresh import TopicRefresher

logger = logging.getLogger(__name__)
T = TypeVar("T")


@contextmanager
def _dialog_demand_scope(kind: DemandKind, acquisition_kind: AcquisitionKind) -> Iterator[None]:
    """Install a precise root for direct calls while preserving an adapter root."""
    try:
        current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(kind):
            with acquisition_context(acquisition_kind):
                yield
    else:
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


class _ForumTopicLike(Protocol):
    id: int
    title: str | None
    icon_emoji_id: int | None
    date: datetime | None


class _ForumTopicsResultLike(Protocol):
    topics: list[_ForumTopicLike]


class _DialogSyncClient(Protocol):
    async def get_entity(self, _peer: object) -> _EntityLike: ...


_EntityFields = dict[str, object]


def _attr[T](obj: object, name: str, default: T) -> T:
    return cast(T, getattr(obj, name, default))


_SELECT_DIRTY_DIALOGS_SQL = "SELECT dialog_id FROM dialogs WHERE needs_refresh = 1 AND hidden = 0"
_SELECT_DIRTY_DIALOG_EXISTS_SQL = "SELECT 1 FROM dialogs WHERE needs_refresh = 1 AND hidden = 0 LIMIT 1"
_UPDATE_DIALOG_ENTITY_SQL = "UPDATE dialogs SET members=?, created=?, needs_refresh=0, snapshot_at=? WHERE dialog_id=?"


def _extract_entity_fields(entity: _EntityLike) -> _EntityFields:
    """Return canonical entity-derived dialog fields for the light pass."""
    if isinstance(entity, types.User):
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.USER).value
        members = None
        created = None
    elif isinstance(entity, types.Chat):
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.CHAT).value
        members = entity.participants_count
        created = None
    elif isinstance(entity, types.Channel):
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.CHANNEL).value
        members = entity.participants_count
        date = entity.date
        created = int(date.timestamp()) if date else None
    else:
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.UNKNOWN).value
        members = None
        created = None
    return {
        "name": _extract_name(entity),
        "type": dialog_type,
        "members": members,
        "created": created,
    }


def _extract_name(entity: _EntityLike) -> str | None:
    """Build a display name from a Telethon entity."""
    title = _attr(entity, "title", None)
    if title:
        return title
    first = _attr(entity, "first_name", None) or ""
    last = _attr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or None


class DialogReconciliationWorker:
    """Refresh dirty dialog entities and optional forum topics."""

    def __init__(
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        topic_refresher: TopicRefresher | None = None,
    ) -> None:
        self._client = cast(_DialogSyncClient, client)
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._topic_refresher = topic_refresher

    async def _handle_light_throttling(self, exc: TelegramRpcThrottled, dialog_id: int) -> bool:
        if exc.retry_after_seconds is None:
            return False
        wait_s = exc.retry_after_seconds
        logger.warning("recon_light_flood_wait dialog_id=%d wait=%ds", dialog_id, wait_s)
        return await sleep_through_flood(self._shutdown_event, wait_s)

    async def _refresh_light_dialog(self, dialog_id: int, *, refresh_topics: bool = True) -> bool | None:
        """Refresh one dirty dialog; None means shutdown interrupted a wait."""
        try:
            entity = await self._client.get_entity(dialog_id)
            fields = _extract_entity_fields(entity)
            snapshot_at = int(time.time())
            with self._conn:
                self._conn.execute(
                    _UPDATE_DIALOG_ENTITY_SQL,
                    (
                        fields["members"],
                        fields["created"],
                        snapshot_at,
                        dialog_id,
                    ),
                )
            if refresh_topics and self._topic_refresher is not None and is_topic_capable(entity):
                topic_count = await self._refresh_forum_topics(dialog_id, entity)
                logger.debug(
                    "recon_light_pass_forum_topics dialog_id=%d count=%d",
                    dialog_id,
                    topic_count,
                )
            return True
        except TelegramRpcThrottled as exc:
            if await self._handle_light_throttling(exc, dialog_id):
                return None
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            logger.info(
                "recon_light admission_deferred dialog_id=%d error_type=%s — preserving refresh flag",
                dialog_id,
                type(exc).__name__,
            )
        except ACCESS_LOST_ERRORS as exc:
            set_access_lost(self._conn, dialog_id, int(time.time()), reason=type(exc).__name__)
            self._conn.commit()
        except PeerIdInvalidError:
            logger.warning(
                "recon_light_pass_peer_invalid dialog_id=%s (session cache miss; will retry next cycle)",
                dialog_id,
            )
        except RPCError as exc:
            logger.warning(
                "recon_light_rpc_error dialog_id=%d error=%s",
                dialog_id,
                exc,
            )
        return False

    @_dialog_sync_rpc_scope(DemandKind.DIALOG_LIGHT_RECONCILIATION, AcquisitionKind.ENTITY_LOOKUP)
    async def run_light_pass(self, *, refresh_topics: bool = True) -> int:
        """Refresh dialogs flagged with ``needs_refresh=1``."""
        rows = cast(list[tuple[int]], self._conn.execute(_SELECT_DIRTY_DIALOGS_SQL).fetchall())
        count = 0
        for (dialog_id,) in rows:
            if self._shutdown_event.is_set():
                logger.info("recon_light_pass_complete count=%d (shutdown)", count)
                return count
            refreshed = await self._refresh_light_dialog(dialog_id, refresh_topics=refresh_topics)
            if refreshed is None:
                return count
            count += int(refreshed)
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
            if exc.retry_after_seconds is None:
                return 0
            wait_s = exc.retry_after_seconds
            logger.warning(
                "recon_forum_topics_flood_wait dialog_id=%d wait=%ds",
                dialog_id,
                wait_s,
            )
            await sleep_through_flood(self._shutdown_event, wait_s)
            return 0
        except TopicSourceUnavailableError as exc:
            logger.warning(
                "recon_forum_topics_fetch_failed dialog_id=%d error=%s",
                dialog_id,
                exc,
            )
            return 0
        logger.debug("recon_topics_complete dialog_id=%d count=%d", dialog_id, count)
        return count


class DialogLightReconciliationDemandAdapter:
    """Bounded dirty-dialog adapter over ``dialogs.needs_refresh``."""

    demand_kind = DemandKind.DIALOG_LIGHT_RECONCILIATION

    def __init__(self, worker: DialogReconciliationWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        """Report dirty visible dialogs without mutating their flags."""
        del now
        row = cast(tuple[int] | None, self._worker._conn.execute(_SELECT_DIRTY_DIALOG_EXISTS_SQL).fetchone())
        if row is None:
            return None
        return DemandStatus(release_at=0.0)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Refresh dirty entity rows until the attempt budget yields."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.status(time.time()) is None:
            return
        with demand_context(DemandKind.DIALOG_LIGHT_RECONCILIATION):
            with rpc_attempt_budget(budget):
                try:
                    await self._worker.run_light_pass(refresh_topics=False)
                except RpcAttemptBudgetExhaustedError:
                    return
