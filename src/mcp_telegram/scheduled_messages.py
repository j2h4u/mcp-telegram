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
from dataclasses import replace
from datetime import datetime
from typing import Protocol, cast

from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .activity_peer_resolve import resolve_linked_chat_id
from .activity_sync import _ActivityClient
from .fts import stem_text
from .message_contracts import ExtractedMessage
from .messages.telegram_adapter import extract_message_row
from .own_only import (
    OwnOnlyContext,
    classify_own_only_dialog,
    enroll_own_only_dialog,
    query_own_only_candidates,
)

logger = logging.getLogger(__name__)

_SCHEDULED_SYNC_KEY = "account"
_DEFAULT_RECONCILIATION_INTERVAL_S = 900.0
_DELETE_SCHEDULED_FTS_SQL = "DELETE FROM scheduled_messages_fts WHERE dialog_id=? AND message_id=?"
_INSERT_SCHEDULED_FTS_SQL = "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)"


def _as_int(value: object) -> int:
    return int(cast(int | str, value))


class _ScheduledClient(Protocol):
    async def get_messages(self, **_kwargs: object) -> object: ...

    async def get_entity(self, _dialog_id: int) -> object: ...

    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def __call__(self, _request: object) -> object: ...


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
        "media_description": message.media_description,
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
    media_description, reply_to_msg_id, forum_topic_id, edit_date, grouped_id,
    reply_to_peer_id, out, is_service, post_author, schedule_repeat_period,
    message_state, visibility, unpublished, unseen, publication_hint_message_id,
    published_message_id, publication_verified_at, published_at, deleted_at,
    first_seen_at, updated_at
) VALUES (
    :dialog_id, :message_id, :scheduled_at, :text, :sender_id, :sender_first_name,
    :media_description, :reply_to_msg_id, :forum_topic_id, :edit_date, :grouped_id,
    :reply_to_peer_id, :out, :is_service, :post_author, :schedule_repeat_period,
    'scheduled', 'author_only', 1, 1, NULL, NULL, NULL, NULL, NULL,
    :updated_at, :updated_at
)
ON CONFLICT(dialog_id, message_id) DO UPDATE SET
    scheduled_at = excluded.scheduled_at,
    text = excluded.text,
    sender_id = excluded.sender_id,
    sender_first_name = excluded.sender_first_name,
    media_description = excluded.media_description,
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


class ScheduledMessageReconciler:
    """Periodic authoritative scheduled-history snapshot worker."""

    def __init__(
        self,
        client: _ScheduledClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        own_only_context: OwnOnlyContext | None = None,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._own_only_context = own_only_context

    def _legacy_dialog_ids(self) -> set[int]:
        rows = cast(
            list[tuple[object]],
            self._conn.execute(
                "SELECT dialog_id FROM synced_dialogs WHERE status != 'access_lost' "
                "UNION SELECT dialog_id FROM scheduled_messages WHERE message_state='scheduled'"
            ).fetchall(),
        )
        return {_as_int(row[0]) for row in rows}

    async def _own_only_dialog_ids(self) -> set[int] | None:  # noqa: PLR0912
        """Classify accessible local candidates before touching scheduled history."""
        context = self._own_only_context
        if context is None:
            return None

        personal_linked_chat_id = context.personal_channel_linked_chat_id
        if context.personal_channel_id is not None:
            resolution = await resolve_linked_chat_id(
                cast(_ActivityClient, self._client), self._conn, context.personal_channel_id
            )
            if resolution.flood_wait_seconds is not None:
                _record_retry(
                    self._conn,
                    int(time.time()) + max(1, resolution.flood_wait_seconds),
                    "FloodWaitError",
                )
                return set()
            personal_linked_chat_id = resolution.linked_chat_id
            context = replace(context, personal_channel_linked_chat_id=personal_linked_chat_id)

        candidates = query_own_only_candidates(
            self._conn,
            personal_channel_id=context.personal_channel_id,
        )
        if context.personal_channel_id is not None and not any(
            int(cast(int, row["dialog_id"])) == context.personal_channel_id for row in candidates
        ):
            candidates.append(
                {
                    "dialog_id": context.personal_channel_id,
                    "name": None,
                    "type": "channel",
                    "linked_chat_id": personal_linked_chat_id,
                    "last_message_at": None,
                }
            )

        eligible: set[int] = set()
        for candidate in candidates:
            if self._shutdown_event.is_set():
                break
            dialog_id = int(cast(int, candidate["dialog_id"]))
            dialog_type = str(candidate.get("type") or "unknown")
            entity: object | None = None
            if dialog_type == "channel" and dialog_id != context.personal_channel_id:
                try:
                    entity = await self._client.get_entity(dialog_id)
                except FloodWaitError as exc:
                    _record_retry(self._conn, int(time.time()) + max(1, int(exc.seconds)), "FloodWaitError")
                    break
                except RPCError as exc:
                    logger.warning("scheduled_own_only_entity_error dialog_id=%d error=%s", dialog_id, exc)
                    continue
            classification = classify_own_only_dialog(
                dialog_id=dialog_id,
                dialog_type=dialog_type,
                entity=entity,
                context=context,
            )
            if classification.included:
                enroll_own_only_dialog(self._conn, dialog_id, classification)
                eligible.add(dialog_id)
        # The classifier is authoritative for the next read pass. Retaining a
        # previously owned channel after rights or personal-channel linkage
        # changes would expose its scheduled queue through local reads.
        with self._conn:
            if eligible:
                placeholders = ",".join("?" for _ in eligible)
                self._conn.execute(
                    f"DELETE FROM own_only_dialogs WHERE dialog_id NOT IN ({placeholders})",
                    tuple(sorted(eligible)),
                )
            else:
                self._conn.execute("DELETE FROM own_only_dialogs")
        return eligible

    async def run_once(self) -> int:
        now = int(time.time())
        retry_at = _retry_at(self._conn)
        if retry_at is not None and retry_at > now:
            return 0

        eligible_dialog_ids = await self._own_only_dialog_ids()
        if eligible_dialog_ids is None:
            dialog_ids = self._legacy_dialog_ids()
        else:
            dialog_ids = eligible_dialog_ids
        total = 0
        flood_waited = False
        for dialog_id in sorted(dialog_ids):
            if self._shutdown_event.is_set():
                break
            try:
                # Telethon translates this to messages.getScheduledHistory.
                result = await self._client.get_messages(entity=dialog_id, scheduled=True)
            except FloodWaitError as exc:
                retry_at = int(time.time()) + max(1, int(exc.seconds))
                _record_retry(self._conn, retry_at, "FloodWaitError")
                logger.warning(
                    "scheduled_reconcile_flood_wait dialog_id=%d retry_at=%d — stopping account pass",
                    dialog_id,
                    retry_at,
                )
                flood_waited = True
                break
            except RPCError as exc:
                logger.warning("scheduled_reconcile_rpc_error dialog_id=%d error=%s", dialog_id, exc)
                continue

            snapshot = list(cast(Sequence[object], result or ()))
            snapshot_ids = {int(getattr(message, "id", 0)) for message in snapshot if getattr(message, "id", None)}
            active_rows = cast(
                list[tuple[object]],
                self._conn.execute(
                    "SELECT message_id FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled'",
                    (dialog_id,),
                ).fetchall(),
            )
            missing_ids = [_as_int(row[0]) for row in active_rows if _as_int(row[0]) not in snapshot_ids]
            with self._conn:
                for message in snapshot:
                    if getattr(message, "id", None):
                        upsert_scheduled_message(self._conn, dialog_id, message, now=now)
                total += mark_missing_from_snapshot(self._conn, dialog_id, missing_ids, now=now)

        if not self._shutdown_event.is_set() and not flood_waited:
            _clear_retry(self._conn, int(time.time()))
        return total


async def run_scheduled_reconciliation_loop(
    client: _ScheduledClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_RECONCILIATION_INTERVAL_S,
    own_only_context: OwnOnlyContext | None = None,
) -> None:
    """Run snapshots until shutdown; FloodWait backoff is persisted, not slept."""
    while not shutdown_event.is_set():
        try:
            await ScheduledMessageReconciler(client, conn, shutdown_event, own_only_context).run_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("scheduled_reconcile_failed", exc_info=True)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
        except TimeoutError:
            continue


__all__ = [
    "ScheduledMessageReconciler",
    "mark_missing_from_snapshot",
    "mark_scheduled_messages_removed",
    "run_scheduled_reconciliation_loop",
    "scheduled_dialog_id",
    "scheduled_message_dialog_id",
    "upsert_scheduled_message",
    "verify_scheduled_publication",
]
