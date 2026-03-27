"""FullSyncWorker — bulk history fetch engine for v1.5 Persistent Sync.

Fetches all historical messages for marked dialogs in batches of 100,
checkpointing progress after each batch so restarts resume without
re-scanning from scratch (DAEMON-03/DAEMON-04).

FloodWait causes an interruptible sleep — progress is never lost on
rate limits (DAEMON-05).

DM bootstrap auto-enrolls all User-type dialogs at daemon startup
(DAEMON-06/DAEMON-07).

Architecture:
- Standalone module so daemon.py stays focused on process lifecycle.
- FullSyncWorker is a stateful class instantiated once per daemon run.
- Plugs into daemon.py sync_main() between heartbeat ticks (DAEMON-10).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import datetime
from typing import Any

from telethon.errors import FloodWaitError  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
    ChannelBannedError,
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserKickedError,
)
from telethon.tl import types  # type: ignore[import-untyped]

from .fts import INSERT_FTS_SQL, stem_text

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_INSERT_MESSAGE_SQL = (
    "INSERT OR REPLACE INTO messages "
    "(dialog_id, message_id, sent_at, text, sender_id, sender_first_name, "
    "media_description, reply_to_msg_id, forum_topic_id, reactions, is_deleted) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)"
)

_NEXT_PENDING_SQL = (
    "SELECT dialog_id, sync_progress FROM synced_dialogs "
    "WHERE status IN ('syncing', 'not_synced') "
    "ORDER BY rowid LIMIT 1"
)

_UPDATE_PROGRESS_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ? WHERE dialog_id = ?"
)

_INSERT_DIALOG_SQL = (
    "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')"
)

_ACCESS_LOST_ERRORS = (
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserKickedError,
    ChannelBannedError,
)

_SET_ACCESS_LOST_SQL = (
    "UPDATE synced_dialogs "
    "SET status = 'access_lost', access_lost_at = ? "
    "WHERE dialog_id = ?"
)


# ---------------------------------------------------------------------------
# Module-level field extraction helpers (shared with DeltaSyncWorker)
# ---------------------------------------------------------------------------


def serialize_reactions(reactions: Any | None) -> str | None:
    """Serialize a Telethon MessageReactions object to a JSON string.

    Format: {"emoji": count, ...} or None if no reactions.

    Per RESEARCH.md Open Question 1 recommendation: store a simple
    JSON summary dict consistent with ReactionMetadataCache in cache.py.
    """
    if reactions is None:
        return None
    results = getattr(reactions, "results", None)
    if not results:
        return None
    summary: dict[str, int] = {}
    for item in results:
        reaction = getattr(item, "reaction", None)
        emoticon = getattr(reaction, "emoticon", None) if reaction is not None else None
        count = getattr(item, "count", 0)
        if emoticon is not None:
            summary[emoticon] = int(count)
    return json.dumps(summary) if summary else None


def extract_message_row(dialog_id: int, msg: Any) -> tuple[object, ...]:
    """Extract sync.db messages row tuple from a Telethon message object.

    Mirrors cache.py::MessageCache.store_messages() field extraction
    adapted for the sync.db schema. Omits edit_date and fetched_at
    (not in sync.db); adds reactions serialization.
    """
    message_id = int(getattr(msg, "id", 0))

    date = getattr(msg, "date", None)
    sent_at = int(date.timestamp()) if isinstance(date, datetime) else 0

    text = getattr(msg, "message", None)

    sender_id = getattr(msg, "sender_id", None)
    sender = getattr(msg, "sender", None)
    sender_first_name = (
        getattr(sender, "first_name", None) if sender is not None else None
    )

    media = getattr(msg, "media", None)
    media_description: str | None = (
        type(media).__name__ if media is not None else None
    )

    reply_to = getattr(msg, "reply_to", None)
    reply_to_msg_id: int | None = None
    forum_topic_id: int | None = None
    if reply_to is not None:
        raw_rtmi = getattr(reply_to, "reply_to_msg_id", None)
        reply_to_msg_id = int(raw_rtmi) if raw_rtmi is not None else None
        if getattr(reply_to, "forum_topic", False):
            top_id = getattr(reply_to, "reply_to_top_id", None)
            forum_topic_id = int(top_id) if top_id is not None else 1

    reactions = serialize_reactions(getattr(msg, "reactions", None))

    return (
        dialog_id,
        message_id,
        sent_at,
        text,
        sender_id,
        sender_first_name,
        media_description,
        reply_to_msg_id,
        forum_topic_id,
        reactions,
    )


# ---------------------------------------------------------------------------
# FullSyncWorker
# ---------------------------------------------------------------------------


class FullSyncWorker:
    """Core bulk-fetch engine for the v1.5 sync daemon.

    Fetches historical Telegram messages in batches and stores them in
    sync.db.  One instance is created per daemon run; it is called
    between heartbeat ticks in sync_main().

    Args:
        client: Telethon TelegramClient (daemon owns the connection).
        conn: Open SQLite writer connection to sync.db.
        shutdown_event: asyncio.Event set when SIGTERM is received.
            Used to make FloodWait sleeps interruptible.
    """

    def __init__(
        self,
        client: Any,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def bootstrap_dms(self) -> int:
        """Enroll all DM dialogs into synced_dialogs with status='syncing'.

        Idempotent — uses INSERT OR IGNORE so existing rows (with real
        progress) are not overwritten.  Only types.User dialogs are
        enrolled; groups and channels require explicit opt-in (Phase 30).

        Returns:
            Count of newly enrolled dialogs (0 if all already present).
        """
        enrolled = 0
        async for dialog in self._client.iter_dialogs():
            if not isinstance(dialog.entity, types.User):
                continue
            cursor = self._conn.execute(_INSERT_DIALOG_SQL, (dialog.id,))
            if cursor.rowcount > 0:
                enrolled += 1
        self._conn.commit()
        logger.info("dm_bootstrap enrolled=%d new DM dialogs", enrolled)
        return enrolled

    async def process_one_batch(self) -> bool:
        """Fetch one batch of messages for the next pending dialog.

        Picks the first dialog with status in ('syncing', 'not_synced'),
        fetches up to 100 messages from where it left off, stores them,
        and updates sync_progress atomically.

        Returns:
            True  — all dialogs are synced (idle) or current dialog done.
            False — more work remains (batch was full, or FloodWait hit).
        """
        pending = self._next_pending_dialog()
        if pending is None:
            return True  # nothing to do — all synced (D-11)

        dialog_id, sync_progress = pending
        _, is_done = await self._fetch_batch(dialog_id, sync_progress)
        return is_done

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_pending_dialog(self) -> tuple[int, int] | None:
        """Return (dialog_id, sync_progress) for the next pending dialog.

        Selects in rowid (insertion) order — no prioritization (D-09).
        Returns None when no dialogs have status in ('syncing', 'not_synced').
        """
        row = self._conn.execute(_NEXT_PENDING_SQL).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1])

    async def _fetch_batch(
        self, dialog_id: int, sync_progress: int
    ) -> tuple[int, bool]:
        """Fetch up to 100 messages for dialog_id older than sync_progress.

        Uses offset_id=sync_progress (exclusive) so each batch fetches
        messages strictly older than the last committed checkpoint (D-04).
        After a full batch (100 msgs), sync_progress advances to the min
        message_id; a partial or empty batch marks the dialog 'synced'.

        On FloodWaitError: sleep interruptibly, return (same_progress, False).
        On other RPCError: log ERROR, skip dialog by marking it done.

        Returns:
            (new_progress, is_done)
        """
        iter_kwargs: dict[str, object] = {
            "entity": dialog_id,
            "limit": 100,
            "offset_id": sync_progress,
        }

        try:
            batch = [msg async for msg in self._client.iter_messages(**iter_kwargs)]
        except FloodWaitError as exc:
            logger.warning(
                "FloodWait dialog_id=%d — sleeping %ds", dialog_id, exc.seconds
            )
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(), timeout=float(exc.seconds)
                )
            except asyncio.TimeoutError:
                pass  # slept the full duration; retry same batch next call
            return sync_progress, False
        except _ACCESS_LOST_ERRORS as exc:
            logger.warning(
                "access_lost dialog_id=%d — %s: %s", dialog_id, type(exc).__name__, exc
            )
            now = int(time.time())
            with self._conn:
                self._conn.execute(_SET_ACCESS_LOST_SQL, (now, dialog_id))
            return sync_progress, True
        except RPCError as exc:
            logger.error(
                "RPC error dialog_id=%d — skipping: %s", dialog_id, exc
            )
            return sync_progress, True

        if not batch:
            # No more messages — dialog fully synced (Pitfall 3)
            self._conn.execute(
                _UPDATE_PROGRESS_SQL, (sync_progress, "synced", dialog_id)
            )
            self._conn.commit()
            return sync_progress, True

        rows = [self._extract_message_row(dialog_id, msg) for msg in batch]
        new_progress = min(int(getattr(msg, "id", 0)) for msg in batch)
        is_done = len(batch) < 100  # partial batch = last batch (Pitfall 3)
        new_status = "synced" if is_done else "syncing"

        # Single atomic transaction: messages + FTS + progress update (D-05)
        with self._conn:
            self._conn.executemany(_INSERT_MESSAGE_SQL, rows)
            self._conn.executemany(
                INSERT_FTS_SQL,
                ((row[0], row[1], stem_text(row[3])) for row in rows),  # type: ignore[arg-type]
            )
            self._conn.execute(
                _UPDATE_PROGRESS_SQL, (new_progress, new_status, dialog_id)
            )

        logger.debug(
            "batch dialog_id=%d fetched=%d new_progress=%d done=%s",
            dialog_id, len(batch), new_progress, is_done,
        )
        return new_progress, is_done

    def _extract_message_row(
        self, dialog_id: int, msg: Any
    ) -> tuple[object, ...]:
        """Extract sync.db messages row tuple from a Telethon message object.

        Thin wrapper around the module-level extract_message_row() so
        DeltaSyncWorker can import the shared implementation directly.
        """
        return extract_message_row(dialog_id, msg)

    @staticmethod
    def _serialize_reactions(reactions: Any | None) -> str | None:
        """Serialize a Telethon MessageReactions object to a JSON string.

        Thin wrapper around the module-level serialize_reactions().
        """
        return serialize_reactions(reactions)
