"""Storage and read-only reconciliation for Telegram scheduled messages.

Scheduled message IDs belong to a queue-local sequence.  This module keeps
their mirror separate from sent history while exposing the same message-shaped
fields and explicit lifecycle state for the query layer to project.  It never
invokes a mutating Telegram method.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .activity_peer_resolve import resolve_linked_chat_id
from .activity_substrate import ActivityClient
from .daemon_log_context import dialog_log_context
from .flood import TelegramRpcThrottled, _raise_if_latched
from .fts import stem_text
from .message_contracts import ExtractedMessage
from .messages.telegram_adapter import extract_message_row
from .own_only import (
    OwnOnlyBasis,
    OwnOnlyContext,
    add_own_only_basis,
    classify_own_only_dialog,
    enroll_own_only_dialog,
    query_own_only_candidates,
    remove_own_only_basis,
)
from .sync_db import SCHEDULED_ACTIVE_REPAIR_SECONDS, SCHEDULED_QUIET_DISCOVERY_SECONDS
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    demand_context,
)
from .telegram_gateway import ScheduledHistoryClient, fetch_scheduled_history_snapshot
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)


_SCHEDULED_SYNC_KEY = "account"
_DELETE_SCHEDULED_FTS_SQL = "DELETE FROM scheduled_messages_fts WHERE dialog_id=? AND message_id=?"
_INSERT_SCHEDULED_FTS_SQL = "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)"


@dataclass(frozen=True, slots=True)
class ScheduledReconciliationPolicy:
    activity_rpc_timeout_seconds: float
    state_scan_seconds: float = 60.0
    failure_retry_seconds: int = 300
    max_dialogs_per_slice: int = 8


def _as_int(value: object) -> int:
    return int(cast(int | str, value))


def _stable_discovery_due_at(dialog_id: int, now: int, spread_seconds: int) -> int:
    return now + ((dialog_id % spread_seconds) + spread_seconds) % spread_seconds


def _mark_scheduled_dialog_dirty(conn: sqlite3.Connection, dialog_id: int, now: int) -> None:
    """Coalesce realtime changes into one durable repair item."""
    discovery_due_at = _stable_discovery_due_at(dialog_id, now, SCHEDULED_QUIET_DISCOVERY_SECONDS)
    conn.execute(
        """
        INSERT INTO scheduled_reconciliation_state (
            dialog_id, repair_due_at, discovery_due_at, dirty_since,
            dirty_generation, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(dialog_id) DO UPDATE SET
            repair_due_at = CASE
                WHEN repair_due_at IS NULL OR repair_due_at > excluded.repair_due_at
                THEN excluded.repair_due_at ELSE repair_due_at END,
            dirty_since = COALESCE(dirty_since, excluded.dirty_since),
            dirty_generation = dirty_generation + 1,
            updated_at = excluded.updated_at
        """,
        (dialog_id, now, discovery_due_at, now, now),
    )
    add_own_only_basis(conn, dialog_id, OwnOnlyBasis.SCHEDULED_EVENT, now=now)


async def _load_candidate_entity(
    client: _ScheduledClient,
    conn: sqlite3.Connection,
    dialog_id: int,
    dialog_type: str,
    personal_channel_id: int | None,
) -> tuple[object | None, bool, bool]:
    """Fetch a channel entity; return (entity, stop, skip) for classification."""
    if dialog_type != "channel" or dialog_id == personal_channel_id:
        return None, False, False
    try:
        with rpc_scope(
            TelegramRpcSource.SCHEDULED_MESSAGES,
            acquisition_kind=AcquisitionKind.ENTITY_LOOKUP,
        ):
            return await client.get_entity(dialog_id), False, False
    except TelegramRpcThrottled as exc:
        _raise_if_latched(exc)
        assert exc.retry_after_seconds is not None
        _record_retry(conn, int(time.time()) + exc.retry_after_seconds, "TelegramRpcThrottled")
        return None, True, False
    except RPCError as exc:
        _log_own_only_entity_rpc_error(conn, dialog_id, exc)
        return None, False, True


class _ScheduledClient(ScheduledHistoryClient, Protocol):
    async def get_entity(self, _dialog_id: int) -> object: ...


class _ScheduledSnapshotMessage(Protocol):
    id: int


def _unix_timestamp(value: object | None) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, int):
        return value
    return None


def scheduled_dialog_id(peer: object | None) -> int | None:
    """Return a canonical dialog id from a raw Telegram Peer."""
    if peer is None:
        return None
    try:
        return int(cast(int, get_peer_id(peer)))
    except TypeError, ValueError:
        # Keep raw-update tests and older Telethon-compatible doubles useful.
        channel_id = cast(object | None, getattr(peer, "channel_id", None))
        if channel_id is not None:
            return -1000000000000 - _as_int(channel_id)
        chat_id = cast(object | None, getattr(peer, "chat_id", None))
        if chat_id is not None:
            return -_as_int(chat_id)
        user_id = cast(object | None, getattr(peer, "user_id", None))
        if user_id is not None:
            return _as_int(user_id)
        return None


def scheduled_message_dialog_id(message: object) -> int | None:
    """Extract the destination dialog id from a scheduled Message."""
    return scheduled_dialog_id(getattr(message, "peer_id", None))


def _scheduled_params(dialog_id: int, extracted: ExtractedMessage, source: object, now: int) -> dict[str, object]:
    message = extracted.message
    scheduled_at = _unix_timestamp(getattr(extracted, "scheduled_at", None))
    if scheduled_at is None:
        # Telegram exposes the schedule date as Message.date.  The extraction
        # helper already normalises it to sent_at for ordinary Message objects.
        scheduled_at = message.sent_at or None
    return {
        "dialog_id": dialog_id,
        "message_id": message.message_id,
        "scheduled_at": scheduled_at,
        "text": message.text,
        "sender_id": message.sender_id,
        "sender_first_name": message.sender_first_name,
        "media_kind": message.media_kind,
        "media_payload": message.media_payload,
        "reply_to_msg_id": message.reply_to_msg_id,
        "forum_topic_id": message.forum_topic_id,
        "edit_date": message.edit_date,
        "grouped_id": message.grouped_id,
        "reply_to_peer_id": message.reply_to_peer_id,
        "out": message.out,
        "is_service": message.is_service,
        "post_author": message.post_author,
        "schedule_repeat_period": getattr(source, "schedule_repeat_period", None),
        "updated_at": now,
    }


_UPSERT_SCHEDULED_SQL = """
INSERT INTO scheduled_messages (
    dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
    media_kind, media_payload, reply_to_msg_id, forum_topic_id, edit_date, grouped_id,
    reply_to_peer_id, out, is_service, post_author, schedule_repeat_period,
    message_state, visibility, unpublished, unseen, publication_hint_message_id,
    published_message_id, publication_verified_at, published_at, deleted_at,
    first_seen_at, updated_at
) VALUES (
    :dialog_id, :message_id, :scheduled_at, :text, :sender_id, :sender_first_name,
    :media_kind, :media_payload, :reply_to_msg_id, :forum_topic_id, :edit_date, :grouped_id,
    :reply_to_peer_id, :out, :is_service, :post_author, :schedule_repeat_period,
    'scheduled', 'author_only', 1, 1, NULL, NULL, NULL, NULL, NULL,
    :updated_at, :updated_at
)
ON CONFLICT(dialog_id, message_id) DO UPDATE SET
    scheduled_at = excluded.scheduled_at,
    text = excluded.text,
    sender_id = excluded.sender_id,
    sender_first_name = excluded.sender_first_name,
    media_kind = excluded.media_kind,
    media_payload = excluded.media_payload,
    reply_to_msg_id = excluded.reply_to_msg_id,
    forum_topic_id = excluded.forum_topic_id,
    edit_date = excluded.edit_date,
    grouped_id = excluded.grouped_id,
    reply_to_peer_id = excluded.reply_to_peer_id,
    out = excluded.out,
    is_service = excluded.is_service,
    post_author = excluded.post_author,
    schedule_repeat_period = excluded.schedule_repeat_period,
    message_state = 'scheduled',
    visibility = 'author_only',
    unpublished = 1,
    unseen = 1,
    publication_hint_message_id = NULL,
    published_message_id = NULL,
    publication_verified_at = NULL,
    published_at = NULL,
    deleted_at = NULL,
    updated_at = excluded.updated_at
"""


def upsert_scheduled_message(
    conn: sqlite3.Connection,
    dialog_id: int,
    message: object,
    *,
    now: int | None = None,
    mark_dirty: bool = True,
) -> None:
    """Insert or replace one scheduled snapshot without touching sent history."""
    extracted = extract_message_row(dialog_id, message)
    timestamp = int(time.time()) if now is None else int(now)
    scheduled_at = _unix_timestamp(getattr(extracted, "scheduled_at", None)) or extracted.message.sent_at
    if scheduled_at is None or scheduled_at <= timestamp:
        return
    conn.execute(_UPSERT_SCHEDULED_SQL, _scheduled_params(dialog_id, extracted, message, timestamp))
    conn.execute(_DELETE_SCHEDULED_FTS_SQL, (dialog_id, extracted.message.message_id))
    conn.execute(
        _INSERT_SCHEDULED_FTS_SQL,
        (dialog_id, extracted.message.message_id, stem_text(extracted.message.text)),
    )
    if mark_dirty:
        _mark_scheduled_dialog_dirty(conn, dialog_id, timestamp)


_INSERT_SCHEDULED_TOMBSTONE_SQL = """
INSERT OR IGNORE INTO scheduled_messages (
    dialog_id, message_id, message_state, visibility, unpublished, unseen,
    first_seen_at, updated_at, deleted_at, publication_hint_message_id
) VALUES (?, ?, ?, 'author_only', 1, 1, ?, ?, ?, ?)
"""


def mark_scheduled_messages_removed(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
    sent_message_ids: Sequence[int] | None = None,
    *,
    now: int | None = None,
) -> None:
    """Retain queue-removal evidence and store publication hints as unverified.

    Telegram's parallel ``messages``/``sent_messages`` vectors identify likely
    publication targets, but the mapping is not trusted until the corresponding
    normal message arrives with ``from_scheduled``.
    """
    timestamp = int(time.time()) if now is None else int(now)
    hints = list(sent_message_ids or ())
    with conn:
        for index, raw_message_id in enumerate(message_ids):
            message_id = int(raw_message_id)
            hint = int(hints[index]) if index < len(hints) else None
            state = "unknown_missing" if hint is not None else "cancelled"
            conn.execute(
                _INSERT_SCHEDULED_TOMBSTONE_SQL,
                (dialog_id, message_id, state, timestamp, timestamp, timestamp if hint is None else None, hint),
            )
            if hint is None:
                conn.execute(
                    "UPDATE scheduled_messages SET message_state='cancelled', unpublished=1, "
                    "unseen=1, deleted_at=?, updated_at=? WHERE dialog_id=? AND message_id=?",
                    (timestamp, timestamp, dialog_id, message_id),
                )
            else:
                conn.execute(
                    "UPDATE scheduled_messages SET message_state='unknown_missing', unpublished=1, "
                    "unseen=1, publication_hint_message_id=?, deleted_at=NULL, updated_at=? "
                    "WHERE dialog_id=? AND message_id=?",
                    (hint, timestamp, dialog_id, message_id),
                )
            conn.execute(_DELETE_SCHEDULED_FTS_SQL, (dialog_id, message_id))
        _mark_scheduled_dialog_dirty(conn, dialog_id, timestamp)


def verify_scheduled_publication(
    conn: sqlite3.Connection,
    dialog_id: int,
    published_message_id: int,
    *,
    now: int | None = None,
) -> int:
    """Confirm a publication hint after a normal ``from_scheduled`` message."""
    timestamp = int(time.time()) if now is None else int(now)
    rows = cast(
        list[tuple[object]],
        conn.execute(
            "SELECT message_id FROM scheduled_messages WHERE dialog_id=? "
            "AND publication_hint_message_id=? AND message_state='unknown_missing'",
            (dialog_id, int(published_message_id)),
        ).fetchall(),
    )
    scheduled_ids = [_as_int(row[0]) for row in rows]
    cursor = conn.execute(
        "UPDATE scheduled_messages SET message_state='published', visibility='chat_visible', unpublished=0, "
        "unseen=0, published_message_id=?, publication_verified_at=?, published_at=?, updated_at=? "
        "WHERE dialog_id=? "
        "AND publication_hint_message_id=? AND message_state='unknown_missing'",
        (int(published_message_id), timestamp, timestamp, timestamp, dialog_id, int(published_message_id)),
    )
    if scheduled_ids:
        conn.executemany(_DELETE_SCHEDULED_FTS_SQL, ((dialog_id, message_id) for message_id in scheduled_ids))
        _mark_scheduled_dialog_dirty(conn, dialog_id, timestamp)
    return cursor.rowcount


def mark_missing_from_snapshot(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
    *,
    now: int | None = None,
) -> int:
    """Mark active rows absent from an authoritative snapshot as non-visible."""
    timestamp = int(time.time()) if now is None else int(now)
    if not message_ids:
        return 0
    placeholders = ",".join("?" for _ in message_ids)
    params: tuple[object, ...] = (timestamp, dialog_id, *[int(item) for item in message_ids])
    cursor = conn.execute(
        "UPDATE scheduled_messages SET message_state='unknown_missing', unpublished=1, unseen=1, "
        "updated_at=? WHERE dialog_id=? AND message_state='scheduled' "
        f"AND message_id IN ({placeholders})",
        params,
    )
    if cursor.rowcount:
        conn.executemany(_DELETE_SCHEDULED_FTS_SQL, ((dialog_id, int(item)) for item in message_ids))
    return cursor.rowcount


def _record_retry(conn: sqlite3.Connection, retry_at: int, error: str) -> None:
    conn.execute(
        "UPDATE scheduled_sync_state SET next_retry_at=?, last_error=? WHERE key=?",
        (retry_at, error, _SCHEDULED_SYNC_KEY),
    )
    conn.commit()


def _log_own_only_entity_rpc_error(conn: sqlite3.Connection, dialog_id: int, exc: RPCError) -> None:
    log_context = dialog_log_context(conn, dialog_id)
    if isinstance(exc, ACCESS_LOST_ERRORS):
        now = int(time.time())
        set_access_lost(conn, dialog_id, now, reason=type(exc).__name__)
        conn.commit()
        return
    logger.warning(
        "scheduled_own_only_entity_error dialog_id=%d name=%r type=%s error_type=%s error=%s",
        dialog_id,
        log_context.name,
        log_context.type,
        type(exc).__name__,
        exc,
    )


def _clear_retry(conn: sqlite3.Connection, now: int) -> None:
    conn.execute(
        "UPDATE scheduled_sync_state SET next_retry_at=NULL, last_snapshot_at=?, last_error=NULL WHERE key=?",
        (now, _SCHEDULED_SYNC_KEY),
    )
    conn.commit()


def _retry_at(conn: sqlite3.Connection) -> int | None:
    row = cast(
        tuple[object] | None,
        conn.execute("SELECT next_retry_at FROM scheduled_sync_state WHERE key=?", (_SCHEDULED_SYNC_KEY,)).fetchone(),
    )
    return _as_int(row[0]) if row and row[0] is not None else None


def _snapshot_message_ids(snapshot: Sequence[object]) -> set[int]:
    return {int(cast(_ScheduledSnapshotMessage, message).id) for message in snapshot if getattr(message, "id", None)}


def _missing_scheduled_ids(conn: sqlite3.Connection, dialog_id: int, snapshot_ids: set[int]) -> list[int]:
    active_rows = cast(
        list[tuple[object]],
        conn.execute(
            "SELECT message_id FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled'",
            (dialog_id,),
        ).fetchall(),
    )
    return [_as_int(row[0]) for row in active_rows if _as_int(row[0]) not in snapshot_ids]


def _upsert_snapshot_messages(conn: sqlite3.Connection, dialog_id: int, snapshot: Sequence[object], now: int) -> None:
    for message in snapshot:
        if getattr(message, "id", None):
            upsert_scheduled_message(conn, dialog_id, message, now=now, mark_dirty=False)


def _has_active_scheduled_messages(conn: sqlite3.Connection, dialog_id: int) -> bool:
    active = cast(
        tuple[object] | None,
        conn.execute(
            "SELECT 1 FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled' LIMIT 1",
            (dialog_id,),
        ).fetchone(),
    )
    return active is not None


class ScheduledMessageReconciler:
    """Durable per-dialog scheduled-history reconciliation worker."""

    def __init__(
        self,
        client: _ScheduledClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        own_only_context: OwnOnlyContext | None = None,
        *,
        policy: ScheduledReconciliationPolicy,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._own_only_context = own_only_context
        self._policy = policy
        self._resolved_context = own_only_context
        self._next_candidate_seed_at = 0

    def _seed_candidates(self, now: int) -> None:
        """Enroll newly visible candidates without making them immediately due."""
        candidate_ids = self._candidate_ids()
        active_ids = {
            _as_int(row[0])
            for row in cast(
                list[tuple[object]],
                self._conn.execute(
                    "SELECT DISTINCT dialog_id FROM scheduled_messages WHERE message_state='scheduled'"
                ).fetchall(),
            )
        }
        candidate_ids.update(active_ids)
        with self._conn:
            for dialog_id in candidate_ids:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO scheduled_reconciliation_state(
                        dialog_id, repair_due_at, discovery_due_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        dialog_id,
                        now if dialog_id in active_ids else None,
                        _stable_discovery_due_at(dialog_id, now, SCHEDULED_QUIET_DISCOVERY_SECONDS),
                        now,
                    ),
                )
        self._next_candidate_seed_at = now + max(1, int(self._policy.state_scan_seconds))

    def _candidate_ids(self) -> set[int]:
        """Return local dialogs eligible for scheduled-queue discovery."""
        candidate_ids = {
            int(cast(int, row["dialog_id"]))
            for row in query_own_only_candidates(
                self._conn,
                personal_channel_id=None
                if self._own_only_context is None
                else self._own_only_context.personal_channel_id,
            )
        }
        candidate_ids.update(
            _as_int(row[0])
            for row in cast(
                list[tuple[object]], self._conn.execute("SELECT dialog_id FROM own_only_dialogs").fetchall()
            )
        )
        return candidate_ids

    def _has_unseeded_candidate(self) -> bool:
        """Report an unseeded candidate without mutating reconciliation state."""
        candidate_ids = self._candidate_ids()
        if not candidate_ids:
            return False
        seeded_ids = {
            _as_int(row[0])
            for row in cast(
                list[tuple[object]],
                self._conn.execute("SELECT dialog_id FROM scheduled_reconciliation_state").fetchall(),
            )
        }
        return bool(candidate_ids - seeded_ids)

    def _seed_candidates_if_due(self, now: int) -> None:
        if now >= self._next_candidate_seed_at:
            self._seed_candidates(now)

    async def _context(self) -> OwnOnlyContext | None:
        context = self._resolved_context
        if (
            context is None
            or context.personal_channel_id is None
            or context.personal_channel_linked_chat_id is not None
        ):
            return context
        resolution = await resolve_linked_chat_id(
            cast(ActivityClient, self._client),
            self._conn,
            context.personal_channel_id,
            timeout_s=self._policy.activity_rpc_timeout_seconds,
        )
        if resolution.flood_wait_seconds is not None:
            raise TelegramRpcThrottled(
                retry_after_seconds=max(1, resolution.flood_wait_seconds),
                latched=False,
                detail="Scheduled ownership discovery was throttled",
            )
        self._resolved_context = replace(context, personal_channel_linked_chat_id=resolution.linked_chat_id)
        return self._resolved_context

    async def _discover_eligibility(self, dialog_id: int) -> bool | None:
        """Classify one dialog; None means preserve prior knowledge and retry."""
        context = await self._context()
        if context is None:
            active = cast(
                tuple[object] | None,
                self._conn.execute(
                    "SELECT 1 FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled' LIMIT 1",
                    (dialog_id,),
                ).fetchone(),
            )
            return active is not None
        row = cast(
            tuple[object, object] | None,
            self._conn.execute("SELECT type, hidden FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
        )
        if row is None:
            known = cast(
                tuple[object] | None,
                self._conn.execute(
                    "SELECT 1 FROM own_only_dialogs WHERE dialog_id=? UNION SELECT 1 FROM scheduled_messages "
                    "WHERE dialog_id=? AND message_state='scheduled' LIMIT 1",
                    (dialog_id, dialog_id),
                ).fetchone(),
            )
            return known is not None
        dialog_type = str(row[0] or "unknown")
        entity, stop, skip = await _load_candidate_entity(
            self._client, self._conn, dialog_id, dialog_type, context.personal_channel_id
        )
        if stop:
            return None
        if skip:
            status = cast(
                tuple[object] | None,
                self._conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
            )
            return False if status is not None and status[0] == "access_lost" else None
        classification = classify_own_only_dialog(
            dialog_id=dialog_id,
            dialog_type=dialog_type,
            entity=entity,
            context=context,
        )
        if classification.included:
            enroll_own_only_dialog(self._conn, dialog_id, classification)
        return classification.included

    async def _fetch_scheduled_snapshot(self, dialog_id: int) -> list[object]:
        """Fetch one scheduled queue snapshot through the Telegram gateway."""
        with rpc_scope(
            TelegramRpcSource.SCHEDULED_MESSAGES,
            acquisition_kind=AcquisitionKind.SCHEDULED_MESSAGES_SNAPSHOT,
        ):
            return await fetch_scheduled_history_snapshot(self._client, dialog_id)

    def _due_rows(self, now: int, demand_kind: DemandKind | None = None) -> list[tuple[int, int, bool]]:
        if demand_kind is None:
            due_predicate = "repair_due_at <= :now OR discovery_due_at <= :now"
            discovery_expression = "discovery_due_at <= :now"
        elif demand_kind is DemandKind.SCHEDULED_REPAIR:
            due_predicate = "repair_due_at <= :now"
            discovery_expression = "0"
        elif demand_kind is DemandKind.SCHEDULED_DISCOVERY:
            due_predicate = "discovery_due_at <= :now"
            discovery_expression = "1"
        else:
            raise ValueError("demand_kind must be scheduled repair or discovery")
        rows = cast(
            list[tuple[object, object, object]],
            self._conn.execute(
                f"""
                SELECT dialog_id, dirty_generation, {discovery_expression}
                FROM scheduled_reconciliation_state
                WHERE {due_predicate}
                ORDER BY CASE WHEN dirty_since IS NULL THEN 1 ELSE 0 END,
                         CASE
                             WHEN dirty_since IS NULL
                             THEN MIN(COALESCE(repair_due_at, discovery_due_at), discovery_due_at)
                             ELSE dirty_since
                         END,
                         MIN(COALESCE(repair_due_at, discovery_due_at), discovery_due_at),
                         dialog_id
                LIMIT :limit
                """,
                {"now": now, "limit": self._policy.max_dialogs_per_slice},
            ).fetchall(),
        )
        return [(_as_int(row[0]), _as_int(row[1]), bool(row[2])) for row in rows]

    def _record_dialog_failure(self, dialog_id: int, kind: str, _code: str, now: int) -> None:
        due_column = "discovery_due_at" if kind == "discovery" else "repair_due_at"
        self._conn.execute(
            f"UPDATE scheduled_reconciliation_state SET {due_column}=?, updated_at=? WHERE dialog_id=?",
            (now + self._policy.failure_retry_seconds, now, dialog_id),
        )
        self._conn.commit()

    def _apply_snapshot(
        self,
        dialog_id: int,
        generation: int,
        snapshot: list[object],
        *,
        discovery: bool,
        now: int,
    ) -> int | None:
        # Acquire the write lock before checking the generation.  Otherwise a
        # concurrent event can dirty the dialog after the check and be erased
        # by this older snapshot.
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            current = cast(
                tuple[object] | None,
                self._conn.execute(
                    "SELECT dirty_generation FROM scheduled_reconciliation_state WHERE dialog_id=?", (dialog_id,)
                ).fetchone(),
            )
            if current is None or _as_int(current[0]) != generation:
                self._conn.rollback()
                return None
            snapshot_ids = _snapshot_message_ids(snapshot)
            missing_ids = _missing_scheduled_ids(self._conn, dialog_id, snapshot_ids)
            _upsert_snapshot_messages(self._conn, dialog_id, snapshot, now)
            changed = mark_missing_from_snapshot(self._conn, dialog_id, missing_ids, now=now)
            active = _has_active_scheduled_messages(self._conn, dialog_id)
            self._conn.execute(
                """
                UPDATE scheduled_reconciliation_state
                SET repair_due_at=?,
                    discovery_due_at=CASE WHEN ? THEN ? ELSE discovery_due_at END,
                    dirty_since=NULL,
                    updated_at=?
                WHERE dialog_id=? AND dirty_generation=?
                """,
                (
                    now + SCHEDULED_ACTIVE_REPAIR_SECONDS if active else None,
                    discovery,
                    now + SCHEDULED_QUIET_DISCOVERY_SECONDS,
                    now,
                    dialog_id,
                    generation,
                ),
            )
            if not active:
                remove_own_only_basis(self._conn, dialog_id, OwnOnlyBasis.SCHEDULED_EVENT, now=now)
            self._conn.commit()
            return changed
        except BaseException:
            self._conn.rollback()
            raise

    def _finish_excluded_discovery(self, dialog_id: int, now: int) -> bool:
        active = cast(
            tuple[object] | None,
            self._conn.execute(
                "SELECT 1 FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled' LIMIT 1",
                (dialog_id,),
            ).fetchone(),
        )
        if active is not None:
            return False
        with self._conn:
            remove_own_only_basis(self._conn, dialog_id, OwnOnlyBasis.SCHEDULED_EVENT, now=now)
            self._conn.execute(
                "UPDATE scheduled_reconciliation_state SET repair_due_at=NULL, discovery_due_at=?, "
                "updated_at=? WHERE dialog_id=?",
                (now + SCHEDULED_QUIET_DISCOVERY_SECONDS, now, dialog_id),
            )
        return True

    async def _prepare_discovery(self, dialog_id: int, now: int) -> tuple[int, bool] | None:
        try:
            eligible = await self._discover_eligibility(dialog_id)
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            assert exc.retry_after_seconds is not None
            _record_retry(self._conn, int(time.time()) + exc.retry_after_seconds, "TelegramRpcThrottled")
            return 0, True
        if eligible is None:
            self._record_dialog_failure(dialog_id, "discovery", "classification_unavailable", now)
            return 0, False
        if not eligible and self._finish_excluded_discovery(dialog_id, now):
            return 0, False
        return None

    async def _fetch_snapshot_or_stop(
        self, dialog_id: int, discovery: bool, now: int
    ) -> tuple[list[object] | None, bool]:
        try:
            return await self._fetch_scheduled_snapshot(dialog_id), False
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            assert exc.retry_after_seconds is not None
            wake_at = int(time.time()) + exc.retry_after_seconds
            _record_retry(self._conn, wake_at, "TelegramRpcThrottled")
            logger.warning(
                "scheduled_reconcile_flood_wait dialog_id=%d retry_at=%d — stopping account pass",
                dialog_id,
                wake_at,
            )
            return None, True
        except RPCError as exc:
            kind = "discovery" if discovery else "repair"
            self._record_dialog_failure(dialog_id, kind, type(exc).__name__, now)
            logger.warning("scheduled_reconcile_rpc_error dialog_id=%d error_type=%s", dialog_id, type(exc).__name__)
            return None, False

    async def _process_due_dialog(self, dialog_id: int, generation: int, discovery: bool, now: int) -> tuple[int, bool]:
        if discovery:
            outcome = await self._prepare_discovery(dialog_id, now)
            if outcome is not None:
                return outcome
        snapshot, stopped = await self._fetch_snapshot_or_stop(dialog_id, discovery, now)
        if snapshot is None:
            return 0, stopped
        changed = self._apply_snapshot(dialog_id, generation, snapshot, discovery=discovery, now=now)
        return (0 if changed is None else changed), False

    async def run_demand_slice(self, demand_kind: DemandKind) -> int:
        """Process one bounded slice for one scheduled demand contract."""
        if demand_kind not in (DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY):
            raise ValueError("demand_kind must be scheduled repair or discovery")
        return await self._run_slice(demand_kind)

    async def _process_due_row(
        self,
        dialog_id: int,
        generation: int,
        discovery: bool,
        now: int,
        demand_kind: DemandKind | None,
    ) -> tuple[int, bool]:
        if demand_kind is None:
            row_demand_kind = DemandKind.SCHEDULED_DISCOVERY if discovery else DemandKind.SCHEDULED_REPAIR
            with demand_context(row_demand_kind):
                return await self._process_due_dialog(dialog_id, generation, discovery, now)
        return await self._process_due_dialog(dialog_id, generation, discovery, now)

    async def _process_due_rows(
        self, due_rows: Sequence[tuple[int, int, bool]], now: int, demand_kind: DemandKind | None
    ) -> tuple[int, bool]:
        total = 0
        flood_waited = False
        for dialog_id, generation, discovery in due_rows:
            if self._shutdown_event.is_set():
                break
            changed, flood_waited = await self._process_due_row(dialog_id, generation, discovery, now, demand_kind)
            total += changed
            if flood_waited:
                break
        return total, flood_waited

    async def _run_slice(self, demand_kind: DemandKind | None = None) -> int:
        """Process one bounded slice, optionally restricted to one demand kind."""
        now = int(time.time())
        retry_at = _retry_at(self._conn)
        if retry_at is not None and retry_at > now:
            return 0
        self._seed_candidates_if_due(now)
        due_rows = self._due_rows(now, demand_kind)
        total, flood_waited = await self._process_due_rows(due_rows, now, demand_kind)

        if due_rows and not self._shutdown_event.is_set() and not flood_waited:
            _clear_retry(self._conn, int(time.time()))
        return total


class _ScheduledDemandAdapter(DurableDemandAdapter):
    """Expose one scheduled queue as durable demand over existing state."""

    demand_kind: DemandKind

    def __init__(self, reconciler: ScheduledMessageReconciler) -> None:
        self._reconciler = reconciler

    def status(self, now: float) -> DemandStatus | None:
        """Read the earliest due row without claiming or changing state."""
        del now
        column = "repair_due_at" if self.demand_kind is DemandKind.SCHEDULED_REPAIR else "discovery_due_at"
        row = cast(
            tuple[object] | None,
            self._reconciler._conn.execute(
                f"SELECT MIN({column}) FROM scheduled_reconciliation_state WHERE {column} IS NOT NULL"
            ).fetchone(),
        )
        unseeded = self.demand_kind is DemandKind.SCHEDULED_DISCOVERY and self._reconciler._has_unseeded_candidate()
        original_release_at: float | None = None
        if row is None or row[0] is None:
            if not unseeded:
                return None
            queue_release_at = 0.0
        else:
            queue_release_at = float(cast(int | float, row[0]))
            original_release_at = queue_release_at
            if unseeded:
                queue_release_at = 0.0
        release_at = queue_release_at
        account_retry_at = _retry_at(self._reconciler._conn)
        if account_retry_at is not None:
            release_at = max(release_at, float(account_retry_at))
        return DemandStatus(release_at, original_release_at)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one bounded scheduled slice under its exact demand and budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        status = self.status(time.time())
        if status is None or not status.is_ready(time.time()):
            return
        with demand_context(self.demand_kind):
            with rpc_attempt_budget(budget):
                try:
                    await self._reconciler.run_demand_slice(self.demand_kind)
                except RpcAttemptBudgetExhaustedError:
                    return


class ScheduledRepairDemandAdapter(_ScheduledDemandAdapter):
    """Durable demand adapter for active scheduled queue repair."""

    demand_kind = DemandKind.SCHEDULED_REPAIR


class ScheduledDiscoveryDemandAdapter(_ScheduledDemandAdapter):
    """Durable demand adapter for quiet scheduled queue discovery."""

    demand_kind = DemandKind.SCHEDULED_DISCOVERY


__all__ = [
    "ScheduledDiscoveryDemandAdapter",
    "ScheduledMessageReconciler",
    "ScheduledReconciliationPolicy",
    "ScheduledRepairDemandAdapter",
    "mark_missing_from_snapshot",
    "mark_scheduled_messages_removed",
    "scheduled_dialog_id",
    "scheduled_message_dialog_id",
    "upsert_scheduled_message",
    "verify_scheduled_publication",
]
