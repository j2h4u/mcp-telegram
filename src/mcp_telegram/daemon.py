"""Sync daemon — long-running process that exclusively owns the TelegramClient.

Started via ``mcp-telegram sync``. Connects to Telegram, ensures sync.db schema,
bootstraps DM dialogs, then runs FullSyncWorker in a tight batch loop with
periodic heartbeat logging and clean SIGTERM handling.

Architecture:
- sync-daemon is the sole owner of TelegramClient — connects once, holds it.
- MCP server runs separately with disable_telegram_session() active and reads
  sync.db via open_sync_db_reader(); it never calls client.connect().
- SIGTERM triggers shutdown_event (set by register_shutdown_handler), which
  checkpoints WAL and closes the DB connection before the daemon disconnects.

Event handlers:
- EventHandlerManager is registered BEFORE Telegram connect() so Telethon
  catch_up=True replays missed updates into live handlers, not an empty handler
  set.  It also remains registered BEFORE FullSyncWorker starts so no real-time
  events are missed during initial bulk fetch.  INSERT OR REPLACE handles any
  overlap between real-time and bulk paths idempotently.
- synced_dialogs set is refreshed every heartbeat so newly enrolled dialogs
  are picked up within one interval without re-registering handlers.
- Weekly gap scan detects tombstoned DM messages that MTProto delete events
  cannot report.

Delta catch-up:
- connect() called with catch_up=True — Telethon replays missed updates via PTS
  on reconnect after handlers are already registered.
- DeltaSyncWorker.run_delta_catch_up() fills forward gaps for all 'synced'
  dialogs before bootstrap_dms() enrolls new ones.

Daemon API:
- DaemonAPIServer runs on a Unix socket alongside the sync loop, serving
  list_messages / search_messages / list_dialogs requests from MCP server.
- FTS backfill runs once at startup for messages without FTS index entries.
- Socket file cleaned up on shutdown (and stale file removed on startup).
"""

import asyncio
import logging
import math
import os
import sqlite3
import time
from collections.abc import Coroutine, Iterator, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Protocol, cast

from telethon import utils as telethon_utils  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetPeerDialogsRequest  # type: ignore[import-untyped]
from telethon.tl.functions.users import GetFullUserRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputDialogPeer,
    TypeInputDialogPeer,
    TypeInputPeer,
    TypeInputUser,
)

from .activity_cold_backfill import ColdBackfillPacing, run_cold_backfill_loop
from .activity_contracts import InputPeerResolver
from .activity_hot_sweep import run_hot_sweep_loop
from .activity_peer_resolve import resolve_input_peer
from .activity_substrate import ActivityClient
from .activity_sync import run_activity_sync_loop
from .config import McpTelegramConfig, SchedulingConfig, load_config, resolve_scheduling_config
from .daemon_api import DaemonApiPolicy, DaemonAPIServer, DaemonClientLike
from .delta_sync import (
    AccessProbePolicy,
    DeltaCatchUpPolicy,
    DeltaSyncWorker,
    _DeltaSyncClient,
    run_access_probe_loop,
    run_delta_catch_up_loop,
)
from .dialog_sync import DialogsBootstrapWorker, run_reconciliation_loop
from .event_handlers import EventHandlerManager
from .feedback_db import SQLiteFeedbackStore, ensure_feedback_schema
from .feedback_service import FeedbackApplicationService
from .flood import (
    FloodWaitKillSwitchPolicy,
    configure_flood_wait_kill_switch,
    flood_seconds,
    flood_wait_kill_switch_status,
    install_telethon_flood_wait_metrics_filter,
    maybe_log_flood_wait_rollup,
    sleep_through_flood,
)
from .folders.refresh import FolderRefresher
from .folders.sqlite_repository import SQLiteFolderSnapshotRepository
from .folders.telegram_adapter import FolderClient, TelethonTelegramFolderGateway
from .folders.worker import FolderProjectionWorker
from .fts import backfill_fts_index
from .message_fact_refresh import (
    MessageFactRefreshPolicy,
    run_message_fact_refresh_loop,
)
from .messages.sqlite_repository import insert_messages_with_fts
from .messages.telegram_adapter import extract_message_row
from .own_only import OwnOnlyContext, ensure_own_only_schema
from .reactions.refresh import ReactionFreshener
from .reactions.sqlite_repository import SQLiteReactionSnapshotRepository
from .reactions.telegram_adapter import TelethonTelegramReactionGateway
from .read_state import apply_read_cursor
from .scheduled_messages import ScheduledReconciliationPolicy, run_scheduled_reconciliation_loop
from .state import StatePaths, ensure_private_state_dir
from .sync_db import (
    _open_sync_db,
    ensure_sync_schema,
    migrate_legacy_databases,
    register_shutdown_handler,
)
from .sync_worker import FullSyncWorker
from .telegram import create_client
from .telegram_read_receipts import TelethonTelegramReadReceiptGateway
from .telegram_rpc import (
    GovernedTelegramClient,
    GovernedTelegramClientTarget,
    TelegramRpcBudget,
    TelegramRpcCircuitOpenError,
    TelegramRpcGovernor,
)
from .topics.refresh import TopicRefresher
from .topics.sqlite_repository import SQLiteTopicSnapshotRepository
from .topics.telegram_adapter import TelethonTelegramTopicGateway, TopicClient

logger = logging.getLogger(__name__)


class _DaemonClient(Protocol):
    def add_event_handler(self, _callback: object, _event: object) -> None: ...

    def remove_event_handler(self, _callback: object) -> None: ...

    def is_connected(self) -> bool: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def get_me(self) -> object: ...

    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def get_entity(self, _dialog_id: int) -> object: ...

    async def get_messages(self, *_args: object, **_kwargs: object) -> object: ...

    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class _ReadPositionDialogLike(Protocol):
    peer: object
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None


class _ReadPositionsResultLike(Protocol):
    dialogs: Sequence[_ReadPositionDialogLike]


class _MessagesTotalLike(Protocol):
    total: int | None


class _MeLike(Protocol):
    id: int


@dataclass(frozen=True, slots=True)
class DaemonHistoryPacing:
    backfill_skip_s: float = 1.0


@dataclass(frozen=True, slots=True)
class DaemonPacing:
    history: DaemonHistoryPacing = DaemonHistoryPacing()


_PACING = DaemonPacing()


HEARTBEAT_INTERVAL_S: float = 60.0
GAP_SCAN_INTERVAL_S: float = 7 * 24 * 3600.0
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 60 * SECONDS_PER_MINUTE

_UNSUPPORTED_TRANSCRIPTION_BACKFILL_BATCH_SIZE: int = 25
_UNSUPPORTED_TRANSCRIPTION_BACKFILL_LIMIT: int = 500
_UNSUPPORTED_MEDIA_DESCRIPTIONS = ("MessageMediaUnsupported", "[неподдерживаемый тип]")

_BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RPCError,
    sqlite3.DatabaseError,
    Exception,
)

_SELECT_NULL_TOTAL_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.total_messages IS NULL AND sd.status != 'not_synced'"
)

_UPDATE_TOTAL_SQL = "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ?"

_SELECT_NULL_READ_CURSORS_SQL = (
    # Phase 39.3-02: picks up dialogs with EITHER cursor NULL. Post-v12
    # migration, every existing synced row has read_outbox_max_id = NULL, so
    # this re-bootstraps all of them in batched GetPeerDialogsRequest calls.
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE (sd.read_inbox_max_id IS NULL OR sd.read_outbox_max_id IS NULL) "
    "AND sd.status = 'synced'"
)

_SELECT_BLANK_UNSUPPORTED_MESSAGES_SQL = (
    "SELECT m.dialog_id, m.message_id FROM messages m "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 "
    "WHERE COALESCE(m.text, '') = '' AND m.media_description IN (?, ?) "
    "ORDER BY m.dialog_id, m.message_id "
    "LIMIT ?"
)


@dataclass(slots=True)
class _SyncLoopState:
    sync_start: float
    last_heartbeat: float
    last_gap_scan: float
    last_hb_msg_count: int
    last_hb_mono: float
    was_idle: bool = False


@dataclass(slots=True)
class _SyncMainContext:
    db_path: Path
    conn: sqlite3.Connection
    feedback_conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    client: _DaemonClient
    reaction_freshener: ReactionFreshener
    message_fact_refresh_policy: MessageFactRefreshPolicy
    api_server: DaemonAPIServer
    topic_refresher: TopicRefresher
    folder_projection_worker: FolderProjectionWorker
    socket_path: Path
    unix_server: asyncio.AbstractServer | None = None
    handler_manager: EventHandlerManager | None = None
    own_only_context: OwnOnlyContext | None = None
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)
    background_tasks: set[asyncio.Task[object]] = field(default_factory=set)
    flood_wait_kill_switch_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True, slots=True)
class _BackfillTotalDialogResult:
    filled: int
    pause_after: bool
    stop: bool = False


async def _backfill_blank_unsupported_messages(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """Re-fetch blank unsupported media rows and materialize text when Telegram exposes it."""
    rows = cast(
        list[tuple[int, int]],
        conn.execute(
            _SELECT_BLANK_UNSUPPORTED_MESSAGES_SQL,
            (*_UNSUPPORTED_MEDIA_DESCRIPTIONS, _UNSUPPORTED_TRANSCRIPTION_BACKFILL_LIMIT),
        ).fetchall(),
    )
    if not rows:
        logger.info("backfill_blank_unsupported_messages — no rows, skipping")
        return 0

    filled = 0
    for dialog_id, message_ids in _group_message_ids_by_dialog(rows).items():
        if shutdown_event.is_set():
            break
        for chunk in _chunk_message_ids(message_ids):
            if shutdown_event.is_set():
                break
            result = await _backfill_blank_unsupported_chunk(client, conn, shutdown_event, dialog_id, chunk)
            filled += result.filled
            if result.stop:
                logger.info("backfill_blank_unsupported_messages filled=%d/%d", filled, len(rows))
                return filled
            if result.pause_after and not await _sleep_between_backfill_total_dialogs(shutdown_event):
                logger.info("backfill_blank_unsupported_messages filled=%d/%d", filled, len(rows))
                return filled

    logger.info("backfill_blank_unsupported_messages filled=%d/%d", filled, len(rows))
    return filled


def _group_message_ids_by_dialog(rows: Sequence[tuple[int, int]]) -> dict[int, list[int]]:
    grouped: dict[int, list[int]] = {}
    for dialog_id, message_id in rows:
        grouped.setdefault(dialog_id, []).append(message_id)
    return grouped


def _chunk_message_ids(message_ids: Sequence[int]) -> Iterator[list[int]]:
    for index in range(0, len(message_ids), _UNSUPPORTED_TRANSCRIPTION_BACKFILL_BATCH_SIZE):
        yield list(message_ids[index : index + _UNSUPPORTED_TRANSCRIPTION_BACKFILL_BATCH_SIZE])


async def _backfill_blank_unsupported_chunk(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    dialog_id: int,
    message_ids: Sequence[int],
) -> _BackfillTotalDialogResult:
    try:
        fetched = cast(Sequence[object], await client.get_messages(entity=dialog_id, ids=list(message_ids)))
    except FloodWaitError as exc:
        logger.warning("backfill_blank_unsupported flood_wait dialog_id=%d seconds=%d", dialog_id, exc.seconds)
        if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
            return _BackfillTotalDialogResult(filled=0, pause_after=False, stop=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=False)
    except _BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS as exc:
        logger.debug("backfill_blank_unsupported skip dialog_id=%d error=%s", dialog_id, exc)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)

    extracted = [extract_message_row(dialog_id, msg) for msg in fetched if msg is not None]
    materialized = [item for item in extracted if item.message.text]
    if not materialized:
        return _BackfillTotalDialogResult(filled=0, pause_after=True)

    with conn:
        enabled = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1",
                (dialog_id,),
            ).fetchone(),
        )
        if enabled is None:
            return _BackfillTotalDialogResult(filled=0, pause_after=True)
        insert_messages_with_fts(conn, materialized)
    return _BackfillTotalDialogResult(filled=len(materialized), pause_after=True)


async def _backfill_total_messages(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> int:
    """One-time sweep to populate total_messages for dialogs with NULL."""
    rows = cast(list[tuple[int]], conn.execute(_SELECT_NULL_TOTAL_SQL).fetchall())
    if not rows:
        logger.info("backfill_total_messages — no NULL rows, skipping")
        return 0

    filled = 0
    for (dialog_id,) in rows:
        if shutdown_event.is_set():
            break
        result = await _backfill_total_message_dialog(client, conn, shutdown_event, dialog_id)
        filled += result.filled
        if result.stop:
            break
        if result.pause_after and not await _sleep_between_backfill_total_dialogs(shutdown_event):
            break

    logger.info("backfill_total_messages filled=%d/%d", filled, len(rows))
    return filled


async def _backfill_total_message_dialog(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    dialog_id: int,
) -> _BackfillTotalDialogResult:
    """Fetch and persist one total_messages value, or handle a single skip/flood."""
    try:
        result = cast(_MessagesTotalLike, await client.get_messages(entity=dialog_id, limit=1))
        total = result.total
        if total is not None:
            with conn:
                conn.execute(
                    _UPDATE_TOTAL_SQL + " AND EXISTS (SELECT 1 FROM full_history_enrollment fhe "
                    "WHERE fhe.dialog_id = synced_dialogs.dialog_id AND fhe.enabled = 1)",
                    (total, dialog_id),
                )
            return _BackfillTotalDialogResult(filled=1, pause_after=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)
    except FloodWaitError as exc:
        logger.warning("backfill_total flood_wait dialog_id=%d seconds=%d", dialog_id, exc.seconds)
        if await sleep_through_flood(shutdown_event, flood_seconds(exc)):
            return _BackfillTotalDialogResult(filled=0, pause_after=False, stop=True)
        return _BackfillTotalDialogResult(filled=0, pause_after=False)
    except _BACKFILL_TOTAL_MESSAGES_SKIP_EXCEPTIONS as exc:
        logger.debug("backfill_total skip dialog_id=%d error=%s", dialog_id, exc)
        return _BackfillTotalDialogResult(filled=0, pause_after=True)


async def _sleep_between_backfill_total_dialogs(shutdown_event: asyncio.Event) -> bool:
    """Pause between backfill_total dialogs; return False when shutdown fires."""
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=_PACING.history.backfill_skip_s)
        return False
    except TimeoutError:
        return True


async def _initialize_read_positions(  # noqa: PLR0913
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    max_dialogs: int | None = None,
    failure_cooldown_seconds: float | None = None,
    batch_size: int | None = None,
    batch_pause_seconds: float | None = None,
) -> int:
    """One bounded sweep to populate BOTH read cursors for synced dialogs.

    Phase 39.3-02 R4: the same GetPeerDialogsRequest sweep that already
    populates ``read_inbox_max_id`` also populates ``read_outbox_max_id``
    from the same ``Dialog`` object — same endpoint, batched at
    ``ceil(N / 15)`` calls (Telethon's batch limit). No additional API
    endpoints introduced.

    D-03 LOCKED NULL preservation: if Telethon returns None for either
    cursor on a Dialog, ``apply_read_cursor`` is NOT called for that
    side. The DB cursor stays NULL so Plan 03's header renders
    ``[unknown (sync pending)]`` rather than lying with ``[all read]``.
    NEVER convert None → 0; NEVER call apply_read_cursor with 0 as a
    stand-in. This consistency rule applies symmetrically to inbox AND
    outbox. It tightens Phase 38's inbox-side behaviour (which used
    ``or 0``) — documented behavioural change.

    Batch size and inter-batch pacing are supplied by the hierarchical
    SchedulingConfig. The caller may also bound selected rows so a recurring
    pass cannot issue an unbounded number of Telegram actions.

    All writes use monotonic UPDATE — ``MAX(COALESCE(existing, 0), incoming)``
    via the shared primitive — so a live MessageRead / outbox-read event
    that arrives during the bootstrap window cannot be overwritten by a
    stale bootstrap reply (designed race safety, not accidental).
    """
    scheduling_defaults = SchedulingConfig()
    effective_batch_size = (
        scheduling_defaults.read_position_reconciliation_batch_size if batch_size is None else batch_size
    )
    effective_batch_pause_seconds = (
        scheduling_defaults.read_position_reconciliation_batch_pause_seconds
        if batch_pause_seconds is None
        else batch_pause_seconds
    )
    if effective_batch_size < 1:
        raise ValueError("batch_size must be positive")
    if effective_batch_pause_seconds <= 0:
        raise ValueError("batch_pause_seconds must be positive")

    now = int(time.time())
    rows = _select_null_read_position_rows(conn, max_dialogs, now=now)
    if not rows:
        logger.info("initialize_read_positions — no NULL rows, skipping")
        return 0

    dialog_ids = [dialog_id for (dialog_id,) in rows]
    filled = 0

    for i in range(0, len(dialog_ids), effective_batch_size):
        if shutdown_event.is_set():
            break
        retry_at = _read_position_retry_at(now, failure_cooldown_seconds)
        batch_ids = dialog_ids[i : i + effective_batch_size]
        batch_filled, stop = await _reconcile_read_position_batch(client, conn, shutdown_event, batch_ids, retry_at)
        filled += batch_filled
        if stop:
            return filled

        if not await _sleep_read_pos_batch(shutdown_event, effective_batch_pause_seconds):
            break

    logger.info("initialize_read_positions filled=%d/%d", filled, len(dialog_ids))
    return filled


async def _reconcile_read_position_batch(
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    batch_ids: list[int],
    retry_at: int | None,
) -> tuple[int, bool]:
    retry_ids: set[int] = set()
    try:
        input_peers, unresolved_ids = await _build_read_position_input_peers(client, batch_ids)
        retry_ids.update(unresolved_ids)
        if input_peers:
            result = cast(_ReadPositionsResultLike, await client(GetPeerDialogsRequest(peers=input_peers)))
            returned_ids: set[int] = set()
            filled = _apply_read_positions_from_dialogs(
                conn, result, retry_at=retry_at, returned_ids=returned_ids, failed_ids=retry_ids
            )
            retry_ids.update(set(batch_ids) - returned_ids)
        else:
            filled = 0
    except FloodWaitError as exc:
        logger.warning("read_pos_bootstrap flood_wait seconds=%d", exc.seconds)
        retry_ids.update(batch_ids)
        _mark_read_position_retry(conn, retry_ids, retry_at)
        conn.commit()
        await sleep_through_flood(shutdown_event, flood_seconds(exc, source="read_position_reconciliation"))
        return 0, True
    except TelegramRpcCircuitOpenError as exc:
        logger.debug("read_pos_bootstrap circuit_open error=%s", exc)
        retry_ids.update(batch_ids)
        _mark_read_position_retry(conn, retry_ids, retry_at)
        conn.commit()
        return 0, True
    except (RPCError, sqlite3.DatabaseError) as exc:
        logger.debug("read_pos_bootstrap batch_failed error=%s", exc)
        retry_ids.update(batch_ids)
        filled = 0
    _mark_read_position_retry(conn, retry_ids, retry_at)
    conn.commit()
    return filled, False


def _select_null_read_position_rows(
    conn: sqlite3.Connection,
    max_dialogs: int | None,
    *,
    now: int,
) -> list[tuple[int]]:
    """Select the durable NULL-cursor queue, optionally bounded for a pass."""
    if max_dialogs is None:
        return cast(
            list[tuple[int]],
            conn.execute(
                f"{_SELECT_NULL_READ_CURSORS_SQL} "
                "AND (sd.read_position_next_attempt_at IS NULL OR sd.read_position_next_attempt_at <= ?) "
                "ORDER BY COALESCE(sd.read_position_next_attempt_at, 0), "
                "COALESCE(sd.read_position_attempt_count, 0), sd.dialog_id",
                (now,),
            ).fetchall(),
        )
    if max_dialogs < 0:
        raise ValueError("max_dialogs must be non-negative")
    return cast(
        list[tuple[int]],
        conn.execute(
            f"{_SELECT_NULL_READ_CURSORS_SQL} "
            "AND (sd.read_position_next_attempt_at IS NULL OR sd.read_position_next_attempt_at <= ?) "
            "ORDER BY COALESCE(sd.read_position_next_attempt_at, 0), "
            "COALESCE(sd.read_position_attempt_count, 0), sd.dialog_id LIMIT ?",
            (now, max_dialogs),
        ).fetchall(),
    )


def _mark_read_position_retry(conn: sqlite3.Connection, dialog_ids: list[int] | set[int], retry_at: int | None) -> None:
    if retry_at is None or not dialog_ids:
        return
    placeholders = ", ".join("?" for _ in dialog_ids)
    conn.execute(
        "UPDATE synced_dialogs "
        "SET read_position_next_attempt_at = ?, "
        "read_position_attempt_count = COALESCE(read_position_attempt_count, 0) + 1 "
        f"WHERE status = 'synced' AND dialog_id IN ({placeholders})",
        (retry_at, *sorted(dialog_ids)),
    )


def _read_position_retry_at(now: int, cooldown_seconds: float | None) -> int | None:
    return None if cooldown_seconds is None else now + max(1, math.ceil(cooldown_seconds))


async def _run_read_position_reconciliation_loop(  # noqa: PLR0913 - daemon composition keeps policy explicit
    client: _DaemonClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval_seconds: float,
    max_dialogs_per_pass: int,
    failure_cooldown_seconds: float | None = None,
    batch_size: int | None = None,
    batch_pause_seconds: float | None = None,
) -> None:
    """Repeatedly reconcile durable NULL read-position work after startup.

    The first pass is immediate. Each subsequent pass waits for the configured
    interval, and all passes execute in this single daemon-owned task, so no
    overlapping Telegram sweeps can occur. SQLite NULL rows are the durable
    retry queue; access restoration and late enrollment naturally reappear in
    the next selection.
    """
    while not shutdown_event.is_set():
        await _initialize_read_positions(
            client,
            conn,
            shutdown_event,
            max_dialogs=max_dialogs_per_pass,
            failure_cooldown_seconds=failure_cooldown_seconds,
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
        )
        if shutdown_event.is_set():
            break
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue


async def _build_read_position_input_peers(
    client: _DaemonClient, batch_ids: list[int]
) -> tuple[list[TypeInputDialogPeer], list[int]]:
    input_peers: list[TypeInputDialogPeer] = []
    unresolved_ids: list[int] = []
    for dialog_id in batch_ids:
        try:
            peer = await client.get_input_entity(dialog_id)
            if peer is None:
                unresolved_ids.append(dialog_id)
                continue
            input_peer = cast(TypeInputPeer, peer)
            input_peers.append(InputDialogPeer(peer=input_peer))
        except FloodWaitError:
            raise
        except TelegramRpcCircuitOpenError:
            raise
        except (RPCError, TypeError, ValueError) as exc:
            logger.debug("read_pos_bootstrap skip dialog_id=%d error=%s", dialog_id, exc)
            unresolved_ids.append(dialog_id)
    return input_peers, unresolved_ids


def _apply_read_positions_from_dialogs(
    conn: sqlite3.Connection,
    result: _ReadPositionsResultLike,
    *,
    retry_at: int | None = None,
    returned_ids: set[int] | None = None,
    failed_ids: set[int] | None = None,
) -> int:
    """Apply read cursors from a GetPeerDialogsRequest result."""
    filled = 0
    with conn:
        for dialog in result.dialogs:
            if _apply_read_position_dialog(conn, dialog, retry_at, returned_ids, failed_ids):
                filled += 1
    return filled


def _apply_read_position_dialog(
    conn: sqlite3.Connection,
    dialog: _ReadPositionDialogLike,
    retry_at: int | None,
    returned_ids: set[int] | None,
    failed_ids: set[int] | None,
) -> bool:
    chat_id = int(cast(int, telethon_utils.get_peer_id(dialog.peer)))
    # D-03 LOCKED: None -> skip (preserve NULL). NEVER fold None -> 0; that
    # would lie with [all read] during the bootstrap window. 0 is a valid
    # distinct value (peer/me has read nothing) and is written as-is.
    inbox_max = cast(int | None, getattr(dialog, "read_inbox_max_id", None))
    outbox_max = cast(int | None, getattr(dialog, "read_outbox_max_id", None))
    if (
        conn.execute("SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1", (chat_id,)).fetchone()
        is None
    ):
        return False
    _add_returned_read_position_id(returned_ids, chat_id)
    wrote_any = False
    if inbox_max is not None and apply_read_cursor(conn, chat_id, "inbox", inbox_max) > 0:
        wrote_any = True
    if outbox_max is not None and apply_read_cursor(conn, chat_id, "outbox", outbox_max) > 0:
        wrote_any = True
    if retry_at is not None:
        if inbox_max is None or outbox_max is None:
            if failed_ids is not None:
                failed_ids.add(chat_id)
        else:
            conn.execute(
                "UPDATE synced_dialogs SET read_position_next_attempt_at = NULL, "
                "read_position_attempt_count = 0 WHERE dialog_id = ?",
                (chat_id,),
            )
    return wrote_any


def _add_returned_read_position_id(returned_ids: set[int] | None, chat_id: int) -> None:
    if returned_ids is not None:
        returned_ids.add(chat_id)


async def _sleep_read_pos_batch(shutdown_event: asyncio.Event, pause_seconds: float | None = None) -> bool:
    # Inter-batch pause: SIGTERM-responsive
    effective_pause_seconds = (
        SchedulingConfig().read_position_reconciliation_batch_pause_seconds if pause_seconds is None else pause_seconds
    )
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=effective_pause_seconds)
        return False
    except TimeoutError:
        return True


# ---------------------------------------------------------------------------
# Heartbeat — standalone for testability (no nonlocal / closure)
# ---------------------------------------------------------------------------


def _fetch_heartbeat_stats(conn: sqlite3.Connection) -> tuple[dict[str, int], int]:
    stats_rows = cast(
        list[tuple[str, int]],
        conn.execute("SELECT status, COUNT(*) FROM synced_dialogs GROUP BY status").fetchall(),
    )
    stats = dict(stats_rows)
    msg_count_row = cast(tuple[int], conn.execute("SELECT COUNT(*) FROM messages").fetchone())
    return stats, int(msg_count_row[0])


def _format_heartbeat_eta(sync_start: float, synced: int, total: int, now_mono: float) -> str:
    if synced <= 0 or synced >= total:
        return " eta=done" if synced >= total else ""

    remaining = total - synced
    elapsed = now_mono - sync_start
    secs_per_dialog = elapsed / synced
    eta_secs = int(remaining * secs_per_dialog)
    if eta_secs >= SECONDS_PER_HOUR:
        return f" eta={eta_secs // SECONDS_PER_HOUR}h{(eta_secs % SECONDS_PER_HOUR) // SECONDS_PER_MINUTE}m"
    if eta_secs >= SECONDS_PER_MINUTE:
        return f" eta={eta_secs // SECONDS_PER_MINUTE}m{eta_secs % SECONDS_PER_MINUTE}s"
    return f" eta={eta_secs}s"


def _log_heartbeat(
    conn: sqlite3.Connection,
    client: _DaemonClient,
    sync_start: float,
    prev_msg_count: int,
    prev_mono: float,
) -> tuple[int, float]:
    """Log heartbeat with sync stats, interval-based rate, and ETA from sync.db.

    Rate is computed over the heartbeat interval (since the last call), not
    since daemon startup — so an idle daemon shows 0msg/s instead of a stale
    decaying lifetime average.

    Returns (current_msg_count, current_mono) for the caller to feed into the
    next invocation.
    """
    try:
        stats, msg_count = _fetch_heartbeat_stats(conn)
    except sqlite3.DatabaseError:
        logger.warning("heartbeat_stats_failed", exc_info=True)
        stats = {}
        msg_count = 0
    synced = int(stats.get("synced", 0) or 0)
    syncing = int(stats.get("syncing", 0) or 0)
    total = synced + syncing + int(stats.get("not_synced", 0) or 0)

    now_mono = time.monotonic()
    interval = now_mono - prev_mono
    delta = max(0, msg_count - int(prev_msg_count or 0))
    rate = delta / interval if interval > 0 else 0.0

    logger.debug(
        "heartbeat — connected=%s dialogs=%d/%d messages=%d rate=%.0fmsg/s%s",
        client.is_connected(),
        synced,
        total,
        msg_count,
        rate,
        _format_heartbeat_eta(sync_start, synced, total, now_mono),
    )
    maybe_log_flood_wait_rollup(logger)
    return msg_count, now_mono


# ---------------------------------------------------------------------------
# Sync loop — batch processing + idle wait
# ---------------------------------------------------------------------------


async def _maybe_heartbeat_and_gap_scan(
    conn: sqlite3.Connection,
    client: _DaemonClient,
    handler_manager: EventHandlerManager,
    state: _SyncLoopState,
) -> _SyncLoopState:
    """Run heartbeat and gap scan if their intervals have elapsed.

    Returns the updated loop state.
    """
    now_mono = time.monotonic()

    if now_mono - state.last_heartbeat >= HEARTBEAT_INTERVAL_S:
        state.last_hb_msg_count, state.last_hb_mono = _log_heartbeat(
            conn,
            client,
            state.sync_start,
            state.last_hb_msg_count,
            state.last_hb_mono,
        )
        handler_manager.refresh_synced_dialogs()
        state.last_heartbeat = now_mono

    if now_mono - state.last_gap_scan >= GAP_SCAN_INTERVAL_S:
        deleted_count = await handler_manager.run_dm_gap_scan()
        logger.info("gap_scan complete — marked_deleted=%d", deleted_count)
        state.last_gap_scan = now_mono

    return state


async def _run_sync_loop(
    worker: FullSyncWorker,
    handler_manager: EventHandlerManager,
    shutdown_event: asyncio.Event,
    conn: sqlite3.Connection,
    client: _DaemonClient,
) -> None:
    """Run the batch-sync loop with periodic heartbeat and gap scan."""
    sync_start = time.monotonic()
    try:
        last_hb_row = cast(tuple[int], conn.execute("SELECT COUNT(*) FROM messages").fetchone())
        last_hb_msg_count = int(last_hb_row[0])
    except sqlite3.DatabaseError:
        last_hb_msg_count = 0
    state = _SyncLoopState(
        sync_start=sync_start,
        last_heartbeat=sync_start,
        last_gap_scan=sync_start,
        last_hb_msg_count=last_hb_msg_count,
        last_hb_mono=sync_start,
    )

    while not shutdown_event.is_set():
        kill_switch_status = flood_wait_kill_switch_status()
        if kill_switch_status.open:
            logger.critical("sync_loop_paused_flood_wait_kill_switch %s", kill_switch_status.detail())
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL_S)
            except TimeoutError:
                continue
            break

        all_synced = await worker.process_one_batch()
        await asyncio.sleep(0)

        state = await _maybe_heartbeat_and_gap_scan(
            conn,
            client,
            handler_manager,
            state,
        )

        if all_synced:
            if not state.was_idle:
                logger.info("sync_idle — all dialogs synced, waiting %ds", HEARTBEAT_INTERVAL_S)
                state.was_idle = True
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=HEARTBEAT_INTERVAL_S,
                )
                break
            except TimeoutError:
                state = await _maybe_heartbeat_and_gap_scan(
                    conn,
                    client,
                    handler_manager,
                    state,
                )
        elif state.was_idle:
            logger.info("sync_resume — work appeared, exiting idle")
            state.was_idle = False


def _create_tracked_task(
    ctx: _SyncMainContext,
    coro: Coroutine[object, object, object],
    *,
    name: str | None = None,
    critical: bool = False,
) -> asyncio.Task[object]:
    """Create an asyncio task and track it for shutdown cancellation."""
    task = asyncio.create_task(coro, name=name)
    ctx.background_tasks.add(task)

    def _on_done(t: asyncio.Task[object]) -> None:
        ctx.background_tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            if critical:
                ctx.api_server._ready = False
                ctx.api_server.startup_detail = f"critical background task failed: {t.get_name()}"
                ctx.shutdown_event.set()
                logger.critical("critical_background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)
            else:
                logger.error("background_task_failed name=%s error=%s", t.get_name(), exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


async def _monitor_flood_wait_kill_switch(ctx: _SyncMainContext) -> None:
    """Stop Telegram-facing work when the account-level FloodWait breaker opens."""
    await ctx.flood_wait_kill_switch_event.wait()
    status = flood_wait_kill_switch_status()
    if not status.open:
        return

    logger.critical("flood_wait_kill_switch_stopping_telegram_work %s", status.detail())
    current_task = asyncio.current_task()
    for task in list(ctx.background_tasks):
        if task is not current_task:
            task.cancel()
    await ctx.client.disconnect()
    logger.critical("flood_wait_kill_switch_telegram_disconnected")


def _install_flood_wait_kill_switch(config: McpTelegramConfig, event: asyncio.Event) -> None:
    policy_config = config.flood_wait
    configure_flood_wait_kill_switch(
        FloodWaitKillSwitchPolicy(
            enabled=policy_config.kill_switch_enabled,
            window_seconds=policy_config.kill_switch_window_seconds,
            max_events=policy_config.kill_switch_max_events,
            max_wait_seconds=policy_config.kill_switch_max_wait_seconds,
            minimum_cooldown_seconds=policy_config.kill_switch_minimum_cooldown_seconds,
        ),
        event=event,
    )


def _delta_catch_up_policy_from_scheduling(scheduling: SchedulingConfig) -> DeltaCatchUpPolicy:
    return DeltaCatchUpPolicy(
        interval_seconds=scheduling.delta_catch_up_interval_seconds,
        max_probes_per_cycle=scheduling.delta_catch_up_max_probes_per_cycle,
        probe_pause_seconds=scheduling.delta_catch_up_probe_pause_seconds,
    )


def _access_probe_policy_from_scheduling(scheduling: SchedulingConfig) -> AccessProbePolicy:
    return AccessProbePolicy(
        interval_seconds=scheduling.access_probe_interval_seconds,
        max_dialogs_per_cycle=scheduling.access_probe_max_dialogs_per_cycle,
        cooldown_seconds=scheduling.access_probe_cooldown_seconds,
        probe_pause_seconds=scheduling.access_probe_pause_seconds,
    )


def _telegram_rpc_budget_from_config(config: McpTelegramConfig) -> TelegramRpcBudget:
    return TelegramRpcBudget(
        max_calls_per_period=config.telegram_rpc.max_calls_per_period,
        period_seconds=config.telegram_rpc.period_seconds,
    )


def _message_fact_refresh_policy_from_config(config: McpTelegramConfig) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        interval_seconds=config.scheduling.message_fact_refresh_seconds,
        reaction_max_messages_per_cycle=config.scheduling.message_fact_refresh_reaction_max_messages_per_cycle,
        read_at_max_messages_per_cycle=config.scheduling.message_fact_refresh_read_at_max_messages_per_cycle,
        pause_seconds=config.scheduling.message_fact_refresh_pause_seconds,
        reaction_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        read_at_ttl_seconds=config.freshness.read_receipts.read_at_ttl_seconds,
    )


def _create_governed_telegram_client(config: McpTelegramConfig) -> _DaemonClient:
    raw_client = create_client(catch_up=True)
    return cast(
        _DaemonClient,
        GovernedTelegramClient(
            cast(GovernedTelegramClientTarget, raw_client),
            TelegramRpcGovernor(
                _telegram_rpc_budget_from_config(config),
                circuit_status=flood_wait_kill_switch_status,
            ),
        ),
    )


async def _build_sync_main_context() -> _SyncMainContext:  # noqa: PLR0914 - composition root wires all daemon-owned services
    config = load_config()
    state_paths = StatePaths.from_state_dir(ensure_private_state_dir(config.state.dir))
    db_path = state_paths.sync_db_path
    ensure_sync_schema(db_path)

    conn = _open_sync_db(db_path)
    migrate_legacy_databases(
        conn,
        state_paths.state_dir,
        telemetry_retention_ttl_seconds=config.telemetry.retention_ttl_seconds,
    )

    # Open feedback.db before registering the shutdown handler so the SIGTERM
    # handler can checkpoint it.  feedback_conn is opened on the asyncio thread
    # (sync_main coroutine) — the same thread the SIGTERM handler runs on via
    # loop.add_signal_handler — so no cross-thread SQLite sharing occurs.
    feedback_db_path = state_paths.feedback_db_path
    feedback_conn = ensure_feedback_schema(feedback_db_path)
    feedback_service = FeedbackApplicationService(SQLiteFeedbackStore(feedback_conn))
    logger.info("feedback.db ready at %s", feedback_db_path)

    shutdown_event = register_shutdown_handler(conn, asyncio.get_running_loop(), feedback_conn=feedback_conn)
    flood_wait_kill_switch_event = asyncio.Event()
    _install_flood_wait_kill_switch(config, flood_wait_kill_switch_event)

    client = _create_governed_telegram_client(config)
    reaction_freshener = ReactionFreshener(
        SQLiteReactionSnapshotRepository(conn),
        TelethonTelegramReactionGateway(client),
        freshness_ttl_seconds=config.freshness.reactions.freshness_ttl_seconds,
        log=logger,
    )
    topic_refresher = TopicRefresher(
        TelethonTelegramTopicGateway(cast(TopicClient, client)),
        SQLiteTopicSnapshotRepository(conn),
    )
    folder_repository = SQLiteFolderSnapshotRepository(conn)
    folder_refresher = FolderRefresher(
        TelethonTelegramFolderGateway(cast(FolderClient, client)),
        folder_repository,
    )
    api_server = DaemonAPIServer(
        conn,
        cast(DaemonClientLike, client),
        shutdown_event,
        feedback_service,
        db_path,
        reaction_freshener=reaction_freshener,
        topic_refresher=topic_refresher,
        policy=DaemonApiPolicy(
            read_at_ttl_seconds=config.freshness.read_receipts.read_at_ttl_seconds,
            entity_detail_ttl_seconds=config.freshness.entities.detail_ttl_seconds,
            user_directory_ttl_seconds=config.freshness.entities.user_directory_ttl_seconds,
            group_directory_ttl_seconds=config.freshness.entities.group_directory_ttl_seconds,
            resolver_enrichment_ttl_seconds=config.freshness.entities.resolver_enrichment_ttl_seconds,
            folder_snapshot_stale_after_seconds=config.scheduling.folder_projection.stale_threshold_seconds,
            telemetry_retention_ttl_seconds=config.telemetry.retention_ttl_seconds,
            slow_request_seconds=config.logging.daemon_api_slow_request_seconds,
        ),
        health_status=flood_wait_kill_switch_status,
    )
    socket_path = state_paths.daemon_socket_path
    socket_path.unlink(missing_ok=True)
    old_umask = os.umask(0o177)
    try:
        unix_server = await asyncio.start_unix_server(
            api_server.handle_client,
            path=str(socket_path),
            limit=2 * 1024 * 1024,
        )
    finally:
        os.umask(old_umask)
        socket_path.chmod(0o600)
    logger.info("daemon API listening on %s (not ready yet)", socket_path)
    return _SyncMainContext(
        db_path=db_path,
        conn=conn,
        feedback_conn=feedback_conn,
        shutdown_event=shutdown_event,
        client=client,
        reaction_freshener=reaction_freshener,
        message_fact_refresh_policy=_message_fact_refresh_policy_from_config(config),
        api_server=api_server,
        topic_refresher=topic_refresher,
        folder_projection_worker=FolderProjectionWorker(
            folder_refresher,
            folder_repository,
            shutdown_event,
            config.scheduling.folder_projection,
        ),
        socket_path=socket_path,
        unix_server=unix_server,
        scheduling=resolve_scheduling_config(config.scheduling),
        flood_wait_kill_switch_event=flood_wait_kill_switch_event,
    )


async def _run_fts_backfill(ctx: _SyncMainContext) -> None:
    # FTS backfill runs in a thread pool (stemming is CPU-bound) so it doesn't
    # block the event loop. Awaited here — before Telegram connect — so the
    # socket is already up and responding "not ready / indexing messages for
    # search" while we work. Total startup time = FTS time + Telegram time.
    ctx.api_server.startup_detail = "indexing messages for search"
    _ = ctx.api_server.startup_detail
    try:
        # Open a dedicated connection for the thread — sqlite3 connections are
        # not thread-safe and cannot be shared across threads.
        def _backfill_in_thread() -> int:
            thread_conn = _open_sync_db(ctx.db_path)
            try:
                return backfill_fts_index(thread_conn)
            finally:
                thread_conn.close()

        backfilled = await asyncio.to_thread(_backfill_in_thread)
        if backfilled:
            logger.info("fts_backfill=%d messages indexed", backfilled)
    except Exception:
        logger.warning("fts_backfill failed — FTS search may be incomplete until next restart", exc_info=True)


async def _connect_telegram(ctx: _SyncMainContext) -> bool:
    try:
        ctx.api_server.startup_detail = "connecting to Telegram"
        _ = ctx.api_server.startup_detail
        await ctx.client.connect()
    except (TimeoutError, OSError) as exc:
        ctx.api_server.startup_detail = f"connection failed: {exc}"
        logger.exception("sync-daemon connection failed: %s", exc)
        return False

    logger.info("sync-daemon started — connected=%s", ctx.client.is_connected())
    return True


async def _load_own_only_context(client: _DaemonClient, account_id: int) -> OwnOnlyContext:
    context = OwnOnlyContext(account_id=account_id)
    try:
        input_user = cast(TypeInputUser, await client.get_input_entity(account_id))
        full_result = await client(GetFullUserRequest(id=input_user))
        user_full = getattr(full_result, "full_user", None)
        personal_channel_id = getattr(user_full, "personal_channel_id", None)
        if isinstance(personal_channel_id, int) and personal_channel_id > 0:
            return OwnOnlyContext(account_id=account_id, personal_channel_id=personal_channel_id)
    except (FloodWaitError, RPCError, TypeError, AttributeError, ValueError) as exc:
        logger.warning("own_only_account_facts_unavailable error=%s", exc)
    return context


async def _prime_runtime(ctx: _SyncMainContext) -> None:
    # Phase 39.1: cache authenticated user id once at startup so query-build
    # paths (Plan 39.1-02) can bind it as a SQL parameter without calling
    # Telethon per request. Failure propagates — daemon cannot serve reads
    # correctly without a stable self_id.
    ctx.api_server.startup_detail = "fetching account info"
    _ = ctx.api_server.startup_detail
    me = cast(_MeLike, await ctx.client.get_me())
    ctx.api_server.self_id = int(me.id)
    ctx.api_server.self_profile = {
        "id": ctx.api_server.self_id,
        "first_name": getattr(me, "first_name", None),
        "last_name": getattr(me, "last_name", None),
        "username": getattr(me, "username", None),
    }
    ctx.own_only_context = await _load_own_only_context(ctx.client, ctx.api_server.self_id)
    ensure_own_only_schema(ctx.conn)
    logger.info("daemon self_id cached: %s", ctx.api_server.self_id)

    ctx.api_server.startup_detail = "refreshing Telegram folders"
    await ctx.folder_projection_worker.prime()

    # Post-v10 runtime backfill: mark historical outgoing DM rows as out=1
    # using sender_id=self_id (the authoritative signal). Pure-SQL v10
    # migration can only match sender_id IS NULL, but re-ingestion after
    # Phase 39.1 typically populates sender_id with the real peer/self
    # values — so the NULL-sender shape is rare in practice. This daemon
    # step closes the gap once self_id is known. Idempotent via out=0.
    try:
        cur = ctx.conn.execute(
            "UPDATE messages SET out = 1 WHERE out = 0 AND dialog_id > 0 AND sender_id = ?",
            (ctx.api_server.self_id,),
        )
        ctx.conn.commit()
        if cur.rowcount > 0:
            logger.info("backfilled out=1 on %d historical outgoing DM rows", cur.rowcount)
    except Exception:
        logger.warning("out=1 backfill skipped — non-fatal", exc_info=True)

    ctx.api_server._ready = True
    if ctx.api_server._ready:
        pass
    logger.info("daemon ready — serving requests on %s", ctx.socket_path)


async def _start_bootstrap_background_tasks(
    ctx: _SyncMainContext,
    worker: FullSyncWorker,
) -> None:
    assert ctx.handler_manager is not None

    ctx.api_server.startup_detail = "bootstrapping DMs"
    _ = ctx.api_server.startup_detail
    enrolled = await worker.bootstrap_dms()
    logger.info("dm_bootstrap complete — enrolled=%d", enrolled)

    ctx.handler_manager.refresh_synced_dialogs()

    # Background tasks — non-blocking, tracked for shutdown
    # D-07 / BOOTSTRAP-05: handler_manager.register() and refresh_synced_dialogs()
    # are both above this line, so live events for any dialog the bootstrap
    # touches are guaranteed to be wired before the first UPSERT.
    # BOOTSTRAP-02: this is a background task — does not block api_server._ready
    # (already set) or the /health endpoint.
    # Phase 41 review HIGH: pass db_path (NOT conn) — the worker opens its own
    # dedicated SQLite connection inside __init__, isolating it from the
    # daemon's main conn used by the other background tasks.
    task_specs: list[tuple[Coroutine[object, object, object], str]] = [
        (
            DialogsBootstrapWorker(
                ctx.client,
                ctx.db_path,
                ctx.shutdown_event,
                startup_detail_setter=lambda s: setattr(ctx.api_server, "startup_detail", s),
            ).run(),
            "dialogs_bootstrap_sweep",
        ),
        (_backfill_total_messages(ctx.client, ctx.conn, ctx.shutdown_event), "backfill_total_messages"),
    ]
    for coro, name in task_specs:
        _create_tracked_task(ctx, coro, name=name)


async def _start_followup_background_tasks(
    ctx: _SyncMainContext,
    delta_worker: DeltaSyncWorker,
) -> None:
    activity_client = cast(ActivityClient, ctx.client)
    delta_client = cast(_DeltaSyncClient, ctx.client)
    _create_tracked_task(
        ctx,
        ctx.folder_projection_worker.run(),
        name="folder_projection_worker",
        critical=True,
    )
    _create_tracked_task(
        ctx,
        _backfill_blank_unsupported_messages(ctx.client, ctx.conn, ctx.shutdown_event),
        name="backfill_blank_unsupported_messages",
    )
    _create_tracked_task(
        ctx,
        run_delta_catch_up_loop(
            delta_worker,
            ctx.shutdown_event,
            _delta_catch_up_policy_from_scheduling(ctx.scheduling),
        ),
        name="delta_catch_up_loop",
    )
    _create_tracked_task(
        ctx,
        run_message_fact_refresh_loop(
            ctx.conn,
            ctx.reaction_freshener,
            TelethonTelegramReadReceiptGateway(ctx.client),
            ctx.shutdown_event,
            ctx.message_fact_refresh_policy,
        ),
        name="message_fact_refresh_loop",
    )
    _create_tracked_task(
        ctx,
        run_access_probe_loop(
            delta_client,
            ctx.conn,
            ctx.shutdown_event,
            delta_worker,
            _access_probe_policy_from_scheduling(ctx.scheduling),
        ),
        name="access_probe_loop",
    )
    _create_tracked_task(
        ctx,
        run_activity_sync_loop(
            activity_client,
            ctx.conn,
            ctx.shutdown_event,
            timeout_s=ctx.scheduling.activity_rpc_timeout_seconds,
        ),
        name="activity_sync_loop",
    )
    _create_tracked_task(
        ctx,
        run_hot_sweep_loop(
            activity_client,
            ctx.conn,
            ctx.shutdown_event,
            interval=ctx.scheduling.activity_hot_sweep_seconds,
            timeout_s=ctx.scheduling.activity_rpc_timeout_seconds,
        ),
        name="activity_hot_sweep",
    )
    _create_tracked_task(
        ctx,
        run_cold_backfill_loop(
            activity_client,
            ctx.conn,
            ctx.shutdown_event,
            pacing=ColdBackfillPacing.from_scheduling(ctx.scheduling),
            timeout_s=ctx.scheduling.activity_rpc_timeout_seconds,
        ),
        name="activity_cold_backfill",
    )
    _create_tracked_task(
        ctx,
        run_scheduled_reconciliation_loop(
            ctx.client,
            ctx.conn,
            ctx.shutdown_event,
            policy=ScheduledReconciliationPolicy(
                interval_seconds=ctx.scheduling.scheduled_reconciliation_seconds,
                flood_sleep_threshold_seconds=ctx.scheduling.scheduled_flood_sleep_threshold_seconds,
                activity_rpc_timeout_seconds=ctx.scheduling.activity_rpc_timeout_seconds,
            ),
            own_only_context=ctx.own_only_context,
        ),
        name="scheduled_message_reconciliation",
    )

    # Phase 43 / RECON-01: hourly light pass + daily full pass keeps the
    # `dialogs` snapshot fresh; processes needs_refresh=1 rows written by
    # Phase 42 event handlers and soft-deletes left/kicked dialogs once a day.
    #
    # The scheduling config's RECON_HOURLY_SECONDS override (43-REVIEWS.md MEDIUM): default is
    # 3600s (1h) for production; setting it to a smaller value (e.g. "30") lets
    # an operator observe a needs_refresh=1 -> 0 transition in seconds during
    # UAT. Daily interval stays at the default 86400s — there is no need for a
    # daily override yet, and the first iteration always runs a full pass
    # regardless of last_full_pass anyway.
    _create_tracked_task(
        ctx,
        run_reconciliation_loop(
            ctx.client,
            ctx.conn,
            ctx.shutdown_event,
            hourly_interval=ctx.scheduling.reconciliation_hourly_seconds,
            topic_refresher=ctx.topic_refresher,
        ),
        name="reconciliation_loop",
    )


async def _shutdown_sync_main_context(ctx: _SyncMainContext) -> None:
    if ctx.unix_server is not None:
        ctx.unix_server.close()
        await ctx.unix_server.wait_closed()
    ctx.socket_path.unlink(missing_ok=True)
    if ctx.handler_manager is not None:
        ctx.handler_manager.unregister()
    # Cancel tracked background tasks
    for task in ctx.background_tasks:
        task.cancel()
    for task in list(ctx.background_tasks):
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected on shutdown; task was cancelled cleanly
        except Exception:
            logger.warning("background_task_shutdown_error name=%s", task.get_name(), exc_info=True)
    ctx.background_tasks.clear()
    await ctx.client.disconnect()
    try:
        ctx.feedback_conn.close()
    except Exception:
        logger.debug("feedback_conn close error", exc_info=True)
    ctx.conn.close()
    logger.info("sync-daemon stopped")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def sync_main() -> None:
    """Main entry point for the sync daemon process.

    Orchestrates: DB init → FTS backfill → Telegram connect → wire services →
    sync loop → cleanup.
    """
    install_telethon_flood_wait_metrics_filter()
    ctx = await _build_sync_main_context()
    try:
        _create_tracked_task(
            ctx,
            _monitor_flood_wait_kill_switch(ctx),
            name="flood_wait_kill_switch_monitor",
        )
        await _run_fts_backfill(ctx)

        input_peer_resolver = cast(InputPeerResolver, partial(resolve_input_peer, cast(ActivityClient, ctx.client)))
        ctx.handler_manager = EventHandlerManager(ctx.client, ctx.conn, ctx.shutdown_event, input_peer_resolver)
        ctx.handler_manager.register()
        logger.info("event handlers registered")

        if not await _connect_telegram(ctx):
            return

        await _prime_runtime(ctx)

        delta_worker = DeltaSyncWorker(cast(_DeltaSyncClient, ctx.client), ctx.conn, ctx.shutdown_event)
        worker = FullSyncWorker(ctx.client, ctx.conn, ctx.shutdown_event)
        await _start_bootstrap_background_tasks(ctx, worker)
        # Must come AFTER handler_manager.register() (startup-ordering invariant):
        # the on_message_read handler must be live before bootstrap starts so no
        # real-time MessageRead events are dropped during the bootstrap window.
        _create_tracked_task(
            ctx,
            _run_read_position_reconciliation_loop(
                ctx.client,
                ctx.conn,
                ctx.shutdown_event,
                interval_seconds=ctx.scheduling.read_position_reconciliation_seconds,
                max_dialogs_per_pass=ctx.scheduling.read_position_reconciliation_max_dialogs_per_pass,
                failure_cooldown_seconds=ctx.scheduling.read_position_reconciliation_failure_cooldown_seconds,
                batch_size=ctx.scheduling.read_position_reconciliation_batch_size,
                batch_pause_seconds=ctx.scheduling.read_position_reconciliation_batch_pause_seconds,
            ),
            name="initialize_read_positions",
        )
        await _start_followup_background_tasks(ctx, delta_worker)
        await _run_sync_loop(worker, ctx.handler_manager, ctx.shutdown_event, ctx.conn, ctx.client)
    finally:
        await _shutdown_sync_main_context(ctx)


_SYNC_MAIN = sync_main
