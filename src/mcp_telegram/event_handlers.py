"""EventHandlerManager — real-time event tracking engine for v1.5 Persistent Sync.

Registers three async Telethon event handlers against a live TelegramClient:
  - on_new_message:    INSERT OR REPLACE new messages into sync.db messages table
  - on_message_edited: version the old text into message_versions, update messages row
  - on_message_deleted: mark channel/supergroup messages as is_deleted=1

DM deletes cannot be tracked in real-time (MTProto UpdateDeleteMessages does not
carry peer identity for personal chats).  Use run_dm_gap_scan() on a weekly
schedule from the daemon heartbeat loop to detect and tombstone deleted DMs.

Architecture:
- Standalone module so daemon.py stays focused on process lifecycle.
- EventHandlerManager is instantiated once per daemon run, registered BEFORE
  FullSyncWorker starts so no real-time events are missed during full sync.
- All DB writes are synchronous sqlite3 (single-row ops, microsecond-fast).
- In-memory _synced_dialog_ids set refreshed via refresh_synced_dialogs() from
  the daemon heartbeat loop.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any

from telethon import events  # type: ignore[import-untyped]

from .fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from .resolver import latinize
from .sync_worker import INSERT_DIALOG_SQL, INSERT_MESSAGE_SQL, UPSERT_ENTITY_SQL, extract_message_row, serialize_reactions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------


_SELECT_MESSAGE_TEXT_SQL = (
    "SELECT text FROM messages WHERE dialog_id=? AND message_id=?"
)

_NEXT_VERSION_SQL = (
    "SELECT COALESCE(MAX(version), 0) + 1 FROM message_versions "
    "WHERE dialog_id=? AND message_id=?"
)

_INSERT_VERSION_SQL = (
    "INSERT INTO message_versions "
    "(dialog_id, message_id, version, old_text, edit_date) "
    "VALUES (?, ?, ?, ?, ?)"
)

_UPDATE_MESSAGE_TEXT_SQL = (
    "UPDATE messages SET text=? WHERE dialog_id=? AND message_id=?"
)

_MARK_DELETED_SQL = (
    "UPDATE messages SET is_deleted=1, deleted_at=? "
    "WHERE dialog_id=? AND message_id=? AND is_deleted=0"
)

_UPDATE_LAST_EVENT_SQL = (
    "UPDATE synced_dialogs SET last_event_at=? WHERE dialog_id=?"
)

_SELECT_SYNCED_DIALOGS_SQL = (
    "SELECT dialog_id FROM synced_dialogs WHERE status != 'access_lost'"
)

_SELECT_SYNCED_ONLY_SQL = (
    "SELECT dialog_id FROM synced_dialogs WHERE status = 'synced'"
)

_SELECT_UNDELETED_MESSAGES_SQL = (
    "SELECT message_id FROM messages "
    "WHERE dialog_id=? AND is_deleted=0 AND sent_at < ?"
)


# ---------------------------------------------------------------------------
# EventHandlerManager
# ---------------------------------------------------------------------------


class EventHandlerManager:
    """Registers and dispatches real-time Telethon events to sync.db.

    Args:
        client: Telethon TelegramClient (daemon owns the connection).
        conn: Open SQLite writer connection to sync.db.
        shutdown_event: asyncio.Event set when SIGTERM is received.
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
        self._synced_dialog_ids: set[int] = set()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self) -> None:
        """Load synced dialogs and attach the three event handlers to the client.

        Must be called BEFORE FullSyncWorker starts to ensure no real-time
        messages are missed during initial bulk fetch.  INSERT OR REPLACE
        handles overlap idempotently.
        """
        self._refresh_synced_dialogs()
        self._client.add_event_handler(self.on_new_message, events.NewMessage)
        self._client.add_event_handler(self.on_message_edited, events.MessageEdited)
        self._client.add_event_handler(self.on_message_deleted, events.MessageDeleted)

    def unregister(self) -> None:
        """Remove all three handlers from the client (graceful shutdown)."""
        self._client.remove_event_handler(self.on_new_message)
        self._client.remove_event_handler(self.on_message_edited)
        self._client.remove_event_handler(self.on_message_deleted)

    def refresh_synced_dialogs(self) -> None:
        """Refresh the in-memory synced-dialog set from the DB.

        Called from the daemon heartbeat loop so newly enrolled dialogs
        are picked up within one heartbeat interval without re-registering
        handlers.
        """
        self._refresh_synced_dialogs()

    def _refresh_synced_dialogs(self) -> None:
        rows = self._conn.execute(_SELECT_SYNCED_DIALOGS_SQL).fetchall()
        self._synced_dialog_ids = {int(row[0]) for row in rows}

    def _auto_enroll_dm(self, dialog_id: int, sender: Any | None = None) -> None:
        """Enroll a new DM dialog into synced_dialogs on first incoming message.

        Called from on_new_message when a private message arrives from a dialog
        not yet in synced_dialogs.  Uses INSERT OR IGNORE so concurrent calls
        and daemon restarts are idempotent.  After enrollment, the dialog is
        added to the in-memory set so subsequent messages are written real-time;
        FullSyncWorker picks up full history in its next batch cycle.

        If sender is provided (types.User), writes an entity row so the resolver
        can find this contact by name immediately.  Entity write is best-effort —
        failure does not prevent enrollment.
        """
        try:
            cursor = self._conn.execute(INSERT_DIALOG_SQL, (dialog_id,))
            self._conn.commit()
            if cursor.rowcount > 0:
                self._synced_dialog_ids.add(dialog_id)
                logger.info("dm_auto_enroll dialog_id=%d", dialog_id)
        except Exception:
            logger.exception("dm_auto_enroll_failed dialog_id=%d", dialog_id)
            return

        if sender is None:
            return
        try:
            first = getattr(sender, "first_name", None) or ""
            last = getattr(sender, "last_name", None) or ""
            name = f"{first} {last}".strip()
            if name:
                self._conn.execute(
                    UPSERT_ENTITY_SQL,
                    (dialog_id, "user", name, getattr(sender, "username", None), latinize(name), int(time.time())),
                )
                self._conn.commit()
                logger.info("dm_auto_enroll_entity dialog_id=%d name=%r", dialog_id, name)
        except Exception:
            logger.exception("dm_auto_enroll_entity_failed dialog_id=%d", dialog_id)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def on_new_message(self, event: Any) -> None:
        """Handle a NewMessage event: INSERT OR REPLACE into messages table.

        For enrolled dialogs: writes the message to sync.db immediately.
        For unenrolled private (DM) dialogs: auto-enrolls them so FullSyncWorker
        picks up the full history in its next batch cycle.
        Updates synced_dialogs.last_event_at in the same transaction.
        """
        dialog_id = event.chat_id
        if dialog_id is None:
            return
        if dialog_id not in self._synced_dialog_ids:
            if event.is_private:
                sender = None
                try:
                    sender = await event.get_sender()
                except Exception:
                    logger.debug("dm_auto_enroll_sender_fetch_failed dialog_id=%d", dialog_id)
                self._auto_enroll_dm(dialog_id, sender=sender)
            return

        try:
            msg = event.message
            row = extract_message_row(dialog_id, msg)
            now = int(time.time())

            with self._conn:
                self._conn.execute(INSERT_MESSAGE_SQL, row)
                self._conn.execute(DELETE_FTS_SQL, (dialog_id, int(getattr(msg, "id", 0))))
                self._conn.execute(
                    INSERT_FTS_SQL,
                    (dialog_id, int(getattr(msg, "id", 0)), stem_text(getattr(msg, "message", None))),
                )
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

            logger.info("event_new dialog_id=%d message_id=%d", dialog_id, msg.id)
        except Exception:
            logger.exception("event_new_failed dialog_id=%s", dialog_id)

    async def on_message_edited(self, event: Any) -> None:
        """Handle a MessageEdited event: version old text, update messages row.

        Three cases:
        1. Message not in sync.db yet: INSERT it with current text, no version history.
        2. Text unchanged: no-op (covers service edits, reactions updates, etc.).
        3. Text changed: insert old_text into message_versions, update messages.text.

        All operations in a single transaction.
        """
        dialog_id = event.chat_id
        if dialog_id is None or dialog_id not in self._synced_dialog_ids:
            return

        try:
            msg = event.message
            message_id = int(getattr(msg, "id", 0))
            new_text = getattr(msg, "message", None)
            now = int(time.time())

            with self._conn:
                existing = self._conn.execute(
                    _SELECT_MESSAGE_TEXT_SQL, (dialog_id, message_id)
                ).fetchone()

                if existing is None:
                    # Message not yet in sync.db: insert with current text;
                    # historical versions are lost (acceptable).
                    row = extract_message_row(dialog_id, msg)
                    self._conn.execute(INSERT_MESSAGE_SQL, row)
                    self._conn.execute(DELETE_FTS_SQL, (dialog_id, message_id))
                    self._conn.execute(
                        INSERT_FTS_SQL, (dialog_id, message_id, stem_text(new_text))
                    )
                    self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
                    logger.info(
                        "event_edit_new dialog_id=%d message_id=%d (not in sync.db, inserted)",
                        dialog_id, message_id,
                    )
                    return

                old_text = existing[0]
                if old_text == new_text:
                    # No text change — service edit, media caption update, etc.
                    return

                edit_date_raw = getattr(msg, "edit_date", None)
                edit_date_unix = (
                    int(edit_date_raw.timestamp()) if edit_date_raw is not None else now
                )

                next_ver = self._conn.execute(
                    _NEXT_VERSION_SQL, (dialog_id, message_id)
                ).fetchone()[0]

                self._conn.execute(
                    _INSERT_VERSION_SQL,
                    (dialog_id, message_id, next_ver, old_text, edit_date_unix),
                )
                self._conn.execute(
                    _UPDATE_MESSAGE_TEXT_SQL, (new_text, dialog_id, message_id)
                )
                # FTS5 INSERT OR REPLACE doesn't replace by content columns — must
                # delete the old row and insert fresh to avoid duplicate FTS entries.
                self._conn.execute(DELETE_FTS_SQL, (dialog_id, message_id))
                self._conn.execute(
                    INSERT_FTS_SQL, (dialog_id, message_id, stem_text(new_text))
                )
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

            logger.info(
                "event_edit dialog_id=%d message_id=%d version=%d",
                dialog_id, message_id, next_ver,
            )
        except Exception:
            logger.exception("event_edit_failed dialog_id=%s", dialog_id)

    async def on_message_deleted(self, event: Any) -> None:
        """Handle a MessageDeleted event: mark channel messages as is_deleted=1.

        chat_id is None for DMs and small groups (MTProto limitation).
        Those cases are handled by run_dm_gap_scan().
        Preserves the last known text column.
        Only updates rows where is_deleted=0 to avoid re-stamping deleted_at.
        """
        dialog_id = event.chat_id

        if dialog_id is None:
            logger.debug(
                "message_deleted: chat_id unknown — DM/group delete not trackable "
                "in real-time (MTProto limitation); weekly gap scan handles DMs"
            )
            return

        if dialog_id not in self._synced_dialog_ids:
            return

        try:
            now = int(time.time())

            with self._conn:
                for msg_id in event.deleted_ids:
                    self._conn.execute(_MARK_DELETED_SQL, (now, dialog_id, msg_id))
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

            logger.info(
                "event_delete dialog_id=%d count=%d", dialog_id, len(event.deleted_ids)
            )
        except Exception:
            logger.exception("event_delete_failed dialog_id=%s", dialog_id)

    # ------------------------------------------------------------------
    # DM gap scan
    # ------------------------------------------------------------------

    async def run_dm_gap_scan(self) -> int:
        """Scan all synced DM dialogs for deleted messages via live Telegram lookup.

        Compares synced message IDs (sent_at < scan_started_at) against live Telegram
        using client.get_messages(entity, ids=[...]) in batches of 100.  Messages
        returning None are confirmed deleted and tombstoned (is_deleted=1).

        Only messages synced before scan_started_at are checked to avoid false positives
        on messages that arrived during the scan itself.

        Returns:
            Total count of messages newly marked as is_deleted=1.
        """
        scan_started_at = int(time.time())
        total_marked = 0

        dialog_ids = [
            int(row[0])
            for row in self._conn.execute(_SELECT_SYNCED_ONLY_SQL).fetchall()
        ]

        for dialog_id in dialog_ids:
            try:
                message_ids = [
                    int(row[0])
                    for row in self._conn.execute(
                        _SELECT_UNDELETED_MESSAGES_SQL, (dialog_id, scan_started_at)
                    ).fetchall()
                ]

                if not message_ids:
                    continue

                # Batch in groups of 100 (Telegram API limit)
                for batch_start in range(0, len(message_ids), 100):
                    batch = message_ids[batch_start : batch_start + 100]
                    results = await self._client.get_messages(dialog_id, ids=batch)

                    now = int(time.time())
                    with self._conn:  # atomic per-dialog batch
                        for queried_id, returned_msg in zip(batch, results):
                            if returned_msg is None:
                                self._conn.execute(
                                    _MARK_DELETED_SQL, (now, dialog_id, queried_id)
                                )
                                total_marked += 1
            except Exception:
                logger.warning(
                    "dm_gap_scan_dialog_failed dialog_id=%d", dialog_id, exc_info=True,
                )

        logger.info("dm_gap_scan marked_deleted=%d", total_marked)
        return total_marked

