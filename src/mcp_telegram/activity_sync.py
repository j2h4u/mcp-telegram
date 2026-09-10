"""Global own-message archive worker.

Populates own-message rows (out=1) in the unified messages table
via messages.Search(InputPeerEmpty, from_id=InputPeerSelf).
Runs as a named daemon background task alongside run_access_probe_loop.
"""

import asyncio
import logging
import math
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty, InputPeerSelf

from .activity_substrate import ActivityClient, call_with_timeout
from .demand_shadow_wiring import DemandCycleRunner
from .entity_store import EntitySnapshot, upsert_entity_snapshots
from .flood import TelegramRpcThrottled, sleep_through_flood
from .hydration_queue import HydrationPriority
from .message_contracts import ExtractedMessage
from .messages.sqlite_bundle import insert_messages_with_fts
from .messages.telegram_adapter import extract_dialog_id, extract_message_row
from .models import DialogType
from .own_only import enroll_own_only_sync_dialog
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    UnclassifiedTelegramDemandError,
    current_demand_token,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import RpcAdmissionClosedError, TelegramRpcSource, rpc_attempt_budget, rpc_scope
from .telethon_dialog import classify_dialog_type

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 3600.0
_BACKFILL_BATCH_LIMIT = 100
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 60 * _SECONDS_PER_MINUTE
_INCREMENTAL_MIN_DATE_KEY = "incremental_min_date"
_INCREMENTAL_OFFSET_ID_KEY = "incremental_offset_id"


@dataclass(frozen=True, slots=True)
class ActivitySyncSearchPacing:
    batch_s: float = 0.5


@dataclass(frozen=True, slots=True)
class ActivitySyncPacing:
    search: ActivitySyncSearchPacing = ActivitySyncSearchPacing()


_PACING = ActivitySyncPacing()


@dataclass(slots=True)
class ArchiveBackfillDemandAdapter(DurableDemandAdapter):
    """Expose one global archive-backfill page over existing checkpoints."""

    client: ActivityClient
    conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    timeout_s: float
    demand_kind = DemandKind.ARCHIVE_BACKFILL

    def status(self, now: float) -> DemandStatus | None:
        """Report immediate work until the archive history floor is committed."""
        _validate_status_now(now)
        if _load_state(self.conn).get("backfill_complete") == "1":
            return None
        return DemandStatus(release_at=0.0)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Fetch and commit at most one backfill search page."""
        with _archive_demand_scope(DemandKind.ARCHIVE_BACKFILL):
            with rpc_attempt_budget(budget):
                await _run_backfill_slice(
                    self.client,
                    self.conn,
                    self.shutdown_event,
                    timeout_s=self.timeout_s,
                )


@dataclass(slots=True)
class ArchiveIncrementalDemandAdapter(DurableDemandAdapter):
    """Expose restart-safe incremental archive pages over activity sync state."""

    client: ActivityClient
    conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    interval_s: float
    timeout_s: float
    demand_kind = DemandKind.ARCHIVE_INCREMENTAL

    def status(self, now: float) -> DemandStatus | None:
        """Report the current incremental page or the next periodic release."""
        _validate_status_now(now)
        state = _load_state(self.conn)
        if state.get("backfill_complete") != "1":
            return None
        last_sync_at = int(state.get("last_sync_at") or 0)
        if last_sync_at == 0:
            return None
        freshness_deadline = float(last_sync_at) + self.interval_s
        release_at = 0.0 if state.get(_INCREMENTAL_MIN_DATE_KEY) is not None else freshness_deadline
        return DemandStatus(
            release_at=release_at,
            freshness_deadline=freshness_deadline,
        )

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Fetch and commit at most one incremental search page."""
        status = self.status(time.time())
        if status is None or not status.is_ready(time.time()):
            return
        with _archive_demand_scope(DemandKind.ARCHIVE_INCREMENTAL):
            with rpc_attempt_budget(budget):
                await _run_incremental_slice(
                    self.client,
                    self.conn,
                    self.shutdown_event,
                    timeout_s=self.timeout_s,
                )


@dataclass
class _BackfillState:
    """Mutable state for a single backfill pass."""

    checkpoint: int
    total_fetched: int = 0
    total_known: int | None = None
    batch_num: int = 0
    loop_start: float = 0.0


@dataclass
class _IncrementalState:
    """Mutable state for a single incremental pass."""

    min_date: int
    inserted: int = 0
    batch_num: int = 0
    offset_id: int = 0
    loop_start: float = 0.0


@dataclass
class _IncrementalBatchLog:
    """Structured log payload for one incremental batch."""

    fetched: int
    in_window: int
    extracted: int
    inserted: int
    next_offset_id: int
    past_window: bool


_SEARCH_BATCH_RETRY = object()
_SEARCH_BATCH_STOP = object()
_INCREMENTAL_BATCH_CONTINUE = object()
_INCREMENTAL_BATCH_BREAK = object()
_INCREMENTAL_BATCH_RETURN = object()


class _SearchEntityLike(Protocol):
    id: int
    first_name: str | None
    last_name: str | None
    title: str | None
    username: str | None


class _SearchMessageLike(Protocol):
    id: int
    date: datetime | None


class _SearchResultLike(Protocol):
    users: Sequence[_SearchEntityLike] | None
    chats: Sequence[_SearchEntityLike] | None
    messages: Sequence[_SearchMessageLike] | None
    count: int | None


_SyncStateRow = tuple[str, str | None]
_DialogStateRow = tuple[int | None, int | None, int | None, str | None, int | None, str | None, int | None, str | None]


def _load_state(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = cast(list[_SyncStateRow], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    return dict(rows)


def _validate_status_now(now: float) -> None:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite non-negative timestamp")


@contextmanager
def _archive_demand_scope(kind: DemandKind) -> Iterator[None]:
    """Install an exact archive operation kind for legacy direct execution."""
    try:
        token = current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(kind):
            yield
        return
    if token.kind is not kind:
        raise RuntimeError(f"active demand kind {token.kind.value} cannot execute {kind.value}")
    yield


def _set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
            (key, value),
        )


def _stamp_last_sync_at(conn: sqlite3.Connection) -> None:
    """Record the sync completion timestamp in activity_sync_state."""
    _set_state(conn, "last_sync_at", str(int(time.time())))


def _finish_incremental_slice(conn: sqlite3.Connection) -> None:
    """Atomically finish one incremental window and publish its next cadence anchor."""
    with conn:
        conn.execute(
            "DELETE FROM activity_sync_state WHERE key IN (?, ?)",
            (_INCREMENTAL_MIN_DATE_KEY, _INCREMENTAL_OFFSET_ID_KEY),
        )
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('last_sync_at', ?)",
            (str(int(time.time())),),
        )


def _normalize(text: str | None) -> str | None:
    """Match the name_normalized convention used elsewhere (lower + strip)."""
    if not text:
        return None
    return text.strip().lower() or None


def _classify_entity(obj: object) -> str | None:
    """Infer entities.type from a Telethon object via the single source of truth.

    Returns the canonical DialogType value string, or None for an unclassifiable
    object. (Previously this independently mapped megagroup -> 'group', which
    diverged from dialogs.type's 'supergroup' — classify_dialog_type fixes that.)
    """
    dt = classify_dialog_type(obj)
    return None if dt is DialogType.UNKNOWN else dt.value


def _optional_entity_attr(obj: object, attr: str) -> str | None:
    value = getattr(obj, attr, None)
    return value if isinstance(value, str) and value else None


def _upsert_entities_from_search(conn: sqlite3.Connection, result: _SearchResultLike) -> None:
    """Upsert users/chats from SearchRequest response into entities table.

    Uses the FULL column set (id, type, name, username, name_normalized, updated_at).
    `type` and `updated_at` are NOT NULL with no DEFAULT — both MUST be supplied
    on every row or the INSERT will fail.
    """
    from telethon.utils import get_peer_id

    now = int(time.time())
    snapshots: list[EntitySnapshot] = []

    for u in result.users or ():
        etype = _classify_entity(u)
        if etype is None:
            continue
        first_name = _optional_entity_attr(u, "first_name")
        last_name = _optional_entity_attr(u, "last_name")
        username = _optional_entity_attr(u, "username")
        name = " ".join(p for p in (first_name, last_name) if p) or username
        snapshots.append(
            EntitySnapshot(
                entity_id=int(u.id),
                entity_type=etype,
                name=name,
                username=username,
                name_normalized=_normalize(name),
                updated_at=now,
            )
        )

    for c in result.chats or ():
        etype = _classify_entity(c)
        if etype is None:
            continue
        try:
            pid = int(cast(int | str, get_peer_id(c)))  # yields -100XXXXX for Channel
        except TypeError:
            continue
        name = _optional_entity_attr(c, "title")
        username = _optional_entity_attr(c, "username")
        snapshots.append(
            EntitySnapshot(
                entity_id=pid,
                entity_type=etype,
                name=name,
                username=username,
                name_normalized=_normalize(name),
                updated_at=now,
            )
        )

    if not snapshots:
        return
    with conn:
        upsert_entity_snapshots(conn, snapshots)


def _fmt_duration(seconds: int) -> str:
    if seconds < _SECONDS_PER_MINUTE:
        return f"{seconds}s"
    if seconds < _SECONDS_PER_HOUR:
        return f"{seconds // _SECONDS_PER_MINUTE}m{seconds % _SECONDS_PER_MINUTE:02d}s"
    return f"{seconds // _SECONDS_PER_HOUR}h{(seconds % _SECONDS_PER_HOUR) // _SECONDS_PER_MINUTE:02d}m"


async def _wait_for_shutdown(shutdown_event: asyncio.Event, timeout: float) -> bool:
    """Sleep until shutdown or timeout; return True when shutdown fired."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=timeout)
        return True
    except TimeoutError:
        return False


def _extract_own_message_rows(batch: Sequence[_SearchMessageLike]) -> list[ExtractedMessage]:
    """Extract canonical own-message rows from a Telegram batch."""
    extracted: list[ExtractedMessage] = []
    for m in batch:
        dialog_id = extract_dialog_id(m)
        if dialog_id is None:
            continue
        extracted.append(extract_message_row(dialog_id, m))
    return extracted


def _persist_own_message_rows(
    conn: sqlite3.Connection,
    extracted: list[ExtractedMessage],
    *,
    priority: HydrationPriority,
) -> None:
    """Persist extracted own-message rows and enroll their dialogs."""
    if not extracted:
        return
    with conn:
        insert_messages_with_fts(conn, extracted, priority=priority)
        dialog_ids = {em.message.dialog_id for em in extracted}
        for dialog_id in dialog_ids:
            enroll_own_only_sync_dialog(conn, dialog_id)


async def _search_backfill_batch(
    client: ActivityClient,
    checkpoint: int,
    shutdown_event: asyncio.Event,
    *,
    total_fetched: int,
    timeout_s: float,
) -> object:
    """Run the backfill SearchRequest and translate control-flow exceptions."""
    try:
        with _archive_demand_scope(DemandKind.ARCHIVE_BACKFILL):
            with rpc_scope(
                TelegramRpcSource.ACTIVITY_ARCHIVE,
                timeout_seconds=timeout_s,
                acquisition_kind=AcquisitionKind.MESSAGE_SEARCH_PAGE,
            ):
                return await call_with_timeout(
                    client,
                    SearchRequest(
                        peer=InputPeerEmpty(),
                        q="",
                        filter=InputMessagesFilterEmpty(),
                        min_date=None,
                        max_date=None,
                        offset_id=checkpoint,
                        add_offset=0,
                        limit=_BACKFILL_BATCH_LIMIT,
                        max_id=0,
                        min_id=0,
                        hash=0,
                        from_id=InputPeerSelf(),
                    ),
                    timeout_s=timeout_s,
                )
    except TelegramRpcThrottled as exc:
        logger.warning(
            "activity_sync_floodwait seconds=%s total_fetched=%d",
            exc.retry_after_seconds,
            total_fetched,
        )
        if exc.retry_after_seconds is None:
            return _SEARCH_BATCH_STOP
        if await sleep_through_flood(shutdown_event, exc.retry_after_seconds):
            return _SEARCH_BATCH_STOP
        return _SEARCH_BATCH_RETRY
    except RpcAdmissionClosedError:
        raise
    except TimeoutError:
        logger.warning(
            "activity_sync_backfill_rpc_timeout offset_id=%d total_fetched=%d",
            checkpoint,
            total_fetched,
        )
        return _SEARCH_BATCH_STOP


async def _search_incremental_batch(  # noqa: PLR0913 - explicit worker state and injected transport policy
    client: ActivityClient,
    min_date: int,
    offset_id: int,
    shutdown_event: asyncio.Event,
    *,
    inserted: int,
    timeout_s: float,
) -> object:
    """Run the incremental SearchRequest and translate control-flow exceptions."""
    try:
        with _archive_demand_scope(DemandKind.ARCHIVE_INCREMENTAL):
            with rpc_scope(
                TelegramRpcSource.ACTIVITY_ARCHIVE,
                timeout_seconds=timeout_s,
                acquisition_kind=AcquisitionKind.MESSAGE_SEARCH_PAGE,
            ):
                return await call_with_timeout(
                    client,
                    SearchRequest(
                        peer=InputPeerEmpty(),
                        q="",
                        filter=InputMessagesFilterEmpty(),
                        min_date=datetime.fromtimestamp(min_date, tz=UTC),
                        max_date=None,
                        offset_id=offset_id,
                        add_offset=0,
                        limit=_BACKFILL_BATCH_LIMIT,
                        max_id=0,
                        min_id=0,
                        hash=0,
                        from_id=InputPeerSelf(),
                    ),
                    timeout_s=timeout_s,
                )
    except TelegramRpcThrottled as exc:
        logger.warning("activity_sync_incremental_floodwait seconds=%s", exc.retry_after_seconds)
        if exc.retry_after_seconds is None:
            return _SEARCH_BATCH_STOP
        if await sleep_through_flood(shutdown_event, exc.retry_after_seconds):
            return _SEARCH_BATCH_STOP
        return _SEARCH_BATCH_RETRY
    except RpcAdmissionClosedError:
        raise
    except TimeoutError:
        logger.warning("activity_sync_rpc_timeout offset_id=%d inserted=%d", offset_id, inserted)
        return _SEARCH_BATCH_STOP


def _trim_incremental_batch(
    batch: Sequence[_SearchMessageLike], min_date: int
) -> tuple[list[_SearchMessageLike], bool]:
    """Apply the client-side min_date filter used by the incremental loop."""
    in_window: list[_SearchMessageLike] = []
    past_window = False
    for m in batch:
        m_ts = int(m.date.timestamp()) if m.date is not None else 0
        if m_ts >= min_date:
            in_window.append(m)
        else:
            past_window = True
            break
    return in_window, past_window


def _log_backfill_batch(progress: _BackfillState, fetched: int, batch_duration_s: float) -> None:
    """Emit the per-batch backfill progress log."""
    pass_elapsed_s = time.monotonic() - progress.loop_start
    rate = progress.total_fetched / pass_elapsed_s if pass_elapsed_s > 0 else 0.0
    if progress.total_known is not None:
        remaining = progress.total_known - progress.total_fetched
        eta_s = int(remaining / rate) if rate > 0 else None
        eta_str = _fmt_duration(eta_s) if eta_s is not None else "?"
        logger.info(
            "activity_sync_backfill_batch batch=%d fetched=%d total=%d/%d rate=%.0f/s eta=%s"
            " offset_id=%d batch_duration_s=%.3f pass_elapsed_s=%.3f next_sleep_s=%.3f",
            progress.batch_num,
            fetched,
            progress.total_fetched,
            progress.total_known,
            rate,
            eta_str,
            progress.checkpoint,
            batch_duration_s,
            pass_elapsed_s,
            _PACING.search.batch_s,
        )
        return
    logger.info(
        "activity_sync_backfill_batch batch=%d fetched=%d total=%d rate=%.0f/s offset_id=%d"
        " batch_duration_s=%.3f pass_elapsed_s=%.3f next_sleep_s=%.3f",
        progress.batch_num,
        fetched,
        progress.total_fetched,
        rate,
        progress.checkpoint,
        batch_duration_s,
        pass_elapsed_s,
        _PACING.search.batch_s,
    )


def _log_incremental_batch(
    progress: _IncrementalState, batch_log: _IncrementalBatchLog, batch_duration_s: float
) -> None:
    """Emit the per-batch incremental progress log."""
    logger.debug(
        "activity_sync_incremental_batch batch=%d fetched=%d in_window=%d "
        "extracted=%d total_inserted=%d next_offset_id=%d past_window=%s"
        " batch_duration_s=%.3f pass_elapsed_s=%.3f next_sleep_s=%.3f",
        progress.batch_num,
        batch_log.fetched,
        batch_log.in_window,
        batch_log.extracted,
        batch_log.inserted,
        batch_log.next_offset_id,
        batch_log.past_window,
        batch_duration_s,
        time.monotonic() - progress.loop_start,
        _PACING.search.batch_s,
    )


def _prepare_incremental_slice(
    conn: sqlite3.Connection,
    state: dict[str, str | None],
    shutdown_event: asyncio.Event,
) -> tuple[int, int] | None:
    """Load and, for a new window, durably initialize incremental state."""
    if shutdown_event.is_set() or state.get("backfill_complete") != "1":
        return None
    last_sync_at = int(state.get("last_sync_at") or 0)
    if last_sync_at == 0:
        return None

    raw_min_date = state.get(_INCREMENTAL_MIN_DATE_KEY)
    min_date = int(raw_min_date) if raw_min_date is not None else max(0, last_sync_at - 60)
    offset_id = int(state.get(_INCREMENTAL_OFFSET_ID_KEY) or 0)
    if raw_min_date is None:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
                (_INCREMENTAL_MIN_DATE_KEY, str(min_date)),
            )
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, '0')",
                (_INCREMENTAL_OFFSET_ID_KEY,),
            )
    return min_date, offset_id


def _commit_incremental_slice_result(
    conn: sqlite3.Connection,
    search_result: _SearchResultLike,
    min_date: int,
    batch_started_at: float,
) -> None:
    """Persist one incremental page and its restart-safe continuation."""
    batch = list(search_result.messages or [])
    if not batch:
        _finish_incremental_slice(conn)
        return

    in_window, past_window = _trim_incremental_batch(batch, min_date)
    extracted = _extract_own_message_rows(in_window)
    _persist_own_message_rows(conn, extracted, priority=HydrationPriority.FOREGROUND)
    _upsert_entities_from_search(conn, search_result)
    next_offset_id = min(message.id for message in batch)
    _log_incremental_batch(
        _IncrementalState(
            min_date=min_date,
            inserted=len(in_window),
            batch_num=1,
            offset_id=next_offset_id,
            loop_start=batch_started_at,
        ),
        _IncrementalBatchLog(
            fetched=len(batch),
            in_window=len(in_window),
            extracted=len(extracted),
            inserted=len(in_window),
            next_offset_id=next_offset_id,
            past_window=past_window,
        ),
        time.monotonic() - batch_started_at,
    )
    if past_window:
        _finish_incremental_slice(conn)
        return
    _set_state(conn, _INCREMENTAL_OFFSET_ID_KEY, str(next_offset_id))


async def _run_backfill_slice(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    timeout_s: float,
) -> None:
    """Fetch one restart-safe archive-backfill page."""
    state = _load_state(conn)
    if shutdown_event.is_set() or state.get("backfill_complete") == "1":
        return
    checkpoint = int(state.get("backfill_offset_id") or 0)
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('backfill_started_at', ?)",
            (str(int(time.time())),),
        )
    batch_started_at = time.monotonic()
    result = await _search_backfill_batch(
        client,
        checkpoint,
        shutdown_event,
        total_fetched=0,
        timeout_s=timeout_s,
    )
    if result is _SEARCH_BATCH_STOP or result is _SEARCH_BATCH_RETRY:
        return
    search_result = cast(_SearchResultLike, result)
    batch = list(search_result.messages or [])
    if not batch:
        with conn:
            conn.execute("INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('backfill_complete', '1')")
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('last_sync_at', ?)",
                (str(int(time.time())),),
            )
        return
    extracted = _extract_own_message_rows(batch)
    _persist_own_message_rows(conn, extracted, priority=HydrationPriority.BACKFILL)
    _upsert_entities_from_search(conn, search_result)
    checkpoint = min(message.id for message in batch)
    _set_state(conn, "backfill_offset_id", str(checkpoint))
    _log_backfill_batch(
        _BackfillState(
            checkpoint=checkpoint,
            total_fetched=len(batch),
            batch_num=1,
            loop_start=batch_started_at,
        ),
        len(batch),
        time.monotonic() - batch_started_at,
    )


async def _run_incremental_slice(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    timeout_s: float,
) -> None:
    """Fetch one restart-safe page from the current incremental window."""
    state = _load_state(conn)
    window = _prepare_incremental_slice(conn, state, shutdown_event)
    if window is None:
        return
    min_date, offset_id = window

    batch_started_at = time.monotonic()
    result = await _search_incremental_batch(
        client,
        min_date,
        offset_id,
        shutdown_event,
        inserted=0,
        timeout_s=timeout_s,
    )
    if result is _SEARCH_BATCH_RETRY:
        return
    if result is _SEARCH_BATCH_STOP:
        _finish_incremental_slice(conn)
        return
    _commit_incremental_slice_result(conn, cast(_SearchResultLike, result), min_date, batch_started_at)


async def _run_backfill(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    timeout_s: float,
) -> None:
    """Run the legacy archive backfill under its exact operation root."""
    with _archive_demand_scope(DemandKind.ARCHIVE_BACKFILL):
        await _run_backfill_in_scope(client, conn, shutdown_event, timeout_s=timeout_s)


async def _run_backfill_in_scope(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    timeout_s: float,
) -> None:
    state = _load_state(conn)
    if state.get("backfill_complete") == "1":
        logger.debug("activity_sync_backfill_skip reason=already_complete")
        return

    progress = _BackfillState(
        checkpoint=int(state.get("backfill_offset_id") or 0),
        loop_start=time.monotonic(),
    )

    # Mark that backfill has started so scan_status can distinguish
    # "never touched" from "running but not yet done".
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('backfill_started_at', ?)",
            (str(int(time.time())),),
        )

    logger.info("activity_sync_backfill_start offset_id=%d", progress.checkpoint)

    while not shutdown_event.is_set():
        if not await _run_backfill_batch(client, conn, shutdown_event, progress, timeout_s=timeout_s):
            return


async def _run_backfill_batch(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    progress: _BackfillState,
    *,
    timeout_s: float,
) -> bool:
    """Fetch, commit, and pace one legacy backfill iteration."""
    batch_started_at = time.monotonic()
    result = await _search_backfill_batch(
        client,
        progress.checkpoint,
        shutdown_event,
        total_fetched=progress.total_fetched,
        timeout_s=timeout_s,
    )
    if result is _SEARCH_BATCH_STOP:
        return False
    if result is _SEARCH_BATCH_RETRY:
        return True
    if not _commit_backfill_result(conn, progress, cast(_SearchResultLike, result), batch_started_at):
        return False
    return not await _wait_for_shutdown(shutdown_event, timeout=_PACING.search.batch_s)


def _commit_backfill_result(
    conn: sqlite3.Connection,
    progress: _BackfillState,
    search_result: _SearchResultLike,
    batch_started_at: float,
) -> bool:
    """Persist one backfill page, returning whether the pass should continue."""
    batch = list(search_result.messages or [])
    if progress.total_known is None:
        progress.total_known = cast(int | None, getattr(search_result, "count", None))
        if progress.total_known is not None:
            logger.info("activity_sync_backfill_total total=%d", progress.total_known)

    if not batch:
        _set_state(conn, "backfill_complete", "1")
        _stamp_last_sync_at(conn)
        logger.info(
            "activity_sync_backfill_complete total_fetched=%d batches=%d duration_s=%.3f",
            progress.total_fetched,
            progress.batch_num,
            time.monotonic() - progress.loop_start,
        )
        return False

    progress.batch_num += 1
    extracted = _extract_own_message_rows(batch)
    _persist_own_message_rows(conn, extracted, priority=HydrationPriority.BACKFILL)
    _upsert_entities_from_search(conn, search_result)
    progress.total_fetched += len(batch)
    progress.checkpoint = min(m.id for m in batch)
    _set_state(conn, "backfill_offset_id", str(progress.checkpoint))
    _log_backfill_batch(progress, len(batch), time.monotonic() - batch_started_at)
    return True


async def _run_incremental(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    timeout_s: float,
) -> None:
    """Run the legacy incremental archive under its exact operation root."""
    with _archive_demand_scope(DemandKind.ARCHIVE_INCREMENTAL):
        await _run_incremental_in_scope(client, conn, shutdown_event, timeout_s=timeout_s)


async def _run_incremental_in_scope(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    timeout_s: float,
) -> None:
    state = _load_state(conn)
    if state.get("backfill_complete") != "1":
        return

    # Anchor by timestamp, not per-chat message_id. Global SearchRequest with
    # InputPeerEmpty returns messages from many dialogs — each with its own
    # message_id sequence. Using min_id=MAX(message_id) across chats causes
    # newer messages in dialogs with lower per-chat IDs to be silently skipped.
    # min_date is a wall-clock filter applied uniformly across all dialogs.
    last_sync_at = int(state.get("last_sync_at") or 0)
    if last_sync_at == 0:
        return

    # 60-second buffer guards against messages at the exact boundary being
    # missed when the previous sync finished mid-second.
    progress = _IncrementalState(min_date=max(0, last_sync_at - 60), loop_start=time.monotonic())
    logger.debug(
        "activity_sync_incremental_start min_date=%d window_s=%d",
        progress.min_date,
        int(time.time()) - progress.min_date,
    )

    while not shutdown_event.is_set():
        outcome = await _run_incremental_batch(client, conn, shutdown_event, progress, timeout_s=timeout_s)
        if outcome is _INCREMENTAL_BATCH_CONTINUE:
            continue
        if outcome is _INCREMENTAL_BATCH_RETURN:
            return
        break

    _stamp_last_sync_at(conn)
    logger.debug(
        "activity_sync_incremental_done batches=%d inserted=%d duration_s=%.3f",
        progress.batch_num,
        progress.inserted,
        time.monotonic() - progress.loop_start,
    )


async def _run_incremental_batch(
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    progress: _IncrementalState,
    *,
    timeout_s: float,
) -> object:
    """Fetch, commit, and pace one legacy incremental iteration."""
    batch_started_at = time.monotonic()
    result = await _search_incremental_batch(
        client,
        progress.min_date,
        progress.offset_id,
        shutdown_event,
        inserted=progress.inserted,
        timeout_s=timeout_s,
    )
    if result is _SEARCH_BATCH_STOP:
        _stamp_last_sync_at(conn)
        return _INCREMENTAL_BATCH_BREAK
    if result is _SEARCH_BATCH_RETRY:
        return _INCREMENTAL_BATCH_CONTINUE

    search_result = cast(_SearchResultLike, result)
    batch = list(search_result.messages or [])
    if not batch:
        return _INCREMENTAL_BATCH_BREAK

    # messages.search(InputPeerEmpty) silently ignores min_date — canonical
    # Telegram-API behavior, see Telethon #218. Apply the date bound
    # client-side. Batch is ordered newest-first by offset_id, so dates
    # are monotonically decreasing: once we hit one older than min_date,
    # every later batch will be older too — break the outer loop.
    in_window, past_window = _trim_incremental_batch(batch, progress.min_date)
    extracted = _extract_own_message_rows(in_window)
    _persist_own_message_rows(conn, extracted, priority=HydrationPriority.FOREGROUND)
    _upsert_entities_from_search(conn, search_result)
    progress.inserted += len(in_window)
    progress.batch_num += 1
    # Always advance offset_id by the full batch — even messages outside
    # the window must be skipped past so we don't re-fetch them.
    progress.offset_id = min(m.id for m in batch)

    # last_sync_at is stamped once at end-of-loop, not per batch:
    # with the client-side min_date filter the loop terminates within
    # a few iterations anyway, and a mid-loop shutdown just means the
    # next incremental re-fetches the in-progress window (UPSERT no-op).
    _log_incremental_batch(
        progress,
        _IncrementalBatchLog(
            fetched=len(batch),
            in_window=len(in_window),
            extracted=len(extracted),
            inserted=progress.inserted,
            next_offset_id=progress.offset_id,
            past_window=past_window,
        ),
        time.monotonic() - batch_started_at,
    )
    if past_window:
        return _INCREMENTAL_BATCH_BREAK
    if await _wait_for_shutdown(shutdown_event, timeout=_PACING.search.batch_s):
        return _INCREMENTAL_BATCH_RETURN
    return _INCREMENTAL_BATCH_CONTINUE


async def run_activity_sync_loop(  # noqa: PLR0913 - explicit loop dependencies and observation hook
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_INTERVAL_S,
    timeout_s: float,
    demand_cycle_runner: DemandCycleRunner | None = None,
) -> None:
    """Background task: keep own-message rows (out=1) in messages up-to-date.

    One pass = (backfill if incomplete) + (incremental if backfill complete).
    Sleeps `interval` between passes, interruptible via shutdown_event.
    """
    while not shutdown_event.is_set():
        logger.debug("activity_sync_loop_start")
        try:
            if demand_cycle_runner is None:
                await _run_backfill(client, conn, shutdown_event, timeout_s=timeout_s)
                await _run_incremental(client, conn, shutdown_event, timeout_s=timeout_s)
            else:
                await demand_cycle_runner(
                    DemandKind.ARCHIVE_BACKFILL,
                    lambda: _run_backfill_in_scope(client, conn, shutdown_event, timeout_s=timeout_s),
                )
                await demand_cycle_runner(
                    DemandKind.ARCHIVE_INCREMENTAL,
                    lambda: _run_incremental_in_scope(client, conn, shutdown_event, timeout_s=timeout_s),
                )
        except RpcAdmissionClosedError:
            raise
        except Exception:
            logger.warning("activity_sync_error", exc_info=True)
        logger.debug("activity_sync_loop_sleeping interval=%.0fs", interval)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
