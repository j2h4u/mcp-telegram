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

import asyncio
import logging
import sqlite3
import time
from typing import Any

from telethon import events  # type: ignore[import-untyped]
from telethon.errors import FloodWaitError, RPCError  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    PeerChannel,
    PeerChat,
    UpdateChannel,
    UpdateChat,
    UpdateDialogPinned,
    UpdateDialogUnreadMark,
    UpdateMessageReactions,
    UpdatePinnedDialogs,
    UpdatePinnedForumTopic,
    UpdateReadChannelInbox,
    UpdateReadHistoryInbox,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .models import DialogType
from .read_state import apply_read_cursor
from .resolver import latinize
from .sync_worker import (
    INSERT_DIALOG_SQL,
    UPSERT_ENTITY_SQL,
    _build_fwd_entity_map,
    apply_reactions_delta,
    extract_message_row,
    extract_reactions_rows,
    insert_messages_with_fts,
)

logger = logging.getLogger(__name__)

_DM_AUTO_ENROLL_SENDER_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RPCError,
    Exception,
)

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------


_SELECT_MESSAGE_TEXT_SQL = "SELECT text FROM messages WHERE dialog_id=? AND message_id=?"

_NEXT_VERSION_SQL = "SELECT COALESCE(MAX(version), 0) + 1 FROM message_versions WHERE dialog_id=? AND message_id=?"

_INSERT_VERSION_SQL = (
    "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date) VALUES (?, ?, ?, ?, ?)"
)

_UPDATE_MESSAGE_TEXT_SQL = "UPDATE messages SET text=? WHERE dialog_id=? AND message_id=?"

_MARK_DELETED_SQL = "UPDATE messages SET is_deleted=1, deleted_at=? WHERE dialog_id=? AND message_id=? AND is_deleted=0"

_UPDATE_LAST_EVENT_SQL = "UPDATE synced_dialogs SET last_event_at=? WHERE dialog_id=?"

_SELECT_SYNCED_DIALOGS_SQL = "SELECT dialog_id FROM synced_dialogs WHERE status != 'access_lost'"

_SELECT_SYNCED_ONLY_SQL = "SELECT dialog_id FROM synced_dialogs WHERE status = 'synced'"

_SELECT_UNDELETED_MESSAGES_SQL = "SELECT message_id FROM messages WHERE dialog_id=? AND is_deleted=0 AND sent_at < ?"

# ---------------------------------------------------------------------------
# Phase 42 SQL — dialogs event writes (UPDATE-only; bootstrap is the sole
# row creator. snapshot_at always bound to int(time.time()) — never NULL —
# per inter-phase contract documented in dialog_sync.py:23-27.)
# ---------------------------------------------------------------------------

_UPDATE_DIALOG_PINNED_SQL = "UPDATE dialogs SET pinned=?, snapshot_at=? WHERE dialog_id=?"

_UPDATE_DIALOG_NEEDS_REFRESH_SQL = "UPDATE dialogs SET needs_refresh=1, snapshot_at=? WHERE dialog_id=?"

_UPDATE_DIALOG_LAST_MESSAGE_AT_SQL = (
    "UPDATE dialogs SET last_message_at = MAX(COALESCE(last_message_at, 0), ?),     snapshot_at = ? WHERE dialog_id = ?"
)

# IN-list rewrite — placeholder count substituted at call site:
_CLEAR_PINS_NOT_IN_SQL_TEMPLATE = (
    "UPDATE dialogs SET pinned=0, snapshot_at=? WHERE pinned=1 AND dialog_id NOT IN ({placeholders})"
)

# Empty-list fast path (NOT IN () is invalid SQLite — see review):
_CLEAR_ALL_PINS_SQL = "UPDATE dialogs SET pinned=0, snapshot_at=? WHERE pinned=1"

# ---------------------------------------------------------------------------
# Phase 42 SQL — topic_metadata event writes (target table extended by
# Plan 01 v19 ALTER. ON CONFLICT preserves existing fields not present in
# the edit via COALESCE. `pinned` is intentionally OMITTED from the UPDATE
# clause — pin state is owned by the dedicated UpdatePinnedForumTopic
# handler. Legacy NOT NULL columns (is_general, is_deleted, updated_at)
# supplied with safe defaults; the on-conflict path leaves them alone
# because they are not in the SET list.)
# ---------------------------------------------------------------------------

_UPSERT_TOPIC_METADATA_SQL = """
INSERT INTO topic_metadata
    (dialog_id, topic_id, title, top_message_id,
     is_general, is_deleted, updated_at,
     icon_emoji_id, pinned, hidden, snapshot_at, date)
VALUES
    (:dialog_id, :topic_id, :title, NULL,
     0, 0, :updated_at,
     :icon_emoji_id, 0, 0, :snapshot_at, :date)
ON CONFLICT(dialog_id, topic_id) DO UPDATE SET
    title          = COALESCE(excluded.title, topic_metadata.title),
    icon_emoji_id  = COALESCE(excluded.icon_emoji_id, topic_metadata.icon_emoji_id),
    updated_at     = excluded.updated_at,
    snapshot_at    = excluded.snapshot_at
WHERE topic_metadata.snapshot_at IS NULL
   OR topic_metadata.snapshot_at < excluded.snapshot_at
"""

_UPDATE_TOPIC_METADATA_EDIT_SQL = (
    "UPDATE topic_metadata "
    "SET title      = COALESCE(?, title), "
    "    icon_emoji_id = COALESCE(?, icon_emoji_id), "
    "    updated_at = ?, snapshot_at = ? "
    "WHERE dialog_id = ? AND topic_id = ? "
    "  AND (snapshot_at IS NULL OR snapshot_at < ?)"
)

_UPDATE_TOPIC_METADATA_HIDDEN_SQL = (
    "UPDATE topic_metadata SET hidden=1, snapshot_at=?, updated_at=? WHERE dialog_id=? AND topic_id=?"
)

_UPDATE_TOPIC_METADATA_PINNED_SQL = (
    "UPDATE topic_metadata SET pinned=?, snapshot_at=?, updated_at=? WHERE dialog_id=? AND topic_id=?"
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
        self._client.add_event_handler(self.on_message_read, events.MessageRead(inbox=True))
        # Phase 39.3-02: outbox read handler (peer→me side).
        # Dispatch path LOCKED to Path A: events.MessageRead(inbox=False).
        # Verified against .venv/lib/python3.14/site-packages/telethon/events/
        # messageread.py:37-48 — build() returns cls.Event(update.peer,
        # update.max_id, True) when the update is UpdateReadHistoryOutbox
        # (line 41-42); filter at lines 57-61 requires event.outbox == True
        # when inbox=False. Maximises symmetry with the Phase 38 inbox handler.
        self._client.add_event_handler(self.on_outbox_read, events.MessageRead(inbox=False))
        # Phase 39.2-01: 5th handler for raw reaction updates. Telethon emits
        # UpdateMessageReactions for User/Chat/Channel peers (single Update type;
        # peer field discriminates). Verified against
        # .venv/lib/python3.14/site-packages/telethon/events/raw.py — single-arg
        # callback contract: `async def handler(update)`.
        self._client.add_event_handler(
            self.on_raw_reaction_update,
            events.Raw(types=[UpdateMessageReactions]),
        )
        # Phase 42: three new Raw handlers for dialog metadata events.
        self._client.add_event_handler(
            self.on_raw_dialog_pinned,
            events.Raw(types=[UpdateDialogPinned, UpdatePinnedDialogs, UpdateDialogUnreadMark]),
        )
        self._client.add_event_handler(
            self.on_raw_channel_chat_update,
            events.Raw(types=[UpdateChannel, UpdateChat]),
        )
        self._client.add_event_handler(
            self.on_raw_inbox_read,
            events.Raw(types=[UpdateReadHistoryInbox, UpdateReadChannelInbox]),
        )
        # Phase 42 EVENTS-05: forum topic pin state.
        self._client.add_event_handler(
            self.on_raw_forum_topic_pinned,
            events.Raw(types=[UpdatePinnedForumTopic]),
        )

    def unregister(self) -> None:
        """Remove all handlers from the client (graceful shutdown)."""
        self._client.remove_event_handler(self.on_new_message)
        self._client.remove_event_handler(self.on_message_edited)
        self._client.remove_event_handler(self.on_message_deleted)
        self._client.remove_event_handler(self.on_message_read)
        self._client.remove_event_handler(self.on_outbox_read)
        self._client.remove_event_handler(self.on_raw_reaction_update)
        self._client.remove_event_handler(self.on_raw_dialog_pinned)
        self._client.remove_event_handler(self.on_raw_channel_chat_update)
        self._client.remove_event_handler(self.on_raw_inbox_read)
        self._client.remove_event_handler(self.on_raw_forum_topic_pinned)

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
                # status='syncing' is stable once inserted — FullSyncWorker only
                # advances it to 'synced', never back to 'not_synced'. Adding to
                # _synced_dialog_ids here is safe: the real-time handler path only
                # writes to messages and synced_dialogs.last_event_at, neither of
                # which depends on the status column. FullSyncWorker will backfill
                # full history via INSERT OR REPLACE in its next batch cycle.
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
            name: str | None = f"{first} {last}".strip() or None
            entity_type_str = DialogType.from_entity(sender).value
            with self._conn:
                self._conn.execute(
                    UPSERT_ENTITY_SQL,
                    (
                        dialog_id,
                        entity_type_str,
                        name,
                        getattr(sender, "username", None),
                        latinize(name) if name else None,
                        int(time.time()),
                    ),
                )
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
                except _DM_AUTO_ENROLL_SENDER_EXCEPTIONS:
                    logger.debug("dm_auto_enroll_sender_fetch_failed dialog_id=%d", dialog_id)
                self._auto_enroll_dm(dialog_id, sender=sender)
            return

        try:
            msg = event.message
            entity_name_map = await _build_fwd_entity_map(msg, self._client)
            extracted = extract_message_row(dialog_id, msg, entity_name_map=entity_name_map)
            now = int(time.time())

            with self._conn:
                insert_messages_with_fts(self._conn, [extracted])
                # Phase 42 EVENTS-04: advance dialogs.last_message_at monotonically.
                # MAX(COALESCE(..., 0), new_ts) ensures no regression on out-of-order
                # events. UPDATE matches 0 rows when the dialog is not yet bootstrapped
                # (no dialogs row) — silent no-op; bootstrap is the sole row creator.
                msg_date = getattr(msg, "date", None)
                self._update_last_message_timestamp(dialog_id, now, msg_date)
                self._handle_topic_message_action(dialog_id, msg, now)

            logger.info("event_new dialog_id=%d message_id=%d", dialog_id, msg.id)
        except Exception:
            logger.exception("event_new_failed dialog_id=%s", dialog_id)

    def _update_last_message_timestamp(
        self,
        dialog_id: int,
        now: int,
        msg_date: Any,
    ) -> None:
        if msg_date is not None:
            self._conn.execute(
                _UPDATE_DIALOG_LAST_MESSAGE_AT_SQL,
                (int(msg_date.timestamp()), now, dialog_id),
            )
        self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

    def _handle_topic_message_action(
        self,
        dialog_id: int,
        msg: Any,
        now: int,
    ) -> None:
        action = getattr(msg, "action", None)
        if isinstance(action, MessageActionTopicCreate):
            self._handle_topic_create_action(dialog_id, msg, now, action)
            return
        if isinstance(action, MessageActionTopicEdit):
            self._handle_topic_edit_action(dialog_id, msg, now, action)

    def _handle_topic_create_action(
        self,
        dialog_id: int,
        msg: Any,
        now: int,
        action: MessageActionTopicCreate,
    ) -> None:
        topic_id = int(getattr(msg, "id", 0))
        if topic_id <= 0:
            return
        msg_date = getattr(msg, "date", None)
        topic_timestamp = int(msg_date.timestamp()) if msg_date is not None else now
        self._conn.execute(
            _UPSERT_TOPIC_METADATA_SQL,
            {
                "dialog_id": dialog_id,
                "topic_id": topic_id,
                "title": getattr(action, "title", None) or "Topic",
                "icon_emoji_id": getattr(action, "icon_emoji_id", None),
                "updated_at": now,
                "snapshot_at": now,
                "date": topic_timestamp,
            },
        )
        logger.info(
            "event_topic_create dialog_id=%d topic_id=%d",
            dialog_id,
            topic_id,
        )

    def _handle_topic_edit_action(
        self,
        dialog_id: int,
        msg: Any,
        now: int,
        action: MessageActionTopicEdit,
    ) -> None:
        reply_to = getattr(msg, "reply_to", None)
        if reply_to is None:
            # Defensive: some MessageActionTopicEdit events carry no
            # reply_to; without it we cannot identify the target topic.
            logger.debug(
                "event_topic_edit_skipped reason=no_reply_to dialog_id=%d",
                dialog_id,
            )
            return

        topic_id_raw = getattr(reply_to, "reply_to_msg_id", None)
        if topic_id_raw is None:
            logger.debug(
                "event_topic_edit_skipped reason=no_reply_to_msg_id dialog_id=%d",
                dialog_id,
            )
            return

        topic_id = int(topic_id_raw)
        if bool(getattr(action, "hidden", False)):
            self._conn.execute(
                _UPDATE_TOPIC_METADATA_HIDDEN_SQL,
                (now, now, dialog_id, topic_id),
            )
            logger.info(
                "event_topic_hidden dialog_id=%d topic_id=%d",
                dialog_id,
                topic_id,
            )
            return

        # Non-hidden edits use an UPDATE-only path.
        # COALESCE(?, existing) preserves fields when the
        # edit omits them (action.title / icon_emoji_id may
        # be None). UPDATE matches 0 rows for unknown topics
        # — silent no-op; on_new_message UPSERT is the sole
        # row-creation path.
        edit_title = getattr(action, "title", None)
        edit_icon = getattr(action, "icon_emoji_id", None)
        self._conn.execute(
            _UPDATE_TOPIC_METADATA_EDIT_SQL,
            (edit_title, edit_icon, now, now, dialog_id, topic_id, now),
        )
        logger.info(
            "event_topic_edit dialog_id=%d topic_id=%d",
            dialog_id,
            topic_id,
        )

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

            # Resolve async data BEFORE opening transaction — SQLite's synchronous
            # driver cannot safely suspend inside a `with self._conn:` block while
            # another coroutine may call into the same connection.
            existing = self._conn.execute(_SELECT_MESSAGE_TEXT_SQL, (dialog_id, message_id)).fetchone()

            if existing is None:
                # Message not yet in sync.db: resolve entity map then insert.
                entity_name_map = await _build_fwd_entity_map(msg, self._client)
                extracted = extract_message_row(dialog_id, msg, entity_name_map=entity_name_map)
                with self._conn:
                    insert_messages_with_fts(self._conn, [extracted])
                    self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
                logger.info(
                    "event_edit_new dialog_id=%d message_id=%d (not in sync.db, inserted)",
                    dialog_id,
                    message_id,
                )
                return

            old_text = existing[0]
            if old_text == new_text:
                # No text change. Two sub-cases:
                # 1. msg.reactions present -> reactions-only edit; apply delta
                #    (Phase 39.2-01 AC-1 via edited path, AC-2 removal via empty results).
                # 2. msg.reactions is None -> service edit / media caption etc.; no-op
                #    (regression guard AC-8).
                reactions_obj = getattr(msg, "reactions", None)
                if reactions_obj is not None:
                    rows = extract_reactions_rows(dialog_id, message_id, reactions_obj)
                    with self._conn:
                        apply_reactions_delta(self._conn, dialog_id, message_id, rows)
                        self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
                    logger.info(
                        "event_edit_reactions dialog_id=%d message_id=%d count=%d",
                        dialog_id,
                        message_id,
                        len(rows),
                    )
                return

            edit_date_raw = getattr(msg, "edit_date", None)
            edit_date_unix = int(edit_date_raw.timestamp()) if edit_date_raw is not None else now

            # Resolve entity map before the transaction (no await inside with).
            entity_name_map = await _build_fwd_entity_map(msg, self._client)
            extracted = extract_message_row(dialog_id, msg, entity_name_map=entity_name_map)

            with self._conn:
                next_ver = self._conn.execute(_NEXT_VERSION_SQL, (dialog_id, message_id)).fetchone()[0]
                self._conn.execute(
                    _INSERT_VERSION_SQL,
                    (dialog_id, message_id, next_ver, old_text, edit_date_unix),
                )
                # Re-insert via insert_messages_with_fts: updates messages row,
                # refreshes FTS, and replaces child rows (edit idempotency).
                insert_messages_with_fts(self._conn, [extracted])
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

            logger.info(
                "event_edit dialog_id=%d message_id=%d version=%d",
                dialog_id,
                message_id,
                next_ver,
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

            logger.info("event_delete dialog_id=%d count=%d", dialog_id, len(event.deleted_ids))
        except Exception:
            logger.exception("event_delete_failed dialog_id=%s", dialog_id)

    async def on_message_read(self, event: Any) -> None:
        """Handle MessageRead(inbox=True): update read_inbox_max_id monotonically.

        Monotonic write via `MAX(COALESCE(existing, 0), incoming)` ensures the
        stored value never regresses — protects against out-of-order events and
        against bootstrap races where an older GetPeerDialogsRequest response
        could otherwise overwrite a newer live event.

        event.chat_id may be None for PM read events on some Telethon versions
        (UpdateReadHistoryInbox normalization differs). We log a WARNING so PM
        read-position staleness is observable; actual state will be re-resolved
        on next daemon restart via _initialize_read_positions.
        """
        dialog_id = event.chat_id

        if dialog_id is None:
            logger.warning(
                "event_read_null_chat_id max_id=%s — PM read position not tracked "
                "in real-time (MTProto/Telethon normalization); bootstrap on next "
                "daemon restart will reconcile",
                getattr(event, "max_id", "?"),
            )
            return

        if dialog_id not in self._synced_dialog_ids:
            return

        try:
            now = int(time.time())
            with self._conn:
                rowcount = apply_read_cursor(self._conn, dialog_id, "inbox", event.max_id)
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
            if rowcount > 0:
                logger.info("event_read dialog_id=%d max_id=%d", dialog_id, event.max_id)
            else:
                logger.warning(
                    "event_read_no_row dialog_id=%d max_id=%d — UPDATE matched 0 rows",
                    dialog_id,
                    event.max_id,
                )
        except Exception:
            logger.exception("event_read_failed dialog_id=%s", dialog_id)

    async def on_outbox_read(self, event: Any) -> None:
        """Handle MessageRead(inbox=False): update read_outbox_max_id monotonically.

        Path A dispatch (LOCKED). Verified against
        ``.venv/lib/python3.14/site-packages/telethon/events/messageread.py``
        lines 37-48: ``MessageRead.build()`` returns
        ``cls.Event(update.peer, update.max_id, True)`` when the update is an
        ``UpdateReadHistoryOutbox`` (lines 41-42); ``filter()`` at lines 57-61
        enforces ``event.outbox == True`` when ``inbox=False``. So this
        callback only ever fires on outbox reads — same shape as
        :meth:`on_message_read`, just the mirrored direction.

        Semantics:
        - PeerUser-only: only DM events advance the cursor. Non-DM events are
          silently dropped (no exception, no DB write). We detect DMs via
          ``event.is_private`` when present (Telethon sets it on the Event);
          when absent (synthetic test events), falling back to the
          ``_synced_dialog_ids`` membership check below is sufficient because
          non-DM dialogs never live in DM-enrollment paths.
        - Monotonic via shared :func:`apply_read_cursor` primitive — a smaller
          ``max_id`` is absorbed by ``MAX(COALESCE(existing, 0), ?)``.
        - ``event.chat_id`` may be None for PM read events on some Telethon
          versions (mirror of the inbox handler's quirk). Log warning, bail.
        - Exceptions wrapped in ``try/except Exception`` (not bare ``except``,
          not swallowing ``asyncio.CancelledError``); observable via the
          ``event_outbox_read_failed`` log.
        """
        dialog_id = event.chat_id

        if dialog_id is None:
            logger.warning(
                "event_outbox_read_null_chat_id max_id=%s — PM outbox read "
                "position not tracked in real-time; bootstrap on next daemon "
                "restart will reconcile",
                getattr(event, "max_id", "?"),
            )
            return

        # PeerUser-only filter: when the Telethon Event exposes is_private,
        # use it; otherwise rely on the synced_dialog_ids check below (non-DM
        # synced dialogs aren't tracked for outbox cursors — the read paths
        # that consume this surface are DM-only).
        is_private = getattr(event, "is_private", None)
        if is_private is False:
            return

        if dialog_id not in self._synced_dialog_ids:
            logger.debug(
                "event_outbox_read_unsynced dialog_id=%d max_id=%s",
                dialog_id,
                getattr(event, "max_id", "?"),
            )
            return

        max_id = getattr(event, "max_id", None)
        if max_id is None:
            return

        try:
            now = int(time.time())
            with self._conn:
                rowcount = apply_read_cursor(self._conn, dialog_id, "outbox", max_id)
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
            if rowcount > 0:
                logger.info("event_outbox_read dialog_id=%d max_id=%d", dialog_id, max_id)
            else:
                logger.warning(
                    "event_outbox_read_no_row dialog_id=%d max_id=%d — UPDATE matched 0 rows",
                    dialog_id,
                    max_id,
                )
        except asyncio.CancelledError:
            raise
        except (RuntimeError, sqlite3.DatabaseError) as exc:
            logger.error(
                "event_outbox_read_failed dialog_id=%s max_id=%s error=%r",
                dialog_id,
                max_id,
                exc,
            )

    async def on_raw_reaction_update(self, update: Any) -> None:
        """Handle raw UpdateMessageReactions for synced dialogs.

        Telethon contract (verified against
        .venv/lib/python3.14/site-packages/telethon/events/raw.py:22-23 and
        .venv/.../telethon/tl/types/__init__.py UpdateMessageReactions):
            async def handler(update)  # single-arg
        ``update`` is the raw TL Update with attributes:
            .peer (PeerUser | PeerChat | PeerChannel), .msg_id, .reactions

        For synced dialogs only: re-fetch the message via
        ``client.get_messages(dialog_id, ids=[msg_id])`` (integer dialog_id —
        no get_entity round-trip), extract reaction rows, apply per-message
        delta. FloodWait is logged + dropped (next JIT read repairs).
        Phase 39.2-01: AC-1 / AC-2 / AC-2-RAW / AC-UPD-USER / AC-UPD-CHANNEL.
        """
        peer = getattr(update, "peer", None)
        msg_id = getattr(update, "msg_id", None)
        if peer is None or msg_id is None:
            return
        try:
            dialog_id = int(get_peer_id(peer))
        except TypeError, ValueError:
            logger.debug("raw_reaction_update_unparseable_peer peer=%r", peer)
            return

        if dialog_id not in self._synced_dialog_ids:
            logger.debug(
                "raw_reaction_update_skipped_unsynced dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return

        try:
            result = await self._client.get_messages(dialog_id, ids=[msg_id])
        except FloodWaitError as exc:
            wait = getattr(exc, "seconds", 0)
            logger.warning(
                "raw_reaction_floodwait dialog_id=%d message_id=%d seconds=%d",
                dialog_id,
                msg_id,
                wait,
            )
            return
        except RPCError, RuntimeError:
            logger.exception(
                "event_raw_reaction_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return

        msg = result[0] if result else None
        if msg is None:
            logger.debug(
                "raw_reaction_update_missing_message dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return

        try:
            rows = extract_reactions_rows(dialog_id, msg_id, getattr(msg, "reactions", None))
            now = int(time.time())
            with self._conn:
                apply_reactions_delta(self._conn, dialog_id, msg_id, rows)
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
            logger.info(
                "event_raw_reaction dialog_id=%d message_id=%d count=%d",
                dialog_id,
                msg_id,
                len(rows),
            )
        except Exception:
            logger.exception(
                "event_raw_reaction_apply_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )

    # ------------------------------------------------------------------
    # Phase 42: dialog metadata Raw handlers (EVENTS-01, EVENTS-02, EVENTS-03)
    # ------------------------------------------------------------------

    def _dialog_id_from_peer(self, peer: Any | None) -> int | None:
        inner_peer = getattr(peer, "peer", peer)
        if inner_peer is None:
            return None
        return int(get_peer_id(inner_peer))

    def _collect_synced_pinned_dialog_ids(self, order: Any) -> list[int]:
        pinned_ids: list[int] = []
        for dialog_peer in order:
            inner_peer = getattr(dialog_peer, "peer", dialog_peer)
            try:
                dialog_id = int(get_peer_id(inner_peer))
            except TypeError, ValueError:
                continue
            if dialog_id in self._synced_dialog_ids:
                pinned_ids.append(dialog_id)
        return pinned_ids

    def _update_dialog_pinned(self, update: UpdateDialogPinned, now: int) -> None:
        dialog_id = self._dialog_id_from_peer(getattr(update, "peer", None))
        if dialog_id is None or dialog_id not in self._synced_dialog_ids:
            return
        pinned = 1 if getattr(update, "pinned", False) else 0
        with self._conn:
            self._conn.execute(_UPDATE_DIALOG_PINNED_SQL, (pinned, now, dialog_id))
        logger.info("event_dialog_pinned dialog_id=%d pinned=%d", dialog_id, pinned)

    def _rewrite_pinned_dialogs(self, update: UpdatePinnedDialogs, now: int) -> None:
        order = getattr(update, "order", None)
        if order is None:
            logger.debug("event_pinned_dialogs_order_none — skip")
            return
        # folder_id=None means the main list; folder_id=1 means Archived, etc.
        # A folder-scoped update carries only pins *within* that folder, so we
        # must not use it to clear pins in other folders.
        folder_id = getattr(update, "folder_id", None)
        # Decode peers; gate by _synced_dialog_ids so we never UPDATE
        # rows for dialogs the daemon does not own.
        pinned_ids = self._collect_synced_pinned_dialog_ids(order)
        with self._conn:
            for dialog_id in pinned_ids:
                self._conn.execute(
                    _UPDATE_DIALOG_PINNED_SQL,
                    (1, now, dialog_id),
                )
            if folder_id is None:
                # Main list: rewrite the full pin set — the update is
                # authoritative for all main-list pins.
                if pinned_ids:
                    placeholders = ",".join("?" * len(pinned_ids))
                    sql = _CLEAR_PINS_NOT_IN_SQL_TEMPLATE.format(
                        placeholders=placeholders,
                    )
                    self._conn.execute(sql, (now, *pinned_ids))
                else:
                    # Empty order list → all dialogs unpinned in main list.
                    # NOT IN () is invalid SQLite — use the dedicated SQL.
                    self._conn.execute(_CLEAR_ALL_PINS_SQL, (now,))
            # For folder-scoped updates (folder_id != None) we only set the
            # pinned=1 rows above; we do not clear other dialogs because the
            # update does not describe pins outside that folder.
        logger.info(
            "event_pinned_dialogs_rewrote pinned_count=%d folder_id=%s",
            len(pinned_ids),
            folder_id,
        )

    def _update_dialog_unread_mark(self, update: UpdateDialogUnreadMark, now: int) -> None:
        dialog_id = self._dialog_id_from_peer(getattr(update, "peer", None))
        if dialog_id is None or dialog_id not in self._synced_dialog_ids:
            return
        with self._conn:
            self._conn.execute(
                _UPDATE_DIALOG_NEEDS_REFRESH_SQL,
                (now, dialog_id),
            )
        logger.info(
            "event_dialog_unread_mark dialog_id=%d needs_refresh=1",
            dialog_id,
        )

    async def on_raw_dialog_pinned(self, update: Any) -> None:
        """Phase 42 EVENTS-01: dialogs.pinned + needs_refresh from raw updates.

        Handles three update types:
          - UpdateDialogPinned: single dialog pin toggle. Gated on
            _synced_dialog_ids; UPDATE-only (bootstrap creates rows).
          - UpdatePinnedDialogs: full pinned-set replacement (order list).
            order=None → no actionable data, skip.
            order=[] → unpin everything via _CLEAR_ALL_PINS_SQL (NOT IN () is
            invalid SQLite).
          - UpdateDialogUnreadMark: no dedicated column today; signal via
            needs_refresh=1 so reconciliation re-fetches the dialog (Phase 43).
        """
        try:
            now = int(time.time())
            if isinstance(update, UpdateDialogPinned):
                self._update_dialog_pinned(update, now)
            elif isinstance(update, UpdatePinnedDialogs):
                self._rewrite_pinned_dialogs(update, now)
            elif isinstance(update, UpdateDialogUnreadMark):
                self._update_dialog_unread_mark(update, now)
        except Exception:
            logger.exception(
                "event_dialog_pinned_failed update=%r",
                type(update).__name__,
            )

    async def on_raw_channel_chat_update(self, update: Any) -> None:
        """Phase 42 EVENTS-03: UpdateChannel / UpdateChat → dialogs.needs_refresh=1.

        Phase 54 EVENTS-03 extension: UpdateChannel for a broadcast channel with
        linked_chat_resolved_at IS NOT NULL also triggers one GetFullChannelRequest
        to refresh dialogs.linked_chat_id (D-10, D-11, D-12).

        Gated on _synced_dialog_ids; UPDATE-only.
        """
        try:
            if isinstance(update, UpdateChannel):
                dialog_id = int(get_peer_id(PeerChannel(update.channel_id)))
            elif isinstance(update, UpdateChat):
                dialog_id = int(get_peer_id(PeerChat(update.chat_id)))
            else:
                return
            if dialog_id not in self._synced_dialog_ids:
                return
            now = int(time.time())
            with self._conn:
                self._conn.execute(_UPDATE_DIALOG_NEEDS_REFRESH_SQL, (now, dialog_id))
                row = self._conn.execute(
                    "SELECT type, linked_chat_resolved_at FROM dialogs WHERE dialog_id = ?",
                    (dialog_id,),
                ).fetchone()
            logger.info("event_channel_chat_dirty dialog_id=%d", dialog_id)
        except Exception:
            logger.exception(
                "event_channel_chat_update_failed update=%r",
                type(update).__name__,
            )
            return

        # D-10 / D-11: refresh linked_chat_id ONLY for UpdateChannel on a channel
        # that has been resolved at least once. Never-resolved channels belong to the
        # sweep's cold path (D-11) — we must not amplify bursts into resolution storms.
        # This await is deliberately OUTSIDE the with self._conn: block above to avoid
        # holding a write transaction open across an async round-trip.
        if isinstance(update, UpdateChannel) and row is not None and row[0] == "channel" and row[1] is not None:
            await self._refresh_linked_chat_id(dialog_id)

    async def _refresh_linked_chat_id(self, dialog_id: int) -> None:
        """Phase 54: event-driven linked_chat_id refresh for a previously-resolved channel.

        Issues exactly one GetFullChannelRequest and UPSERTs linked_chat_id +
        linked_chat_resolved_at into dialogs. FloodWait is swallowed without
        writing — the unchanged resolved_at acts as the retry signal for the
        next sweep cycle or UpdateChannel event.
        """
        from telethon.errors import FloodWaitError  # type: ignore[import-untyped]
        from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[import-untyped]
        from telethon.tl.types import PeerChannel as _PeerChannel  # type: ignore[import-untyped]
        from telethon.utils import get_peer_id as _get_peer_id  # type: ignore[import-untyped]

        from .activity_peer_resolve import resolve_input_peer

        input_channel = await resolve_input_peer(self._client, dialog_id)
        if input_channel is None:
            logger.debug("event_linked_chat_refresh_no_input_peer dialog_id=%d", dialog_id)
            return

        try:
            full_result = await self._client(GetFullChannelRequest(channel=input_channel))
        except FloodWaitError as exc:
            logger.warning(
                "event_linked_chat_refresh_flood dialog_id=%d flood_wait_seconds=%d",
                dialog_id,
                int(exc.seconds),
            )
            return
        except Exception:
            logger.debug("event_linked_chat_refresh_failed dialog_id=%d", dialog_id, exc_info=True)
            return

        raw = getattr(full_result.full_chat, "linked_chat_id", None)
        normalised: int | None = None
        if raw is not None:
            if raw > 0:
                normalised = int(_get_peer_id(_PeerChannel(raw)))
            else:
                normalised = int(raw)

        now = int(time.time())
        with self._conn:
            self._conn.execute(
                "INSERT INTO dialogs (dialog_id, linked_chat_id, linked_chat_resolved_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(dialog_id) DO UPDATE SET "
                "    linked_chat_id = excluded.linked_chat_id, "
                "    linked_chat_resolved_at = excluded.linked_chat_resolved_at",
                (dialog_id, normalised, now),
            )
        logger.info(
            "event_linked_chat_refresh dialog_id=%d linked_chat_id=%r",
            dialog_id,
            normalised,
        )

    async def on_raw_inbox_read(self, update: Any) -> None:
        """Phase 42 EVENTS-02: UpdateReadHistoryInbox / UpdateReadChannelInbox.

        Captures still_unread_count via structured log (the high-level
        events.MessageRead wrapper drops this field). No dialogs.unread_count
        column is added in this milestone — capture-via-log is the explicit
        satisfaction strategy for EVENTS-02 (see plan revision_notes).

        Gated on _synced_dialog_ids; observability-only — no dialogs UPDATE.
        Updates synced_dialogs.last_event_at via the existing _UPDATE_LAST_EVENT_SQL
        so the last_event_at observability stays intact.
        """
        try:
            if isinstance(update, UpdateReadHistoryInbox):
                dialog_id = int(get_peer_id(update.peer))
            elif isinstance(update, UpdateReadChannelInbox):
                dialog_id = int(get_peer_id(PeerChannel(update.channel_id)))
            else:
                return
            if dialog_id not in self._synced_dialog_ids:
                return
            still_unread = int(getattr(update, "still_unread_count", 0))
            max_id = int(getattr(update, "max_id", 0))
            now = int(time.time())
            with self._conn:
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
            logger.info(
                "event_raw_inbox_read dialog_id=%d max_id=%d still_unread_count=%d",
                dialog_id,
                max_id,
                still_unread,
            )
        except Exception:
            logger.exception(
                "event_raw_inbox_read_failed update=%r",
                type(update).__name__,
            )

    async def on_raw_forum_topic_pinned(self, update: Any) -> None:
        """Phase 42 EVENTS-05: UpdatePinnedForumTopic → topic_metadata.pinned.

        Gated on _synced_dialog_ids; UPDATE-only. Missing-row UPDATE matches
        0 rows without crashing — bootstrap / on_new_message UPSERT remain the
        sole row-creation paths.
        """
        try:
            if not isinstance(update, UpdatePinnedForumTopic):
                return
            peer = getattr(update, "peer", None)
            topic_id_raw = getattr(update, "topic_id", None)
            if peer is None or topic_id_raw is None:
                return
            dialog_id = int(get_peer_id(peer))
            if dialog_id not in self._synced_dialog_ids:
                return
            topic_id = int(topic_id_raw)
            pinned = 1 if getattr(update, "pinned", False) else 0
            now = int(time.time())
            with self._conn:
                self._conn.execute(
                    _UPDATE_TOPIC_METADATA_PINNED_SQL,
                    (pinned, now, now, dialog_id, topic_id),
                )
            logger.info(
                "event_forum_topic_pinned dialog_id=%d topic_id=%d pinned=%d",
                dialog_id,
                topic_id,
                pinned,
            )
        except Exception:
            logger.exception(
                "event_forum_topic_pinned_failed update=%r",
                type(update).__name__,
            )

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

        dialog_ids = [int(row[0]) for row in self._conn.execute(_SELECT_SYNCED_ONLY_SQL).fetchall()]

        for dialog_id in dialog_ids:
            try:
                total_marked += await self._scan_dm_gap_dialog(dialog_id, scan_started_at)
            except Exception:
                logger.warning(
                    "dm_gap_scan_dialog_failed dialog_id=%d",
                    dialog_id,
                    exc_info=True,
                )

        logger.info("dm_gap_scan marked_deleted=%d", total_marked)
        return total_marked

    async def _scan_dm_gap_dialog(self, dialog_id: int, scan_started_at: int) -> int:
        message_ids = [
            int(row[0])
            for row in self._conn.execute(
                _SELECT_UNDELETED_MESSAGES_SQL,
                (dialog_id, scan_started_at),
            ).fetchall()
        ]
        if not message_ids:
            return 0

        marked = 0
        # Batch in groups of 100 (Telegram API limit)
        for batch_start in range(0, len(message_ids), 100):
            batch = message_ids[batch_start : batch_start + 100]
            results = await self._client.get_messages(dialog_id, ids=batch)

            now = int(time.time())
            with self._conn:  # atomic per-dialog batch
                for queried_id, returned_msg in zip(batch, results, strict=False):
                    if returned_msg is None:
                        self._conn.execute(_MARK_DELETED_SQL, (now, dialog_id, queried_id))
                        marked += 1
        return marked
