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
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol, cast, runtime_checkable

from telethon import events  # type: ignore[import-untyped]
from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    PeerChannel,
    PeerChat,
    TypeInputChannel,
    UpdateChannel,
    UpdateChat,
    UpdateDeleteScheduledMessages,
    UpdateDialogPinned,
    UpdateDialogUnreadMark,
    UpdateMessageReactions,
    UpdateNewScheduledMessage,
    UpdatePinnedDialogs,
    UpdatePinnedForumTopic,
    UpdateReadChannelInbox,
    UpdateReadHistoryInbox,
    UpdateTranscribedAudio,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .activity_contracts import InputPeerResolver
from .entity_store import EntitySnapshot, upsert_entity_snapshots
from .flood import TelegramRpcThrottled
from .history_enrollment import ensure_automatic_dm_enrollment
from .hydration_queue import HydrationPriority
from .messages.sqlite_repository import (
    apply_message_transcription,
    insert_messages_with_fts,
    list_undeleted_message_ids,
    mark_message_deleted,
    persist_edited_message,
    read_message_out,
    read_message_text,
    stage_message_transcription,
)
from .messages.telegram_adapter import (
    PeerNameClient as _PeerNameClient,
)
from .messages.telegram_adapter import (
    build_forward_entity_name_map as _build_fwd_entity_map,
)
from .messages.telegram_adapter import (
    extract_message_row,
)
from .reactions.contracts import ReactionAggregate
from .reactions.persistence import replace_reaction_aggregates
from .reactions.projection import project_reaction_aggregates
from .read_state import apply_read_cursor
from .realtime_history_policy import (
    RealtimeBodyEvent,
    RealtimeHistoryCoverage,
    allows_existing_body_update,
    allows_missing_body_insert,
    allows_new_message,
    realtime_history_coverage,
)
from .resolver import latinize
from .scheduled_messages import (
    mark_scheduled_messages_removed,
    scheduled_dialog_id,
    scheduled_message_dialog_id,
    upsert_scheduled_message,
    verify_scheduled_publication,
)
from .telethon_dialog import classify_dialog_type
from .unread_state import apply_unread_facts

logger = logging.getLogger(__name__)


@runtime_checkable
class _PeerContainer(Protocol):
    peer: object


@runtime_checkable
class _SenderLike(Protocol):
    first_name: str | None
    last_name: str | None
    username: str | None


class _MessageLike(Protocol):
    id: int
    message: str | None
    date: datetime | None
    edit_date: datetime | None
    action: object | None
    reactions: object | None
    reply_to: object | None


class _NewMessageEvent(Protocol):
    chat_id: int | None
    is_private: bool
    message: _MessageLike

    async def get_sender(self) -> _SenderLike | None: ...


class _EditedMessageEvent(Protocol):
    chat_id: int | None
    message: _MessageLike


class _DeletedMessagesEvent(Protocol):
    chat_id: int | None
    deleted_ids: Sequence[int]


class _ReadMessageEvent(Protocol):
    chat_id: int | None
    max_id: int | None
    contents: bool


class _OutboxReadEvent(Protocol):
    chat_id: int | None
    is_private: bool | None
    max_id: int | None


class _RawReactionUpdate(Protocol):
    peer: object | None
    msg_id: int | None


class _ChannelChatUpdateLike(Protocol):
    channel_id: int


class _ChatUpdateLike(Protocol):
    chat_id: int


class _InboxReadUpdateLike(Protocol):
    peer: object
    max_id: int | None
    still_unread_count: int | None


class _ChannelInboxReadUpdateLike(Protocol):
    channel_id: int
    max_id: int | None
    still_unread_count: int | None


class _ForumTopicPinnedUpdateLike(Protocol):
    peer: object | None
    topic_id: int | None
    pinned: bool


def _is_valid_nonnegative_int(value: object, *, allow_none: bool = False) -> bool:
    """Validate Telegram integer facts without coercing malformed values."""
    return (allow_none and value is None) or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _apply_inbox_read_fact(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    max_id: int,
    unread_count: int | None,
    observed_at: int,
) -> tuple[int, int]:
    """Apply the composite Telegram inbox-read fact in one transaction.

    This is the application seam for the raw update: it delegates cursor and
    exact unread-count persistence to their canonical primitives, while the
    caller owns the transaction boundary and commit.

    ``create_missing=True`` is deliberate for the metadata side: an update
    can arrive before dialog snapshot sync, so it may create a thin
    ``dialogs`` row marked for refresh. The cursor side remains UPDATE-only
    and therefore does nothing when ``synced_dialogs`` has no row.
    """
    cursor_rowcount = apply_read_cursor(conn, dialog_id, "inbox", max_id)
    unread_rowcount = apply_unread_facts(
        conn,
        dialog_id,
        unread_count=unread_count,
        observed_at=observed_at,
        create_missing=True,
    )
    conn.execute(_UPDATE_LAST_EVENT_SQL, (observed_at, dialog_id))
    return cursor_rowcount, unread_rowcount


@dataclass(frozen=True, slots=True)
class _TranscriptionEvent:
    """Validated final transcription payload from Telegram."""

    dialog_id: int
    message_id: int
    text: str
    transcription_id: int


class _EventHandlerClient(Protocol):
    def add_event_handler(self, _callback: object, _event: object) -> None: ...

    def remove_event_handler(self, _callback: object) -> None: ...

    async def get_messages(self, *_args: object, **_kwargs: object) -> object: ...

    async def __call__(self, _request: object) -> object: ...


class _RealtimeHistoryStatusReader(Protocol):
    """Narrow SQLite capability used by body-event orchestration."""

    def read_status(self, dialog_id: int) -> tuple[str | None, bool]: ...


class _SQLiteRealtimeHistoryStatusReader:
    """Read only the sync status needed by the realtime history policy."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def read_status(self, dialog_id: int) -> tuple[str | None, bool]:
        row = cast(
            tuple[str | None, int | None] | None,
            self._conn.execute(
                """SELECT sd.status, fhe.enabled
                   FROM synced_dialogs sd
                   LEFT JOIN full_history_enrollment fhe USING(dialog_id)
                   WHERE sd.dialog_id = ?""",
                (dialog_id,),
            ).fetchone(),
        )
        if row is None:
            return None, False
        return cast(str | None, row[0]), bool(row[1])


_DM_AUTO_ENROLL_SENDER_EXCEPTIONS: tuple[type[BaseException], ...] = (
    RPCError,
    Exception,
)


def _first_non_empty_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value != "":
            return value
    return None


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------


_UPDATE_LAST_EVENT_SQL = "UPDATE synced_dialogs SET last_event_at=? WHERE dialog_id=?"

_SELECT_SYNCED_DIALOGS_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "LEFT JOIN full_history_enrollment fhe USING(dialog_id) "
    "WHERE sd.status != 'access_lost' AND (fhe.enabled = 1 OR sd.status = 'own_only')"
)

_SELECT_SYNCED_ONLY_SQL = (
    "SELECT sd.dialog_id FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.status = 'synced'"
)

# ---------------------------------------------------------------------------
# Phase 42 SQL — dialogs event writes. Body metadata remains UPDATE-only;
# unread metadata uses the shared seam below, which may create a thin row with
# snapshot_at=NULL and needs_refresh=1 without unhiding an existing row.
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
        client: _EventHandlerClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        input_peer_resolver: InputPeerResolver,
    ) -> None:
        self._client = client
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._input_peer_resolver = input_peer_resolver
        self._shutdown_event.is_set()
        self._synced_dialog_ids: set[int] = set()
        self._realtime_history_status: _RealtimeHistoryStatusReader = _SQLiteRealtimeHistoryStatusReader(conn)

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
        self._client.add_event_handler(
            self.on_raw_transcribed_audio,
            events.Raw(types=[UpdateTranscribedAudio]),
        )
        self._client.add_event_handler(
            self.on_raw_new_scheduled_message,
            events.Raw(types=[UpdateNewScheduledMessage]),
        )
        self._client.add_event_handler(
            self.on_raw_delete_scheduled_messages,
            events.Raw(types=[UpdateDeleteScheduledMessages]),
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
        self._client.remove_event_handler(self.on_raw_transcribed_audio)
        self._client.remove_event_handler(self.on_raw_new_scheduled_message)
        self._client.remove_event_handler(self.on_raw_delete_scheduled_messages)
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
        rows = cast(list[tuple[int]], self._conn.execute(_SELECT_SYNCED_DIALOGS_SQL).fetchall())
        self._synced_dialog_ids = {int(dialog_id) for (dialog_id,) in rows}

    def _realtime_coverage(self, dialog_id: int) -> RealtimeHistoryCoverage:
        """Resolve current DB status before a message-body event."""
        status, enabled = self._realtime_history_status.read_status(dialog_id)
        return realtime_history_coverage(status, enabled)

    @staticmethod
    def _metadata_realtime_allowed(coverage: RealtimeHistoryCoverage) -> bool:
        """Metadata follows an admitted realtime coverage, not raw status text."""
        return coverage is not RealtimeHistoryCoverage.NO_REALTIME_HISTORY

    def _auto_enroll_dm(self, dialog_id: int, sender: _SenderLike | None = None) -> bool:
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
            outcome = ensure_automatic_dm_enrollment(self._conn, dialog_id)
            self._conn.commit()
            if outcome.enabled and outcome.action in {"queue_full_history", "preserved_enabled_intent"}:
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
            return False

        if sender is None:
            return True
        try:
            first = _first_non_empty_str(getattr(sender, "first_name", None)) or ""
            last = _first_non_empty_str(getattr(sender, "last_name", None)) or ""
            username = _first_non_empty_str(getattr(sender, "username", None))
            name: str | None = f"{first} {last}".strip() or None
            entity_type_str = classify_dialog_type(sender).value
            with self._conn:
                upsert_entity_snapshots(
                    self._conn,
                    (
                        EntitySnapshot(
                            entity_id=dialog_id,
                            entity_type=entity_type_str,
                            name=name,
                            username=username,
                            name_normalized=latinize(name) if name else None,
                            updated_at=int(time.time()),
                        ),
                    ),
                )
            logger.info("dm_auto_enroll_entity dialog_id=%d name=%r", dialog_id, name)
        except Exception:
            logger.exception("dm_auto_enroll_entity_failed dialog_id=%d", dialog_id)
        return True

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_scheduled_publication_if_needed(
        conn: sqlite3.Connection,
        dialog_id: int,
        message: _MessageLike,
        *,
        now: int | None = None,
    ) -> None:
        if not bool(getattr(message, "from_scheduled", False)):
            return
        with conn:
            if now is None:
                verify_scheduled_publication(conn, dialog_id, int(message.id))
            else:
                verify_scheduled_publication(conn, dialog_id, int(message.id), now=now)

    async def on_new_message(self, event: _NewMessageEvent) -> None:
        """Handle a NewMessage event: INSERT OR REPLACE into messages table.

        For enrolled dialogs: writes the message to sync.db immediately.
        For unenrolled private (DM) dialogs: auto-enrolls them so FullSyncWorker
        picks up the full history in its next batch cycle.
        Updates synced_dialogs.last_event_at in the same transaction.
        """
        dialog_id = event.chat_id
        if dialog_id is None:
            return
        msg = event.message
        coverage = await self._new_message_coverage(dialog_id, event)
        if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
            return
        if not allows_new_message(coverage, outgoing=bool(getattr(msg, "out", False))):
            self._project_denied_new_message_metadata(dialog_id, msg, coverage)
            return

        try:
            entity_name_map = await _build_fwd_entity_map(msg, cast(_PeerNameClient, self._client))
            extracted = extract_message_row(dialog_id, msg, entity_name_map=entity_name_map)
            # Forward enrichment suspends the handler; status may have changed
            # while Telegram was queried, so gate the mutation again.
            coverage = self._realtime_coverage(dialog_id)
            if not allows_new_message(coverage, outgoing=bool(getattr(msg, "out", False))):
                self._project_denied_new_message_metadata(dialog_id, msg, coverage)
                return
            now = int(time.time())

            with self._conn:
                if not allows_new_message(
                    self._realtime_coverage(dialog_id), outgoing=bool(getattr(msg, "out", False))
                ):
                    return
                insert_messages_with_fts(self._conn, [extracted], priority=HydrationPriority.FOREGROUND)
                # A normal message carrying from_scheduled is the verification
                # point for the untrusted sent_messages hint retained by the
                # scheduled-queue delete update.
                self._verify_scheduled_publication_if_needed(self._conn, dialog_id, msg, now=now)
                # Phase 42 EVENTS-04: advance dialogs.last_message_at monotonically.
                # MAX(COALESCE(..., 0), new_ts) ensures no regression on out-of-order
                # events. UPDATE matches 0 rows when the dialog is not yet bootstrapped
                # (no dialogs row) — silent no-op; bootstrap is the sole row creator.
                self._project_new_message_metadata(dialog_id, msg, now)
                self._record_body_event(dialog_id, now)

            logger.debug("event_new dialog_id=%d message_id=%d", dialog_id, msg.id)
        except Exception:
            logger.exception("event_new_failed dialog_id=%s", dialog_id)

    async def _new_message_coverage(
        self,
        dialog_id: int,
        event: _NewMessageEvent,
    ) -> RealtimeHistoryCoverage:
        coverage = self._realtime_coverage(dialog_id)
        if coverage is not RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
            return coverage
        self._verify_scheduled_publication_if_needed(self._conn, dialog_id, event.message)
        status, _ = self._realtime_history_status.read_status(dialog_id)
        if event.is_private and status is None:
            sender = None
            try:
                sender = await event.get_sender()
            except _DM_AUTO_ENROLL_SENDER_EXCEPTIONS:
                logger.debug("dm_auto_enroll_sender_fetch_failed dialog_id=%d", dialog_id)
            self._auto_enroll_dm(dialog_id, sender=sender)
            coverage = self._realtime_coverage(dialog_id)
        return coverage

    def _project_denied_new_message_metadata(
        self,
        dialog_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
    ) -> None:
        if not self._metadata_realtime_allowed(coverage):
            return
        now = int(time.time())
        with self._conn:
            self._project_new_message_metadata(dialog_id, msg, now)

    def _update_last_message_timestamp(self, dialog_id: int, now: int, msg_date: datetime | None) -> None:
        if msg_date is not None:
            self._conn.execute(
                _UPDATE_DIALOG_LAST_MESSAGE_AT_SQL,
                (int(msg_date.timestamp()), now, dialog_id),
            )

    def _record_body_event(self, dialog_id: int, now: int) -> None:
        self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

    def _project_new_message_metadata(self, dialog_id: int, msg: _MessageLike, now: int) -> None:
        """Project dialog/topic metadata independently of message-body scope."""
        self._update_last_message_timestamp(dialog_id, now, msg.date)
        self._handle_topic_message_action(dialog_id, msg, now)

    def _handle_topic_message_action(
        self,
        dialog_id: int,
        msg: _MessageLike,
        now: int,
    ) -> None:
        action = cast(object | None, getattr(msg, "action", None))
        if isinstance(action, MessageActionTopicCreate):
            self._handle_topic_create_action(dialog_id, msg, now, action)
            return
        if isinstance(action, MessageActionTopicEdit):
            self._handle_topic_edit_action(dialog_id, msg, now, action)

    def _handle_topic_create_action(
        self,
        dialog_id: int,
        msg: _MessageLike,
        now: int,
        action: MessageActionTopicCreate,
    ) -> None:
        topic_id = int(msg.id)
        if topic_id <= 0:
            return
        msg_date = msg.date
        topic_timestamp = int(msg_date.timestamp()) if msg_date is not None else now
        self._conn.execute(
            _UPSERT_TOPIC_METADATA_SQL,
            {
                "dialog_id": dialog_id,
                "topic_id": topic_id,
                "title": action.title or "Topic",
                "icon_emoji_id": action.icon_emoji_id,
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
        msg: _MessageLike,
        now: int,
        action: MessageActionTopicEdit,
    ) -> None:
        reply_to = msg.reply_to
        if reply_to is None:
            # Defensive: some MessageActionTopicEdit events carry no
            # reply_to; without it we cannot identify the target topic.
            logger.debug(
                "event_topic_edit_skipped reason=no_reply_to dialog_id=%d",
                dialog_id,
            )
            return

        topic_id_raw = cast(int | None, getattr(reply_to, "reply_to_msg_id", None))
        if topic_id_raw is None:
            logger.debug(
                "event_topic_edit_skipped reason=no_reply_to_msg_id dialog_id=%d",
                dialog_id,
            )
            return

        topic_id = int(topic_id_raw)
        if action.hidden:
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
        edit_title = action.title
        edit_icon = action.icon_emoji_id
        self._conn.execute(
            _UPDATE_TOPIC_METADATA_EDIT_SQL,
            (edit_title, edit_icon, now, now, dialog_id, topic_id, now),
        )
        logger.info(
            "event_topic_edit dialog_id=%d topic_id=%d",
            dialog_id,
            topic_id,
        )

    async def on_message_edited(self, event: _EditedMessageEvent) -> None:
        """Handle a MessageEdited event: version old text, update messages row.

        Three cases:
        1. Message not in sync.db yet: INSERT it with current text, no version history.
        2. Text unchanged: no-op (covers service edits, reactions updates, etc.).
        3. Text changed: insert old_text into message_versions, update messages.text.

        All operations in a single transaction.
        """
        dialog_id = event.chat_id
        if dialog_id is None:
            return

        try:
            msg = event.message
            message_id = int(msg.id)
            new_text = msg.message
            now = int(time.time())
            coverage = self._realtime_coverage(dialog_id)
            if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
                return

            # Resolve async data BEFORE opening transaction — SQLite's synchronous
            # driver cannot safely suspend inside a `with self._conn:` block while
            # another coroutine may call into the same connection.
            existing = read_message_text(self._conn, dialog_id, message_id)
            existing_out = read_message_out(self._conn, dialog_id, message_id)

            if not existing.found:
                if await self._insert_missing_edited_message(dialog_id, msg, coverage, now):
                    logger.info(
                        "event_edit_new dialog_id=%d message_id=%d (not in sync.db, inserted)",
                        dialog_id,
                        message_id,
                    )
                return

            old_text = existing.text
            if old_text == new_text:
                # No text change. Two sub-cases:
                # 1. msg.reactions present -> reactions-only edit; apply delta
                #    (Phase 39.2-01 AC-1 via edited path, AC-2 removal via empty results).
                # 2. msg.reactions is None -> service edit / media caption etc.; no-op
                #    (regression guard AC-8).
                self._apply_reaction_only_edit(dialog_id, message_id, msg, coverage, existing_out.outgoing, now)
                return

            next_ver = await self._persist_changed_edit(
                dialog_id,
                msg,
                coverage,
                existing_out.outgoing,
                old_text,
                now,
            )
            if next_ver is None:
                return

            logger.debug(
                "event_edit dialog_id=%d message_id=%d version=%d",
                dialog_id,
                message_id,
                next_ver,
            )
        except Exception:
            logger.exception("event_edit_failed dialog_id=%s", dialog_id)

    async def _insert_missing_edited_message(
        self,
        dialog_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
        now: int,
    ) -> bool:
        outgoing = bool(getattr(msg, "out", False))
        if not allows_missing_body_insert(coverage, RealtimeBodyEvent.EDIT, outgoing=outgoing):
            return False
        entity_name_map = await _build_fwd_entity_map(msg, cast(_PeerNameClient, self._client))
        extracted = extract_message_row(dialog_id, msg, entity_name_map=entity_name_map)
        coverage = self._realtime_coverage(dialog_id)
        if not allows_missing_body_insert(coverage, RealtimeBodyEvent.EDIT, outgoing=outgoing):
            return False
        with self._conn:
            if not allows_missing_body_insert(
                self._realtime_coverage(dialog_id), RealtimeBodyEvent.EDIT, outgoing=outgoing
            ):
                return False
            insert_messages_with_fts(self._conn, [extracted], priority=HydrationPriority.FOREGROUND)
            self._record_body_event(dialog_id, now)
        return True

    def _apply_reaction_only_edit(  # noqa: PLR0913, PLR0917
        self,
        dialog_id: int,
        message_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
        outgoing: bool,
        now: int,
    ) -> None:
        reactions_obj = msg.reactions
        if reactions_obj is None or not allows_existing_body_update(
            coverage,
            RealtimeBodyEvent.REACTION,
            outgoing=outgoing,
        ):
            return
        aggregates = project_reaction_aggregates(reactions_obj)
        with self._conn:
            replace_reaction_aggregates(self._conn, dialog_id, message_id, aggregates)
            self._record_body_event(dialog_id, now)
        logger.debug(
            "event_edit_reactions dialog_id=%d message_id=%d count=%d",
            dialog_id,
            message_id,
            len(aggregates),
        )

    async def _persist_changed_edit(  # noqa: PLR0913, PLR0917
        self,
        dialog_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
        outgoing: bool,
        old_text: str | None,
        now: int,
    ) -> int | None:
        edit_date_raw = msg.edit_date
        edit_date_unix = int(edit_date_raw.timestamp()) if edit_date_raw is not None else now
        entity_name_map = await _build_fwd_entity_map(msg, cast(_PeerNameClient, self._client))
        extracted = extract_message_row(dialog_id, msg, entity_name_map=entity_name_map)
        coverage = self._realtime_coverage(dialog_id)
        if not allows_existing_body_update(coverage, RealtimeBodyEvent.EDIT, outgoing=outgoing):
            return None
        if coverage is RealtimeHistoryCoverage.OWN_OUTGOING:
            # The incoming edit object may omit Telethon's ``out`` flag;
            # preserve the canonical outgoing classification of the row.
            extracted = replace(extracted, message=replace(extracted.message, out=1))
        with self._conn:
            if not allows_existing_body_update(
                self._realtime_coverage(dialog_id), RealtimeBodyEvent.EDIT, outgoing=outgoing
            ):
                return None
            next_ver = persist_edited_message(
                self._conn,
                extracted,
                old_text=old_text,
                edit_date=edit_date_unix,
                priority=HydrationPriority.FOREGROUND,
            )
            self._record_body_event(dialog_id, now)
        return next_ver

    async def on_message_deleted(self, event: _DeletedMessagesEvent) -> None:
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

        coverage = self._realtime_coverage(dialog_id)
        if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
            return

        try:
            now = int(time.time())
            allowed_ids = list(event.deleted_ids)
            if coverage is RealtimeHistoryCoverage.OWN_OUTGOING:
                allowed_ids = [
                    msg_id for msg_id in allowed_ids if read_message_out(self._conn, dialog_id, int(msg_id)).outgoing
                ]
            if not allowed_ids:
                return

            with self._conn:
                for msg_id in allowed_ids:
                    mark_message_deleted(self._conn, dialog_id, msg_id, now)
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

            logger.info("event_delete dialog_id=%d count=%d", dialog_id, len(event.deleted_ids))
        except Exception:
            logger.exception("event_delete_failed dialog_id=%s", dialog_id)

    async def on_raw_new_scheduled_message(self, update: object) -> None:
        """Mirror create/edit/reschedule updates without touching sent history."""
        message = cast(object | None, getattr(update, "message", None))
        if message is None:
            return
        dialog_id = scheduled_message_dialog_id(message)
        if dialog_id is None:
            logger.warning("scheduled_new_missing_peer message_id=%s", cast(object, getattr(message, "id", None)))
            return
        try:
            with self._conn:
                upsert_scheduled_message(self._conn, dialog_id, message)
            message_id_attr = "id"
            logger.info(
                "scheduled_new dialog_id=%d message_id=%d",
                dialog_id,
                int(cast(int, getattr(message, message_id_attr))),
            )
        except Exception:
            logger.exception("scheduled_new_failed dialog_id=%s", dialog_id)

    async def on_raw_delete_scheduled_messages(self, update: object) -> None:
        """Retain cancellation/publication evidence from a queue-removal update."""
        dialog_id = scheduled_dialog_id(getattr(update, "peer", None))
        message_ids = cast(Sequence[int] | None, getattr(update, "messages", None))
        if dialog_id is None or not message_ids:
            return
        sent_message_ids = cast(Sequence[int] | None, getattr(update, "sent_messages", None))
        try:
            mark_scheduled_messages_removed(self._conn, dialog_id, message_ids, sent_message_ids)
            logger.info("scheduled_removed dialog_id=%d count=%d", dialog_id, len(message_ids))
        except Exception:
            logger.exception("scheduled_removed_failed dialog_id=%s", dialog_id)

    async def on_message_read(self, event: _ReadMessageEvent) -> None:
        """Handle MessageRead(inbox=True): update read_inbox_max_id monotonically.

        Monotonic write via `MAX(COALESCE(existing, 0), incoming)` ensures the
        stored value never regresses — protects against out-of-order events and
        against bootstrap races where an older GetPeerDialogsRequest response
        could otherwise overwrite a newer live event.

        ``UpdateReadMessagesContents`` is also normalized as a
        ``MessageRead(inbox=True)`` event, but it has no peer and only signals
        that message contents were opened (for example, a voice note). It is
        not a dialog read-cursor movement. Peerless synthetic or malformed
        events are likewise ignored quietly at debug level.
        """
        contents = bool(getattr(event, "contents", False))
        if contents:
            logger.debug(
                "event_read_contents_ignored chat_id=%s max_id=%s — no dialog read cursor applied",
                getattr(event, "chat_id", None),
                getattr(event, "max_id", None),
            )
            return

        dialog_id = cast(int | None, getattr(event, "chat_id", None))

        if dialog_id is None:
            logger.debug(
                "event_read_without_chat_id contents=%s max_id=%s — no dialog read cursor applied",
                contents,
                getattr(event, "max_id", None),
            )
            return

        if dialog_id not in self._synced_dialog_ids:
            return

        try:
            now = int(time.time())
            with self._conn:
                max_id = cast(int | None, getattr(event, "max_id", None))
                if max_id is None:
                    return
                rowcount = apply_read_cursor(self._conn, dialog_id, "inbox", max_id)
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
            if rowcount > 0:
                logger.debug("event_read dialog_id=%d max_id=%d", dialog_id, max_id)
            else:
                logger.warning(
                    "event_read_no_row dialog_id=%d max_id=%d — UPDATE matched 0 rows",
                    dialog_id,
                    max_id,
                )
        except Exception:
            logger.exception("event_read_failed dialog_id=%s", dialog_id)

    async def on_outbox_read(self, event: _OutboxReadEvent) -> None:
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
          versions (mirror of the inbox handler's quirk). Log warning, bail;
          dialog reconciliation remains the fallback source of cursor truth.
        - Exceptions wrapped in ``try/except Exception`` (not bare ``except``,
          not swallowing ``asyncio.CancelledError``); observable via the
          ``event_outbox_read_failed`` log.
        """
        dialog_id = event.chat_id

        if dialog_id is None:
            logger.warning(
                "event_outbox_read_null_chat_id max_id=%s — PM outbox read "
                "position could not be updated from this real-time event because "
                "Telethon did not provide chat_id; dialog reconciliation will "
                "refresh cursors from Telegram Dialog state",
                event.max_id,
            )
            return

        # PeerUser-only filter: when the Telethon Event exposes is_private,
        # use it; otherwise rely on the synced_dialog_ids check below (non-DM
        # synced dialogs aren't tracked for outbox cursors — the read paths
        # that consume this surface are DM-only).
        is_private = cast(bool | None, getattr(event, "is_private", None))
        if is_private is False:
            return

        if dialog_id not in self._synced_dialog_ids:
            logger.debug(
                "event_outbox_read_unsynced dialog_id=%d max_id=%s",
                dialog_id,
                event.max_id,
            )
            return

        max_id = event.max_id
        if max_id is None:
            return

        try:
            now = int(time.time())
            with self._conn:
                rowcount = apply_read_cursor(self._conn, dialog_id, "outbox", max_id)
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
            if rowcount > 0:
                logger.debug("event_outbox_read dialog_id=%d max_id=%d", dialog_id, max_id)
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

    async def on_raw_reaction_update(self, update: _RawReactionUpdate) -> None:
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
        event_ids = self._reaction_event_ids(update)
        if event_ids is None:
            return
        dialog_id, msg_id = event_ids

        coverage = self._realtime_coverage(dialog_id)
        if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
            logger.debug(
                "raw_reaction_update_skipped_unsynced dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return

        existing_out = read_message_out(self._conn, dialog_id, int(msg_id))
        if coverage is RealtimeHistoryCoverage.OWN_OUTGOING and not allows_existing_body_update(
            coverage,
            RealtimeBodyEvent.REACTION,
            outgoing=existing_out.outgoing,
        ):
            return

        msg = await self._fetch_reaction_message(dialog_id, msg_id)
        if msg is None:
            logger.debug(
                "raw_reaction_update_missing_message dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return

        try:
            aggregates = project_reaction_aggregates(msg.reactions)
            if not self._apply_reaction_event(dialog_id, msg_id, aggregates):
                return
            logger.debug(
                "event_raw_reaction dialog_id=%d message_id=%d count=%d",
                dialog_id,
                msg_id,
                len(aggregates),
            )
        except Exception:
            logger.exception(
                "event_raw_reaction_apply_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )

    @staticmethod
    def _reaction_event_ids(update: _RawReactionUpdate) -> tuple[int, int] | None:
        peer = update.peer
        msg_id = update.msg_id
        if peer is None or msg_id is None:
            return None
        try:
            return int(cast(int, get_peer_id(peer))), int(msg_id)
        except TypeError, ValueError:
            logger.debug("raw_reaction_update_unparseable_peer peer=%r", peer)
            return None

    async def _fetch_reaction_message(self, dialog_id: int, msg_id: int) -> _MessageLike | None:
        try:
            result = cast(Sequence[_MessageLike | None], await self._client.get_messages(dialog_id, ids=[msg_id]))
        except TelegramRpcThrottled as exc:
            logger.warning(
                "raw_reaction_floodwait dialog_id=%d message_id=%d seconds=%s",
                dialog_id,
                msg_id,
                exc.retry_after_seconds,
            )
            return None
        except RPCError, RuntimeError:
            logger.exception(
                "event_raw_reaction_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return None
        return result[0] if result else None

    def _apply_reaction_event(
        self,
        dialog_id: int,
        msg_id: int,
        aggregates: Sequence[ReactionAggregate],
    ) -> bool:
        coverage = self._realtime_coverage(dialog_id)
        existing_out = read_message_out(self._conn, dialog_id, msg_id)
        if not allows_existing_body_update(coverage, RealtimeBodyEvent.REACTION, outgoing=existing_out.outgoing):
            return False
        if coverage is not RealtimeHistoryCoverage.FULL_HISTORY and not existing_out.found:
            return False
        now = int(time.time())
        with self._conn:
            replace_reaction_aggregates(self._conn, dialog_id, msg_id, aggregates)
            self._record_body_event(dialog_id, now)
        return True

    async def on_raw_transcribed_audio(self, update: UpdateTranscribedAudio) -> None:
        """Capture one final Telegram transcription fact under current policy.

        ``received_at`` is local technical arrival time only: Telegram does not
        provide an event timestamp or ordering guarantee here. The latest
        received fact is projected on canonical message writes regardless of a
        later access-policy change.
        """
        event_fields = self._transcription_event_fields(update)
        if event_fields is None:
            return
        dialog_id = event_fields.dialog_id
        msg_id = event_fields.message_id

        coverage = self._realtime_coverage(dialog_id)
        if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
            logger.debug(
                "raw_transcribed_audio_skipped_unsynced dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
            return

        try:
            now = int(time.time())
            row = read_message_text(self._conn, dialog_id, msg_id)
            if row.found:
                self._update_existing_transcription(
                    event_fields,
                    row.text,
                    coverage,
                    now,
                )
            elif coverage is RealtimeHistoryCoverage.FULL_HISTORY:
                self._stage_missing_transcription(event_fields, now)
            else:
                return
            logger.debug(
                "event_raw_transcribed_audio dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
        except Exception:
            logger.exception(
                "event_raw_transcribed_audio_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )

    def _stage_missing_transcription(
        self,
        event: _TranscriptionEvent,
        now: int,
    ) -> None:
        """Store a FULL_HISTORY fact only while capture authorization remains."""
        with self._conn:
            if self._realtime_coverage(event.dialog_id) is not RealtimeHistoryCoverage.FULL_HISTORY:
                return
            if read_message_text(self._conn, event.dialog_id, event.message_id).found:
                return
            stage_message_transcription(
                self._conn,
                event.dialog_id,
                event.message_id,
                transcribed_text=event.text,
                transcription_id=event.transcription_id,
                received_at=now,
            )
            self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, event.dialog_id))

    def _update_existing_transcription(
        self,
        event: _TranscriptionEvent,
        old_text: str | None,
        coverage: RealtimeHistoryCoverage,
        now: int,
    ) -> None:
        """Upsert a final fact and project changed text under current policy."""
        existing_out = read_message_out(self._conn, event.dialog_id, event.message_id)
        if not allows_existing_body_update(
            coverage,
            RealtimeBodyEvent.TRANSCRIPTION,
            outgoing=existing_out.outgoing,
        ):
            return
        with self._conn:
            if not allows_existing_body_update(
                self._realtime_coverage(event.dialog_id),
                RealtimeBodyEvent.TRANSCRIPTION,
                outgoing=existing_out.outgoing,
            ):
                return
            apply_message_transcription(
                self._conn,
                event.dialog_id,
                event.message_id,
                transcribed_text=event.text,
                transcription_id=event.transcription_id,
                received_at=now,
            )
            if old_text != event.text:
                self._conn.execute(_UPDATE_LAST_EVENT_SQL, (now, event.dialog_id))

    @staticmethod
    def _transcription_event_fields(update: UpdateTranscribedAudio) -> _TranscriptionEvent | None:
        peer: object = getattr(update, "peer", None)
        msg_id: object = getattr(update, "msg_id", None)
        text_value: object = getattr(update, "text", None)
        transcription_id: object = getattr(update, "transcription_id", None)
        if bool(getattr(update, "pending", False)):
            return None
        if peer is None or not isinstance(msg_id, int) or isinstance(msg_id, bool):
            return None
        if not isinstance(text_value, str) or not text_value.strip():
            return None
        if not isinstance(transcription_id, int) or isinstance(transcription_id, bool):
            return None
        try:
            dialog_id = int(cast(int, get_peer_id(peer)))
        except TypeError, ValueError:
            logger.debug("raw_transcribed_audio_unparseable_peer peer=%r", peer)
            return None
        return _TranscriptionEvent(dialog_id, msg_id, text_value.strip(), transcription_id)

    # ------------------------------------------------------------------
    # Phase 42: dialog metadata Raw handlers (EVENTS-01, EVENTS-02, EVENTS-03)
    # ------------------------------------------------------------------

    def _dialog_id_from_peer(self, peer: object | _PeerContainer | None) -> int | None:
        inner_peer = peer.peer if isinstance(peer, _PeerContainer) else peer
        if inner_peer is None:
            return None
        return int(cast(int, get_peer_id(inner_peer)))

    def _collect_synced_pinned_dialog_ids(self, order: Sequence[object]) -> list[int]:
        pinned_ids: list[int] = []
        for dialog_peer in order:
            inner_peer = dialog_peer.peer if isinstance(dialog_peer, _PeerContainer) else dialog_peer
            try:
                dialog_id = int(cast(int, get_peer_id(inner_peer)))
            except TypeError, ValueError:
                continue
            if dialog_id in self._synced_dialog_ids:
                pinned_ids.append(dialog_id)
        return pinned_ids

    def _update_dialog_pinned(self, update: UpdateDialogPinned, now: int) -> None:
        dialog_id = self._dialog_id_from_peer(update.peer)
        if dialog_id is None or dialog_id not in self._synced_dialog_ids:
            return
        pinned = 1 if update.pinned else 0
        with self._conn:
            self._conn.execute(_UPDATE_DIALOG_PINNED_SQL, (pinned, now, dialog_id))
        logger.info("event_dialog_pinned dialog_id=%d pinned=%d", dialog_id, pinned)

    def _rewrite_pinned_dialogs(self, update: UpdatePinnedDialogs, now: int) -> None:
        order = update.order
        if order is None:
            logger.debug("event_pinned_dialogs_order_none — skip")
            return
        # folder_id=None means the main list; folder_id=1 means Archived, etc.
        # A folder-scoped update carries only pins *within* that folder, so we
        # must not use it to clear pins in other folders.
        folder_id = update.folder_id
        # Decode peers; gate by _synced_dialog_ids so we never UPDATE
        # rows for dialogs the daemon does not own.
        pinned_ids = self._collect_synced_pinned_dialog_ids(cast(Sequence[object], order))
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
        dialog_id = self._dialog_id_from_peer(update.peer)
        if dialog_id is None:
            return
        with self._conn:
            rowcount = apply_unread_facts(
                self._conn,
                dialog_id,
                unread_mark=getattr(update, "unread", None),
                observed_at=now,
                mark_needs_refresh=True,
                create_missing=True,
            )
        logger.info(
            "event_dialog_unread_mark dialog_id=%d unread=%s updated=%d",
            dialog_id,
            getattr(update, "unread", None),
            rowcount,
        )

    async def on_raw_dialog_pinned(self, update: object) -> None:
        """Phase 42 EVENTS-01: dialogs.pinned + needs_refresh from raw updates.

        Handles three update types:
          - UpdateDialogPinned: single dialog pin toggle. Gated on
            _synced_dialog_ids; UPDATE-only (bootstrap creates rows).
          - UpdatePinnedDialogs: full pinned-set replacement (order list).
            order=None → no actionable data, skip.
            order=[] → unpin everything via _CLEAR_ALL_PINS_SQL (NOT IN () is
            invalid SQLite).
          - UpdateDialogUnreadMark: persist Telegram's exact unread boolean,
            including False, and retain the existing needs_refresh signal.
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

    async def on_raw_channel_chat_update(self, update: _ChannelChatUpdateLike | _ChatUpdateLike) -> None:
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
                row = cast(
                    tuple[str | None, int | None] | None,
                    self._conn.execute(
                        "SELECT type, linked_chat_resolved_at FROM dialogs WHERE dialog_id = ?",
                        (dialog_id,),
                    ).fetchone(),
                )
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
        from telethon.tl.functions.channels import GetFullChannelRequest  # type: ignore[import-untyped]
        from telethon.tl.types import PeerChannel as _PeerChannel  # type: ignore[import-untyped]
        from telethon.utils import get_peer_id as _get_peer_id  # type: ignore[import-untyped]

        input_channel = cast(
            TypeInputChannel | None,
            await self._input_peer_resolver(dialog_id),
        )
        if input_channel is None:
            logger.debug("event_linked_chat_refresh_no_input_peer dialog_id=%d", dialog_id)
            return

        try:
            full_result = await self._client(GetFullChannelRequest(channel=input_channel))
        except TelegramRpcThrottled as exc:
            logger.warning(
                "event_linked_chat_refresh_flood dialog_id=%d flood_wait_seconds=%s",
                dialog_id,
                exc.retry_after_seconds,
            )
            return
        except Exception:
            logger.debug("event_linked_chat_refresh_failed dialog_id=%d", dialog_id, exc_info=True)
            return

        full_chat = getattr(full_result, "full_chat", None)
        raw = cast(int | None, getattr(full_chat, "linked_chat_id", None))
        normalised: int | None = None
        if raw is not None:
            if raw > 0:
                normalised = int(cast(int, _get_peer_id(_PeerChannel(raw))))
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

    async def on_raw_inbox_read(self, update: _InboxReadUpdateLike | _ChannelInboxReadUpdateLike) -> None:
        """Phase 42 EVENTS-02: UpdateReadHistoryInbox / UpdateReadChannelInbox.

        Captures both ``max_id`` and ``still_unread_count`` via the raw update
        (the high-level ``events.MessageRead`` wrapper drops the latter) and
        stores the exact Telegram facts in one transaction. Metadata facts are
        intentionally not gated on sync coverage; body-event gates remain
        unchanged. A missing or malformed ``max_id`` is ignored rather than
        converted into a fabricated cursor value. The metadata policy may
        create a thin ``dialogs`` row when the synced-dialog row is absent.
        """
        try:
            if isinstance(update, UpdateReadHistoryInbox):
                dialog_id = int(get_peer_id(update.peer))
            elif isinstance(update, UpdateReadChannelInbox):
                dialog_id = int(get_peer_id(PeerChannel(update.channel_id)))
            else:
                return
            still_unread_raw = getattr(update, "still_unread_count", None)
            if not _is_valid_nonnegative_int(still_unread_raw, allow_none=True):
                logger.debug(
                    "event_raw_inbox_read_invalid_still_unread_count dialog_id=%d value=%r",
                    dialog_id,
                    still_unread_raw,
                )
                return
            still_unread = cast(int | None, still_unread_raw)
            max_id_raw = getattr(update, "max_id", None)
            if not _is_valid_nonnegative_int(max_id_raw):
                logger.debug(
                    "event_raw_inbox_read_invalid_max_id dialog_id=%d max_id=%r",
                    dialog_id,
                    max_id_raw,
                )
                return
            max_id = cast(int, max_id_raw)
            now = int(time.time())
            with self._conn:
                cursor_rowcount, unread_rowcount = _apply_inbox_read_fact(
                    self._conn,
                    dialog_id,
                    max_id=max_id,
                    unread_count=still_unread,
                    observed_at=now,
                )
            logger.debug(
                "event_raw_inbox_read dialog_id=%d max_id=%d still_unread_count=%s cursor_updated=%d unread_updated=%d",
                dialog_id,
                max_id,
                still_unread,
                cursor_rowcount,
                unread_rowcount,
            )
        except Exception:
            logger.exception(
                "event_raw_inbox_read_failed update=%r",
                type(update).__name__,
            )

    async def on_raw_forum_topic_pinned(self, update: _ForumTopicPinnedUpdateLike) -> None:
        """Phase 42 EVENTS-05: UpdatePinnedForumTopic → topic_metadata.pinned.

        Gated on _synced_dialog_ids; UPDATE-only. Missing-row UPDATE matches
        0 rows without crashing — bootstrap / on_new_message UPSERT remain the
        sole row-creation paths.
        """
        try:
            if not isinstance(update, UpdatePinnedForumTopic):
                return
            peer = update.peer
            topic_id_raw = update.topic_id
            if peer is None or topic_id_raw is None:
                return
            dialog_id = int(cast(int, get_peer_id(peer)))
            if dialog_id not in self._synced_dialog_ids:
                return
            topic_id = int(topic_id_raw)
            pinned = 1 if update.pinned else 0
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

        dialog_rows = cast(list[tuple[int]], self._conn.execute(_SELECT_SYNCED_ONLY_SQL).fetchall())
        dialog_ids = [int(dialog_id) for (dialog_id,) in dialog_rows]

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
        message_ids = list(list_undeleted_message_ids(self._conn, dialog_id, scan_started_at))
        if not message_ids:
            return 0

        marked = 0
        # Batch in groups of 100 (Telegram API limit)
        for batch_start in range(0, len(message_ids), 100):
            batch = message_ids[batch_start : batch_start + 100]
            results = cast(
                "Sequence[_MessageLike | None]",
                await self._client.get_messages(dialog_id, ids=batch),
            )

            now = int(time.time())
            with self._conn:  # atomic per-dialog batch
                for queried_id, returned_msg in zip(batch, results, strict=False):
                    if returned_msg is None and mark_message_deleted(self._conn, dialog_id, queried_id, now):
                        marked += 1
        return marked


_EXPORTED_SYMBOLS = (
    EventHandlerManager,
    EventHandlerManager.register,
    EventHandlerManager.unregister,
    EventHandlerManager.refresh_synced_dialogs,
    EventHandlerManager.run_dm_gap_scan,
)
