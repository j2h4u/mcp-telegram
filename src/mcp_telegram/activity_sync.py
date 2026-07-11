"""Global own-message archive worker.

Populates own-message rows (out=1) in the unified messages table
via messages.Search(InputPeerEmpty, from_id=InputPeerSelf).
Runs as a named daemon background task alongside run_access_probe_loop.
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import InputMessagesFilterEmpty, InputPeerEmpty, InputPeerSelf

from .flood import flood_seconds, sleep_through_flood
from .models import DialogType
from .sync_worker import (
    ExtractedMessage,
    extract_message_row,
    insert_messages_with_fts,
)
from .telethon_dialog import classify_dialog_type

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 3600.0
_BACKFILL_BATCH_LIMIT = 100
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 60 * _SECONDS_PER_MINUTE


@dataclass(frozen=True, slots=True)
class ActivitySyncSearchPacing:
    batch_s: float = 0.5


@dataclass(frozen=True, slots=True)
class ActivitySyncPacing:
    search: ActivitySyncSearchPacing = ActivitySyncSearchPacing()


_PACING = ActivitySyncPacing()


# Upper bound on a single SearchRequest await. Prevents a wedged MTProto
# socket after startup FloodWait from hanging the incremental loop
# indefinitely (D-02 expert panel).
_SEARCH_RPC_TIMEOUT_S: float = 120.0

# Mirrors sync_worker.UPSERT_ENTITY_SQL (verified line 239).
# NOTE: `type` and `updated_at` are NOT NULL with no DEFAULT — both MUST be supplied.
UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
)

# Activity-sync dialog enrollment: 'own_only' is the lowest non-empty
# coverage status (D-2). INSERT OR IGNORE preserves higher-status rows
# already enrolled by FullSyncWorker (syncing/synced) or probe loops
# (fragment/access_lost). Status only escalates.
INSERT_OWN_ONLY_DIALOG_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'own_only')"


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


class _ActivityClient(Protocol):
    def __call__(self, request: object) -> Coroutine[object, object, object]: ...

    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]: ...


class _HasPeerIdLike(Protocol):
    peer_id: object | None


class _SearchEntityLike(Protocol):
    id: int
    first_name: str | None
    last_name: str | None
    title: str | None
    username: str | None


class _SearchMessageLike(_HasPeerIdLike, Protocol):
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


def _set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
            (key, value),
        )


def _stamp_last_sync_at(conn: sqlite3.Connection) -> None:
    """Record the sync completion timestamp in activity_sync_state."""
    _set_state(conn, "last_sync_at", str(int(time.time())))


def extract_dialog_id(msg: _HasPeerIdLike) -> int | None:
    """Resolve dialog_id from a Telethon message's peer_id field.

    Public name so activity_peer_sweep can import it without relying on the
    module-private underscore convention.
    """
    peer = msg.peer_id
    if peer is None:
        return None
    # telethon.utils.get_peer_id handles User/Chat/Channel variants
    from telethon.utils import get_peer_id

    try:
        return int(cast(int | str, get_peer_id(peer)))
    except Exception:
        logger.warning("activity_sync_peer_id_unresolvable", exc_info=True)
        return None


# Backward-compat alias for callers that used the private name.
_extract_dialog_id = extract_dialog_id


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
    rows: list[tuple[int, str, str | None, str | None, str | None, int]] = []

    for u in result.users or ():
        etype = _classify_entity(u)
        if etype is None:
            continue
        first_name = _optional_entity_attr(u, "first_name")
        last_name = _optional_entity_attr(u, "last_name")
        username = _optional_entity_attr(u, "username")
        name = " ".join(p for p in (first_name, last_name) if p) or username
        rows.append((int(u.id), etype, name, username, _normalize(name), now))

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
        rows.append((pid, etype, name, username, _normalize(name), now))

    if not rows:
        return
    with conn:
        conn.executemany(UPSERT_ENTITY_SQL, rows)


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
        dialog_id = _extract_dialog_id(m)
        if dialog_id is None:
            continue
        extracted.append(extract_message_row(dialog_id, m))
    return extracted


def _persist_own_message_rows(conn: sqlite3.Connection, extracted: list[ExtractedMessage]) -> None:
    """Persist extracted own-message rows and enroll their dialogs."""
    if not extracted:
        return
    with conn:
        insert_messages_with_fts(conn, extracted)
        dialog_ids = {em.message.dialog_id for em in extracted}
        conn.executemany(
            INSERT_OWN_ONLY_DIALOG_SQL,
            [(did,) for did in dialog_ids],
        )


async def _search_backfill_batch(
    client: _ActivityClient,
    checkpoint: int,
    shutdown_event: asyncio.Event,
    *,
    total_fetched: int,
) -> object:
    """Run the backfill SearchRequest and translate control-flow exceptions."""
    try:
        return await _call_with_timeout(
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
        )
    except FloodWaitError as exc:
        logger.warning(
            "activity_sync_floodwait seconds=%d total_fetched=%d",
            exc.seconds,
            total_fetched,
        )
        if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
            return _SEARCH_BATCH_STOP
        return _SEARCH_BATCH_RETRY
    except TimeoutError:
        logger.warning(
            "activity_sync_backfill_rpc_timeout offset_id=%d total_fetched=%d",
            checkpoint,
            total_fetched,
        )
        return _SEARCH_BATCH_STOP


async def _search_incremental_batch(
    client: _ActivityClient,
    min_date: int,
    offset_id: int,
    shutdown_event: asyncio.Event,
    *,
    inserted: int,
) -> object:
    """Run the incremental SearchRequest and translate control-flow exceptions."""
    try:
        return await _call_with_timeout(
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
        )
    except FloodWaitError as exc:
        logger.warning("activity_sync_incremental_floodwait seconds=%d", exc.seconds)
        if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
            return _SEARCH_BATCH_STOP
        return _SEARCH_BATCH_RETRY
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
    logger.info(
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


async def call_with_timeout(client: _ActivityClient, request: object) -> object:
    """Invoke a Telethon RPC with a hard timeout and abandon on overrun.

    asyncio.wait_for awaits the wrapped task to actually finish after
    cancellation. Telethon's MTProto futures sometimes never resolve
    (post-startup flood wait corridor), causing wait_for to hang
    indefinitely past the requested deadline.

    asyncio.wait + explicit cancel returns immediately on timeout;
    the abandoned task is left to complete (or be GC'd) in the background
    rather than blocking our control flow.

    Raises TimeoutError on overrun. Re-raises FloodWaitError and other
    exceptions surfaced by the RPC call.

    Public name so activity_peer_sweep can import it without relying on the
    module-private underscore convention.
    """
    task = asyncio.create_task(client(request))
    done, _pending = await asyncio.wait({task}, timeout=_SEARCH_RPC_TIMEOUT_S)
    if not done:
        task.cancel()
        raise TimeoutError(f"RPC exceeded {_SEARCH_RPC_TIMEOUT_S}s deadline")
    return task.result()


# Backward-compat alias for callers that used the private name.
_call_with_timeout = call_with_timeout


async def _run_backfill(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
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
        batch_started_at = time.monotonic()
        result = await _search_backfill_batch(
            client,
            progress.checkpoint,
            shutdown_event,
            total_fetched=progress.total_fetched,
        )
        if result is _SEARCH_BATCH_STOP:
            return
        if result is _SEARCH_BATCH_RETRY:
            continue

        search_result = cast(_SearchResultLike, result)
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
            return

        progress.batch_num += 1
        extracted = _extract_own_message_rows(batch)
        _persist_own_message_rows(conn, extracted)

        _upsert_entities_from_search(conn, search_result)

        progress.total_fetched += len(batch)
        progress.checkpoint = min(m.id for m in batch)
        _set_state(conn, "backfill_offset_id", str(progress.checkpoint))
        _log_backfill_batch(progress, len(batch), time.monotonic() - batch_started_at)

        if await _wait_for_shutdown(shutdown_event, timeout=_PACING.search.batch_s):
            return


async def _run_incremental(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
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
    logger.info(
        "activity_sync_incremental_start min_date=%d window_s=%d",
        progress.min_date,
        int(time.time()) - progress.min_date,
    )

    while not shutdown_event.is_set():
        batch_started_at = time.monotonic()
        result = await _search_incremental_batch(
            client,
            progress.min_date,
            progress.offset_id,
            shutdown_event,
            inserted=progress.inserted,
        )
        if result is _SEARCH_BATCH_STOP:
            _stamp_last_sync_at(conn)
            break
        if result is _SEARCH_BATCH_RETRY:
            continue

        search_result = cast(_SearchResultLike, result)
        batch = list(search_result.messages or [])
        if not batch:
            break

        # messages.search(InputPeerEmpty) silently ignores min_date — canonical
        # Telegram-API behavior, see Telethon #218. Apply the date bound
        # client-side. Batch is ordered newest-first by offset_id, so dates
        # are monotonically decreasing: once we hit one older than min_date,
        # every later batch will be older too — break the outer loop.
        in_window, past_window = _trim_incremental_batch(batch, progress.min_date)
        extracted = _extract_own_message_rows(in_window)
        _persist_own_message_rows(conn, extracted)

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
            break

        if await _wait_for_shutdown(shutdown_event, timeout=_PACING.search.batch_s):
            return

    _stamp_last_sync_at(conn)
    logger.info(
        "activity_sync_incremental_done batches=%d inserted=%d duration_s=%.3f",
        progress.batch_num,
        progress.inserted,
        time.monotonic() - progress.loop_start,
    )


async def run_activity_sync_loop(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_INTERVAL_S,
) -> None:
    """Background task: keep own-message rows (out=1) in messages up-to-date.

    One pass = (backfill if incomplete) + (incremental if backfill complete).
    Sleeps `interval` between passes, interruptible via shutdown_event.
    """
    while not shutdown_event.is_set():
        logger.info("activity_sync_loop_start")
        try:
            await _run_backfill(client, conn, shutdown_event)
            await _run_incremental(client, conn, shutdown_event)
        except Exception:
            logger.warning("activity_sync_error", exc_info=True)
        logger.info("activity_sync_loop_sleeping interval=%.0fs", interval)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
