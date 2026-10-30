"""EventHandlerManager — real-time event tracking engine for v1.5 Persistent Sync.

Registers three async Telethon event handlers against a live TelegramClient:
  - on_new_message:    INSERT OR REPLACE new messages into sync.db messages table
  - on_message_edited: version the old text into message_versions, update messages row
  - on_message_deleted: tombstone deleted messages while retaining their last known text

Personal-chat deletion updates can omit peer identity. They are resolved against
the durable incoming-human-DM policy when the local message ID has one unique
candidate. A periodic gap scan verifies any update that cannot be resolved safely.

Architecture:
- Standalone module so daemon.py stays focused on process lifecycle.
- EventHandlerManager is instantiated once per daemon run, registered BEFORE
  FullSyncWorker starts so no real-time events are missed during full sync.
- Complete SQLite write units run through the shared dedicated writer thread.
- In-memory _synced_dialog_ids set refreshed via refresh_synced_dialogs() from
  the daemon heartbeat loop.
"""

import asyncio
import logging
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from functools import wraps
from typing import Protocol, cast, runtime_checkable

from telethon import events  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    Channel,
    ChannelParticipantBanned,
    ChannelParticipantLeft,
    MessageActionTopicCreate,
    MessageActionTopicEdit,
    MessageService,
    PeerChannel,
    PeerChat,
    PeerUser,
    TypeInputChannel,
    TypePeer,
    UpdateChannel,
    UpdateChannelParticipant,
    UpdateChat,
    UpdateChatParticipant,
    UpdateDeleteScheduledMessages,
    UpdateDialogPinned,
    UpdateDialogUnreadMark,
    UpdateMessageReactions,
    UpdateNewChannelMessage,
    UpdateNewMessage,
    UpdateNewScheduledMessage,
    UpdateNotifySettings,
    UpdatePinnedDialogs,
    UpdatePinnedForumTopic,
    UpdatePinnedForumTopics,
    UpdateReadChannelInbox,
    UpdateReadHistoryInbox,
    UpdateTranscribedAudio,
    UpdateUser,
    UpdateUserName,
    User,
)
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .access_lifecycle import AccessLossEvidence, set_access_lost, unhide_after_realtime_presence
from .channel_full_siblings import (
    ChannelFullSiblingsToken,
    capture_channel_full_siblings_token,
    write_channel_full_siblings,
)
from .delivered_update import (
    current_delivered_update,
    delivered_observed_at,
    fail_delivered_update,
)
from .demand_wiring import DemandOfferSink, offer_durable_demand
from .dialog_directory import (
    apply_active_generation_pin_delta,
    apply_realtime_eligibility,
    clear_realtime_mute,
    record_realtime_pin_fence,
    sync_active_generation_pins_from_publication,
)
from .dialog_fact_observations import claim_dialog_fact
from .dialog_identity import capture_identity_baseline, publish_dialog_identity
from .dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation, ObservedText
from .dialog_pin_observations import claim_pin_delta, claim_pin_snapshot, ordered_pin_overlay
from .entity_profile.contracts import ProfileFieldObservation, SuppliedProfileUser
from .entity_profile.repository import (
    EntityProfileRepository,
    admit_profile_peer,
    bound_profile_account,
    personal_profile_peer,
)
from .entity_profile.updates import (
    ProfilePush,
    bundled_profile_users,
    mark_push_applied,
    profile_update_id,
    project_profile_update,
    project_user_profile,
    push_marker,
)
from .entity_store import PartialEntityIdentity, apply_partial_entity_identity
from .flood import TelegramRpcThrottled
from .folders.sqlite_repository import SQLiteFolderSnapshotRepository
from .history_enrollment import EnrollmentOutcome, ensure_automatic_dm_enrollment, full_history_enabled
from .hydration_queue import HydrationPriority
from .identity_observation import USERNAME_UNOBSERVED, observe_username
from .linked_chat_fact import LinkedChatWork, linked_chat_fact_owner
from .messages.sqlite_bundle import (
    find_unique_nonchannel_message_dialogs,
    insert_messages_with_fts,
    list_undeleted_message_ids,
    mark_message_deleted,
    persist_edited_message,
    read_message_out,
    read_message_text,
    record_message_deletions,
    record_nonchannel_deletions,
)
from .messages.sqlite_hydration import (
    apply_message_transcription,
    hydration_observation_current,
    stage_message_transcription,
)
from .messages.telegram_adapter import (
    extract_message_row,
)
from .models import DialogType
from .observation_order import delivered_observation_order
from .reactions.contracts import ReactionAggregate, ReactionAggregateSource, ReactionObservationBoundary
from .reactions.persistence import allocate_observation_boundary, replace_reaction_aggregates
from .reactions.projection import project_reaction_aggregates, project_reaction_visibility
from .read_state import apply_read_cursor
from .realtime_history_policy import (
    RealtimeBodyEvent,
    RealtimeHistoryCoverage,
    allows_existing_body_update,
    allows_missing_body_insert,
    allows_new_message,
    realtime_history_coverage,
)
from .runtime_observations import RuntimeObservationSink
from .scheduled_messages import (
    mark_scheduled_messages_removed,
    scheduled_dialog_has_due_work,
    scheduled_dialog_id,
    scheduled_message_dialog_id,
    upsert_scheduled_message,
    verify_scheduled_publication,
)
from .scheduled_write_fence import ScheduledMutationFence
from .sqlite_writer import SQLiteWriter
from .sync_transactions import require_write_transaction, write_savepoint, write_transaction
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcSource,
    rpc_scope,
)
from .telethon_dialog import classify_dialog_type, observe_dialog_identity
from .topics.catalog import TopicCatalogRepository
from .topics.contracts import is_topic_capable
from .topics.sqlite_repository import SQLiteTopicMetadataRepository
from .unread_state import apply_unread_facts

logger = logging.getLogger(__name__)


def _rpc_scope[**P, R](
    source: TelegramRpcSource,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Give one async operation an explicit source for nested Telegram calls."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with rpc_scope(source):
                return await func(*args, **kwargs)

        return wrapped

    return decorator


def _demand_root[**P, R](
    kind: DemandKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Install the code-owned root identity for one Telethon event callback."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            manager = cast(EventHandlerManager, args[0])
            try:
                await manager._wait_for_update_barrier()
                try:
                    current_demand_token()
                except UnclassifiedTelegramDemandError:
                    with demand_context(kind):
                        return await func(*args, **kwargs)
                return await func(*args, **kwargs)
            except BaseException:
                fail_delivered_update()
                raise

        return wrapped

    return decorator


class UpdateProcessingBarrier:
    """Startup-only gate that retains Telethon-delivered updates until account bind."""

    def __init__(self, *, closed: bool = False) -> None:
        self._opened = asyncio.Event()
        self._cancelled = False
        if not closed:
            self._opened.set()

    def open(self) -> None:
        if not self._cancelled:
            self._opened.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._opened.set()

    @property
    def is_open(self) -> bool:
        """Return whether callbacks pass the startup barrier immediately."""
        return self._opened.is_set()

    async def wait(self, shutdown_event: asyncio.Event) -> None:
        if self._cancelled or shutdown_event.is_set():
            raise asyncio.CancelledError
        if self._opened.is_set():
            return
        opened_wait = asyncio.create_task(self._opened.wait())
        shutdown_wait = asyncio.create_task(shutdown_event.wait())
        try:
            await asyncio.wait((opened_wait, shutdown_wait), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (opened_wait, shutdown_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(opened_wait, shutdown_wait, return_exceptions=True)
        if self._cancelled or shutdown_event.is_set():
            raise asyncio.CancelledError


def _acquisition[**P, R](
    kind: AcquisitionKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Refine a nested Telegram acquisition without replacing its event root."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with acquisition_context(kind):
                return await func(*args, **kwargs)

        return wrapped

    return decorator


@runtime_checkable
class _PeerContainer(Protocol):
    peer: object


@runtime_checkable
class _SenderLike(Protocol):
    first_name: str | None
    last_name: str | None
    username: str | None


@dataclass(frozen=True, slots=True)
class _DmIdentity:
    name: str | None
    username: str | None
    entity_type: str
    name_observed: bool
    username_observed: bool
    dialog_identity_observation: DialogIdentityObservation | None


@dataclass(frozen=True, slots=True)
class _IdentityBaseline:
    revision: int | None


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


class _EditedMessageEvent(Protocol):
    chat_id: int | None
    message: _MessageLike


class _DeletedMessagesEvent(Protocol):
    chat_id: int | None
    deleted_ids: Sequence[int]


class _OutboxReadEvent(Protocol):
    chat_id: int | None
    is_private: bool | None
    max_id: int | None


def _inbox_read_update_identity(update: object) -> tuple[int, str] | None:
    if isinstance(update, UpdateReadHistoryInbox):
        return int(get_peer_id(update.peer)), "UpdateReadHistoryInbox"
    if isinstance(update, UpdateReadChannelInbox):
        return int(get_peer_id(PeerChannel(update.channel_id))), "UpdateReadChannelInbox"
    return None


def _read_state(conn: sqlite3.Connection, dialog_id: int) -> tuple[object | None, object | None]:
    cursor_row = cast(
        tuple[object | None] | None,
        conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    unread_row = cast(
        tuple[object | None] | None,
        conn.execute("SELECT unread_count FROM dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    return (cursor_row[0] if cursor_row else None, unread_row[0] if unread_row else None)


def _apply_observed_inbox_read(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    max_id: int,
    still_unread: int | None,
    operation_id: str,
    partial_reason: str | None,
) -> tuple[tuple[int, int], dict[str, object]]:
    with write_transaction(conn):
        before_cursor, before_unread = _read_state(conn, dialog_id)
        rowcounts = _apply_inbox_read_fact(
            conn,
            dialog_id,
            max_id=max_id,
            unread_count=still_unread,
            observed_at=delivered_observed_at(),
        )
        after_cursor, after_unread = _read_state(conn, dialog_id)
    changed = before_cursor != after_cursor or before_unread != after_unread
    return rowcounts, {
        "kind": "sync.inbox_read_finished",
        "dialog_id": dialog_id,
        "operation_id": operation_id,
        "outcome": "partial" if partial_reason else ("applied" if changed else "unchanged"),
        "reason_code": partial_reason,
        "payload": {
            "cursor_before": before_cursor,
            "cursor_after": after_cursor,
            "unread_before": before_unread,
            "unread_after": after_unread,
        },
    }


class _RawReactionUpdate(Protocol):
    peer: object | None
    msg_id: int | None
    reactions: object | None


class _ChannelChatUpdateLike(Protocol):
    channel_id: int


class _ChatUpdateLike(Protocol):
    chat_id: int


class _ParticipantUpdateLike(Protocol):
    @property
    def date(self) -> datetime | None: ...

    @property
    def actor_id(self) -> int: ...

    @property
    def user_id(self) -> int: ...

    @property
    def new_participant(self) -> object | None: ...


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


class _ForumTopicsPinnedUpdateLike(Protocol):
    peer: object | None
    order: object | None


def _is_valid_nonnegative_int(value: object, *, allow_none: bool = False) -> bool:
    """Validate Telegram integer facts without coercing malformed values."""
    return (allow_none and value is None) or (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _validated_topic_pin_order(value: object) -> tuple[int, ...] | None:
    if value is None or not isinstance(value, (list, tuple)):
        return None
    if any(not _is_valid_nonnegative_int(topic_id) for topic_id in value):
        return None
    return tuple(int(topic_id) for topic_id in value)


def _channel_chat_dialog_id(update: _ChannelChatUpdateLike | _ChatUpdateLike) -> int | None:
    if isinstance(update, UpdateChannel):
        return int(get_peer_id(PeerChannel(update.channel_id)))
    if isinstance(update, UpdateChat):
        return int(get_peer_id(PeerChat(update.chat_id)))
    return None


def _should_refresh_linked_chat_id(
    update: _ChannelChatUpdateLike | _ChatUpdateLike,
    row: tuple[str | None, int | None] | None,
) -> bool:
    return isinstance(update, UpdateChannel) and row is not None and row[0] == "channel" and row[1] is not None


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
    require_write_transaction(conn)
    cursor_rowcount = apply_read_cursor(conn, dialog_id, "inbox", max_id)
    unread_rowcount = apply_unread_facts(
        conn,
        dialog_id,
        unread_count=unread_count,
        observed_at=observed_at,
        create_missing=True,
    )
    apply_realtime_eligibility(
        conn,
        dialog_id,
        unread=_current_unread_eligibility(conn, dialog_id),
        observed_at=observed_at,
    )
    conn.execute(_UPDATE_LAST_EVENT_SQL, (observed_at, dialog_id))
    return cursor_rowcount, unread_rowcount


def _current_unread_eligibility(conn: sqlite3.Connection, dialog_id: int) -> int | None:
    row = cast(
        tuple[int | None, int | None, int | None] | None,
        conn.execute(
            "SELECT unread_count,unread_mentions_count,unread_mark FROM dialogs WHERE dialog_id=?", (dialog_id,)
        ).fetchone(),
    )
    if row is None:
        return None
    # Realtime inbox and mark updates do not carry an unread-mentions
    # observation. A stored zero may merely be the row default, so they can
    # establish a positive aggregate but cannot manufacture a false one.
    if any(value is not None and value > 0 for value in row[:2]) or row[2] == 1:
        return 1
    return None


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

    async def get_me(self) -> object: ...

    async def get_entity(self, _dialog_id: object) -> object: ...

    def __call__(self, _request: object) -> Coroutine[object, object, object]: ...


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


def _first_non_empty_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value != "":
            return value
    return None


def _newer_dialog_identity_fields(conn: sqlite3.Connection, dialog_id: int, order: int) -> frozenset[str]:
    rows = cast(
        list[tuple[str]],
        conn.execute(
            "SELECT field FROM dialog_fact_observations WHERE dialog_id=? AND observation_order>? "
            "AND field IN ('name','username')",
            (dialog_id, order),
        ).fetchall(),
    )
    return frozenset(field for (field,) in rows)


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

# All dialogs.pinned writers also synchronize the published-pin relation below;
# publication and active staging are the authoritative cross-source pin record.
_UPDATE_DIALOG_PINNED_SQL = "UPDATE dialogs SET pinned=?, snapshot_at=? WHERE dialog_id=?"

_UPDATE_DIALOG_NEEDS_REFRESH_SQL = "UPDATE dialogs SET needs_refresh=1, snapshot_at=? WHERE dialog_id=?"

_UPDATE_DIALOG_LAST_MESSAGE_AT_SQL = (
    "UPDATE dialogs SET last_message_at = MAX(COALESCE(last_message_at, 0), ?),     snapshot_at = ? WHERE dialog_id = ?"
)

_UPSERT_REALTIME_DM_DIALOG_SQL = """
INSERT INTO dialogs (
    dialog_id, last_message_at, snapshot_at, hidden, needs_refresh,
    archived, pinned, unread_mentions_count, unread_reactions_count
) VALUES (?, ?, ?, 0, 1, 0, 0, 0, 0)
ON CONFLICT(dialog_id) DO UPDATE SET
    last_message_at = CASE
        WHEN excluded.last_message_at IS NULL THEN dialogs.last_message_at
        WHEN dialogs.last_message_at IS NULL THEN excluded.last_message_at
        ELSE MAX(dialogs.last_message_at, excluded.last_message_at)
    END,
    snapshot_at = CASE
        WHEN dialogs.snapshot_at IS NULL THEN excluded.snapshot_at
        ELSE MAX(dialogs.snapshot_at, excluded.snapshot_at)
    END,
    needs_refresh = 1
"""

# Empty-list fast path (NOT IN () is invalid SQLite — see review):
_CLEAR_ALL_PINS_SQL = "UPDATE dialogs SET pinned=0, snapshot_at=? WHERE pinned=1"

# ---------------------------------------------------------------------------
# EventHandlerManager
# ---------------------------------------------------------------------------


def _identity_path_excluded(path: str, roots: set[str]) -> bool:
    return any(path == root or path.startswith((root + ".", root + "[")) for root in roots)


def _mask_profile_identity_fields(push: ProfilePush, excluded: set[str]) -> ProfilePush:
    semantic_roots = {"user." + field for field in excluded if field in {"first_name", "last_name", "username"}}
    if "extra_usernames" in excluded:
        semantic_roots.add("user.usernames")
    return replace(
        push,
        fields={field: value for field, value in push.fields.items() if field not in excluded},
        semantic_fields={path: value for path, value in push.semantic_fields.items() if path not in semantic_roots},
        semantic_coverage={
            path: value
            for path, value in push.semantic_coverage.items()
            if not _identity_path_excluded(path, semantic_roots)
        },
    )


class EventHandlerManager:
    """Registers and dispatches real-time Telethon events to sync.db.

    Args:
        client: Telethon TelegramClient (daemon owns the connection).
        conn: Open SQLite writer connection to sync.db.
        shutdown_event: asyncio.Event set when SIGTERM is received.
    """

    def __init__(  # noqa: PLR0913 - explicit daemon-owned writer and profile policy
        self,
        client: _EventHandlerClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        update_barrier: UpdateProcessingBarrier | None = None,
        *,
        writer: SQLiteWriter,
        profile_detail_ttl_seconds: int,
        profile_pair_mode: str = "enabled",
        scheduled_mutation_fence: ScheduledMutationFence | None = None,
    ) -> None:
        self._client = client
        self._conn = conn
        self._writer = writer
        self._shutdown_event = shutdown_event
        self._update_barrier = update_barrier
        self._shutdown_event.is_set()
        self._synced_dialog_ids: set[int] = set()
        self._realtime_history_status: _RealtimeHistoryStatusReader = _SQLiteRealtimeHistoryStatusReader(conn)
        self._self_id: int | None = None
        self._demand_sink: DemandOfferSink | None = None
        self._observation_sink: RuntimeObservationSink | None = None
        self._profile_pair_mode = profile_pair_mode
        self._profile_detail_ttl_seconds = profile_detail_ttl_seconds
        self._profile_push_owner = object()
        self._scheduled_mutation_fence = scheduled_mutation_fence

    def bind_observation_sink(self, sink: RuntimeObservationSink) -> None:
        self._observation_sink = sink

    def _record_observation(self, **event: object) -> None:
        try:
            if self._observation_sink is not None:
                self._observation_sink.record(**event)  # type: ignore[arg-type]
        except Exception:
            logger.exception("runtime_event_record_failed kind=%s", event.get("kind"))

    def bind_demand_sink(self, sink: DemandOfferSink) -> None:
        """Attach post-commit durable wakeups after daemon composition."""
        self._demand_sink = sink

    async def _wait_for_update_barrier(self) -> None:
        if self._update_barrier is not None:
            await self._update_barrier.wait(self._shutdown_event)

    def _require_demand_sink(self) -> DemandOfferSink:
        sink = self._demand_sink
        if sink is None:
            raise RuntimeError("durable demand sink is not bound")
        return sink

    def _offer(self, *kinds: DemandKind) -> None:
        offer_durable_demand(self._require_demand_sink(), *kinds)

    def _offer_message_ingestion(self) -> None:
        self._offer(
            DemandKind.LIVE_HYDRATION_BATCH,
            DemandKind.BACKFILL_HYDRATION_BATCH,
            DemandKind.MESSAGE_FACT_REFRESH,
            DemandKind.READ_RECEIPT_BATCH,
        )

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
        self._client.add_event_handler(self.on_raw_identity_or_notify, events.Raw())
        self._client.add_event_handler(self.on_new_message, events.NewMessage)
        self._client.add_event_handler(
            self.on_raw_topic_message,
            events.Raw(types=[UpdateNewMessage, UpdateNewChannelMessage]),
        )
        self._client.add_event_handler(self.on_message_edited, events.MessageEdited)
        self._client.add_event_handler(self.on_message_deleted, events.MessageDeleted)
        # Phase 39.3-02: outbox read handler (peer→me side).
        # Dispatch path LOCKED to Path A: events.MessageRead(inbox=False).
        # Verified against .venv/lib/python3.15/site-packages/telethon/events/
        # messageread.py:37-48 — build() returns cls.Event(update.peer,
        # update.max_id, True) when the update is UpdateReadHistoryOutbox
        # (line 41-42); filter at lines 57-61 requires event.outbox == True
        # when inbox=False. Maximises symmetry with the Phase 38 inbox handler.
        self._client.add_event_handler(self.on_outbox_read, events.MessageRead(inbox=False))
        # Phase 39.2-01: 5th handler for raw reaction updates. Telethon emits
        # UpdateMessageReactions for User/Chat/Channel peers (single Update type;
        # peer field discriminates). Verified against
        # .venv/lib/python3.15/site-packages/telethon/events/raw.py — single-arg
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
            self.on_raw_participant,
            events.Raw(types=[UpdateChannelParticipant, UpdateChatParticipant]),
        )
        self._client.add_event_handler(
            self.on_raw_inbox_read,
            events.Raw(types=[UpdateReadHistoryInbox, UpdateReadChannelInbox]),
        )
        # Phase 42 EVENTS-05: forum topic pin state.
        self._client.add_event_handler(
            self.on_raw_forum_topic_pinned,
            events.Raw(types=[UpdatePinnedForumTopic, UpdatePinnedForumTopics]),
        )

    def unregister(self) -> None:
        """Remove all handlers from the client (graceful shutdown)."""
        if self._update_barrier is not None:
            self._update_barrier.cancel()
        self._client.remove_event_handler(self.on_new_message)
        self._client.remove_event_handler(self.on_raw_topic_message)
        self._client.remove_event_handler(self.on_message_edited)
        self._client.remove_event_handler(self.on_message_deleted)
        self._client.remove_event_handler(self.on_outbox_read)
        self._client.remove_event_handler(self.on_raw_reaction_update)
        self._client.remove_event_handler(self.on_raw_transcribed_audio)
        self._client.remove_event_handler(self.on_raw_new_scheduled_message)
        self._client.remove_event_handler(self.on_raw_delete_scheduled_messages)
        self._client.remove_event_handler(self.on_raw_dialog_pinned)
        self._client.remove_event_handler(self.on_raw_channel_chat_update)
        self._client.remove_event_handler(self.on_raw_identity_or_notify)
        self._client.remove_event_handler(self.on_raw_participant)
        self._client.remove_event_handler(self.on_raw_inbox_read)
        self._client.remove_event_handler(self.on_raw_forum_topic_pinned)

    def refresh_synced_dialogs(self) -> None:
        """Refresh the in-memory synced-dialog set from the DB.

        Called from the daemon heartbeat loop so newly enrolled dialogs
        are picked up within one heartbeat interval without re-registering
        handlers.
        """
        self._refresh_synced_dialogs()

    def set_self_id(self, self_id: int) -> None:
        self._self_id = self_id

    async def _resolve_self_id(self) -> int | None:
        if self._self_id is not None:
            return self._self_id
        try:
            me = await self._client.get_me()
            value = getattr(me, "id", None)
            if isinstance(value, int) and not isinstance(value, bool):
                self._self_id = value
        except RpcAdmissionClosedError:
            raise
        except Exception:
            fail_delivered_update()
            logger.exception("participant_update_self_id_unavailable")
        return self._self_id

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

    @staticmethod
    def _dm_identity(
        sender: _SenderLike | None,
        *,
        dialog_id: int,
        observed_at: int,
    ) -> _DmIdentity | None:
        if sender is None:
            return None
        first_raw = getattr(sender, "first_name", USERNAME_UNOBSERVED)
        last_raw = getattr(sender, "last_name", USERNAME_UNOBSERVED)
        first = _first_non_empty_str(first_raw) or ""
        last = _first_non_empty_str(last_raw) or ""
        raw_username = getattr(sender, "username", USERNAME_UNOBSERVED)
        raw_usernames = getattr(sender, "usernames", USERNAME_UNOBSERVED)
        observed_username = observe_username(raw_username, raw_usernames)
        return _DmIdentity(
            name=f"{first} {last}".strip() or None,
            username=None if observed_username is USERNAME_UNOBSERVED else cast(str | None, observed_username),
            entity_type=classify_dialog_type(sender).value,
            name_observed=first_raw is not USERNAME_UNOBSERVED or last_raw is not USERNAME_UNOBSERVED,
            username_observed=observed_username is not USERNAME_UNOBSERVED,
            dialog_identity_observation=observe_dialog_identity(
                sender,
                dialog_id=dialog_id,
                source="realtime",
                observed_at=observed_at,
            ),
        )

    def _persist_dm_enrollment(  # noqa: PLR0913 - explicit worker connection
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        identity: _DmIdentity | None,
        *,
        message_timestamp: int | None,
        observed_at: int,
        identity_baseline: _IdentityBaseline | None = None,
    ) -> EnrollmentOutcome:
        with write_transaction(conn):
            baseline_revision = (
                identity_baseline.revision
                if identity_baseline is not None
                else capture_identity_baseline(conn, dialog_id)
            )
            outcome = ensure_automatic_dm_enrollment(conn, dialog_id, now=observed_at)
            if not outcome.enabled:
                return outcome
            row_existed_before_presence = (
                conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() is not None
            )
            conn.execute(
                _UPSERT_REALTIME_DM_DIALOG_SQL,
                (
                    dialog_id,
                    message_timestamp,
                    observed_at,
                ),
            )
            if identity is not None:
                self._publish_dm_identity(
                    conn,
                    identity,
                    dialog_id,
                    baseline_revision,
                    observed_at,
                    row_existed_before_presence=row_existed_before_presence,
                )
            unhide_after_realtime_presence(conn, dialog_id)
            return outcome

    def _publish_dm_identity(  # noqa: PLR0913 - explicit worker connection
        self,
        conn: sqlite3.Connection,
        identity: _DmIdentity,
        dialog_id: int,
        baseline_revision: int | None,
        observed_at: int,
        *,
        row_existed_before_presence: bool,
    ) -> None:
        require_write_transaction(conn)
        observation = identity.dialog_identity_observation
        if observation is None:
            return
        expected_revision = (
            baseline_revision if baseline_revision is not None else (0 if not row_existed_before_presence else None)
        )
        if expected_revision is None or capture_identity_baseline(conn, dialog_id) != expected_revision:
            return
        order = delivered_observation_order(conn)
        name_accepted = observation.name is IDENTITY_OMITTED or claim_dialog_fact(conn, dialog_id, "name", order)
        username_accepted = observation.username is IDENTITY_OMITTED or claim_dialog_fact(
            conn, dialog_id, "username", order
        )
        observation = replace(
            observation,
            name=observation.name if name_accepted else IDENTITY_OMITTED,
            username=observation.username if username_accepted else IDENTITY_OMITTED,
            complete=all((observation.complete, name_accepted, username_accepted)),
        )
        publish_dialog_identity(
            conn,
            dialog_id,
            observation,
            expected_revision,
        )

    def _persist_dm_entity(
        self, conn: sqlite3.Connection, dialog_id: int, identity: _DmIdentity, observed_at: int
    ) -> None:
        try:
            with write_transaction(conn):
                retained = current_delivered_update()
                newer_fields = (
                    frozenset()
                    if retained is None
                    else _newer_dialog_identity_fields(conn, dialog_id, retained.observation_order)
                )
                apply_partial_entity_identity(
                    conn,
                    dialog_id,
                    PartialEntityIdentity(
                        observed_at=observed_at,
                        observed_type=identity.entity_type,
                        name_observed=identity.name_observed and "name" not in newer_fields,
                        name=identity.name,
                        username_observed=identity.username_observed and "username" not in newer_fields,
                        username=identity.username,
                    ),
                )
        except Exception:
            # Entity enrichment is optional after the mandatory dialog snapshot
            # has committed; the next refresh can fill a failed cache write.
            logger.exception("dm_auto_enroll_entity_failed dialog_id=%d", dialog_id)

    async def _auto_enroll_dm(
        self,
        dialog_id: int,
        sender: _SenderLike | None = None,
        *,
        message_date: datetime | None = None,
        observed_at: int | None = None,
        identity_baseline: _IdentityBaseline | None = None,
    ) -> bool:
        """Enroll a new DM dialog into synced_dialogs on first incoming message.

        Called from on_new_message when a private message arrives from a dialog
        not yet in synced_dialogs.  Uses INSERT OR IGNORE so concurrent calls
        and daemon restarts are idempotent.  After enrollment, the dialog is
        added to the in-memory set so subsequent messages are written real-time;
        FullSyncWorker picks up full history in its next batch cycle.

        If sender is provided (types.User), writes an entity and dialog snapshot
        so the resolver can find this contact immediately.  Identity lookup is
        best-effort: a missing peer still receives a visible thin dialog row.
        """
        now = delivered_observed_at() if observed_at is None else observed_at
        message_timestamp = int(message_date.timestamp()) if message_date is not None else None
        identity = self._dm_identity(sender, dialog_id=dialog_id, observed_at=now)

        try:
            outcome = await self._writer.run(
                lambda conn: self._persist_dm_enrollment(
                    conn,
                    dialog_id,
                    identity,
                    message_timestamp=message_timestamp,
                    observed_at=now,
                    identity_baseline=identity_baseline,
                )
            )
            if outcome.enabled and identity is not None:
                await self._writer.run(lambda conn: self._persist_dm_entity(conn, dialog_id, identity, now))
                await self._persist_optional_dm_profile(sender, dialog_id)
            if outcome.enabled and outcome.action in {"queue_full_history", "preserved_enabled_intent"}:
                self._offer(DemandKind.FULL_SYNC_PAGE)
                # status='syncing' is stable once inserted — FullSyncWorker only
                # advances it to 'synced', never back to 'not_synced'. Adding to
                # _synced_dialog_ids here is safe: the real-time handler path only
                # writes to messages and synced_dialogs.last_event_at, neither of
                # which depends on the status column. FullSyncWorker will backfill
                # full history via INSERT OR REPLACE in its next batch cycle.
                self._synced_dialog_ids.add(dialog_id)
                logger.info("dm_auto_enroll dialog_id=%d", dialog_id)
        except Exception:
            fail_delivered_update()
            logger.exception("dm_auto_enroll_failed dialog_id=%d", dialog_id)
            return False
        if identity is not None:
            logger.info("dm_auto_enroll_entity dialog_id=%d name=%r", dialog_id, identity.name)
        return True

    async def _persist_optional_dm_profile(self, sender: object, dialog_id: int) -> None:
        if self._self_id is None or project_user_profile(sender) is None:
            return
        try:
            await self._persist_profile_pushes(sender=sender)
        except Exception:
            logger.exception("dm_auto_enroll_profile_failed dialog_id=%d", dialog_id)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _verify_scheduled_publication_if_needed(
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        message: _MessageLike,
        *,
        now: int | None = None,
    ) -> None:
        if not bool(getattr(message, "from_scheduled", False)):
            return
        with write_savepoint(conn):
            if now is None:
                verify_scheduled_publication(
                    conn,
                    dialog_id,
                    int(message.id),
                    mutation_fence=self._scheduled_mutation_fence,
                    account_id=self._self_id,
                    published_at=int(message.date.timestamp()) if isinstance(message.date, datetime) else None,
                )
            else:
                verify_scheduled_publication(
                    conn,
                    dialog_id,
                    int(message.id),
                    now=now,
                    mutation_fence=self._scheduled_mutation_fence,
                    account_id=self._self_id,
                    published_at=int(message.date.timestamp()) if isinstance(message.date, datetime) else None,
                )

    async def _prepare_new_message_profile(
        self, event: _NewMessageEvent, dialog_id: int, observed_at: int
    ) -> tuple[int, _IdentityBaseline]:
        """Admit supplied private-peer evidence before message-history policy."""

        def reserve_observation(conn: sqlite3.Connection) -> tuple[int, _IdentityBaseline, bool]:
            with write_savepoint(conn):
                admitted = bool(
                    self._self_id is not None
                    and event.is_private
                    and admit_profile_peer(
                        conn,
                        self._self_id,
                        dialog_id,
                        now=observed_at,
                        pair_mode=self._profile_pair_mode,
                    )
                )
                return (
                    delivered_observation_order(conn),
                    _IdentityBaseline(capture_identity_baseline(conn, dialog_id)),
                    admitted,
                )

        observation_order, identity_baseline, admitted = await self._writer.run(reserve_observation)
        if admitted:
            self._offer(DemandKind.ENTITY_PROFILE_REFRESH)
        if event.is_private and self._self_id is not None:
            sender = getattr(event, "sender", None) or getattr(event.message, "sender", None)
            await self._persist_optional_dm_profile(sender, dialog_id)
        return observation_order, identity_baseline

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    @_acquisition(AcquisitionKind.ENTITY_LOOKUP)
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
        try:
            reaction_observed_at = delivered_observed_at()
            msg = event.message

            observation_order, identity_baseline = await self._prepare_new_message_profile(
                event, dialog_id, reaction_observed_at
            )
            coverage = await self._new_message_coverage(dialog_id, event, identity_baseline=identity_baseline)
            if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
                return
            if not allows_new_message(coverage, outgoing=bool(getattr(msg, "out", False))):
                await self._project_denied_new_message_metadata(
                    dialog_id, msg, coverage, observation_order=observation_order
                )
                return
            entity_name_map = await self._forward_entity_name_map(msg)
            extracted = replace(
                extract_message_row(dialog_id, msg, entity_name_map=entity_name_map),
                observation_order=observation_order,
            )
            # Forward enrichment suspends the handler; status may have changed
            # while Telegram was queried, so gate the mutation again.
            coverage = self._realtime_coverage(dialog_id)
            if not allows_new_message(coverage, outgoing=bool(getattr(msg, "out", False))):
                await self._project_denied_new_message_metadata(
                    dialog_id, msg, coverage, observation_order=observation_order
                )
                return
            now = delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> tuple[bool, bool]:
                with write_transaction(conn):
                    if not allows_new_message(
                        realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(dialog_id)),
                        outgoing=bool(getattr(msg, "out", False)),
                    ):
                        return False, False
                    insert_messages_with_fts(
                        conn,
                        [extracted],
                        priority=HydrationPriority.FOREGROUND,
                        reaction_source=ReactionAggregateSource.REALTIME_MESSAGE,
                        reaction_observed_at=reaction_observed_at,
                    )
                    # A normal message carrying from_scheduled is the verification
                    # point for the untrusted sent_messages hint retained by the
                    # scheduled-queue delete update.
                    self._verify_scheduled_publication_if_needed(conn, dialog_id, msg, now=now)
                    # Phase 42 EVENTS-04: advance dialogs.last_message_at monotonically.
                    # MAX(COALESCE(..., 0), new_ts) ensures no regression on out-of-order
                    # events. UPDATE matches 0 rows when the dialog is not yet bootstrapped
                    # (no dialogs row) — silent no-op; bootstrap is the sole row creator.
                    self._project_new_message_metadata(conn, dialog_id, msg, now, observation_order=observation_order)
                    self._record_body_event(conn, dialog_id, now)
                    needs_scheduled_work = scheduled_dialog_has_due_work(conn, dialog_id)
                return True, needs_scheduled_work

            accepted, needs_scheduled_work = await self._writer.run(_persist_event_0)
            if not accepted:
                return

            self._offer_message_ingestion()
            if bool(getattr(msg, "from_scheduled", False)) and needs_scheduled_work:
                self._offer(DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY)
            logger.debug("event_new dialog_id=%d message_id=%d", dialog_id, msg.id)
        except RpcAdmissionClosedError:
            raise
        except Exception as exc:
            fail_delivered_update()
            logger.exception(
                "event_new_failed dialog_id=%s sqlite_errorcode=%s sqlite_errorname=%s",
                dialog_id,
                getattr(exc, "sqlite_errorcode", None),
                getattr(exc, "sqlite_errorname", None),
            )

    async def _new_message_coverage(
        self,
        dialog_id: int,
        event: _NewMessageEvent,
        *,
        identity_baseline: _IdentityBaseline | None = None,
    ) -> RealtimeHistoryCoverage:
        coverage = self._realtime_coverage(dialog_id)
        if coverage is not RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
            return coverage

        def verify_publication(conn: sqlite3.Connection) -> bool:
            self._verify_scheduled_publication_if_needed(conn, dialog_id, event.message, now=delivered_observed_at())
            return scheduled_dialog_has_due_work(conn, dialog_id)

        needs_scheduled_work = await self._writer.run(verify_publication)
        if bool(getattr(event.message, "from_scheduled", False)) and needs_scheduled_work:
            self._offer(DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY)
        status, _ = self._realtime_history_status.read_status(dialog_id)
        if event.is_private and status is None:
            await self._enroll_new_private_message(dialog_id, event, identity_baseline)
            coverage = self._realtime_coverage(dialog_id)
        return coverage

    async def _enroll_new_private_message(
        self,
        dialog_id: int,
        event: _NewMessageEvent,
        identity_baseline: _IdentityBaseline | None,
    ) -> None:
        peer_field = "chat" if bool(getattr(event.message, "out", False)) else "sender"
        peer = cast(
            _SenderLike | None,
            getattr(event, peer_field, None) or getattr(event.message, peer_field, None),
        )
        enrolled = await self._auto_enroll_dm(
            dialog_id,
            sender=peer,
            message_date=event.message.date,
            observed_at=delivered_observed_at(),
            identity_baseline=identity_baseline,
        )
        if not enrolled:
            fail_delivered_update()

    async def _forward_entity_name_map(self, message: object) -> dict[int, str]:
        """Use delivered/local forward names and queue missing profile evidence."""
        fwd = cast(object | None, getattr(message, "fwd_from", None))
        from_id = cast(object | None, getattr(fwd, "from_id", None)) if fwd is not None else None
        if fwd is None or from_id is None or getattr(fwd, "from_name", None) is not None:
            return {}
        try:
            peer_id = get_peer_id(cast(TypePeer, from_id))
        except TypeError, ValueError:
            return {}

        def read_local_name(conn: sqlite3.Connection) -> tuple[str | None, bool]:
            with write_transaction(conn):
                row = cast(
                    tuple[object, ...] | None,
                    conn.execute("SELECT name FROM entities WHERE id=?", (peer_id,)).fetchone(),
                )
                name = cast(str | None, row[0]) if row is not None else None
                if name or self._self_id is None or peer_id == self._self_id:
                    return name, False
                refresh = cast(
                    tuple[object, ...] | None,
                    conn.execute(
                        "SELECT status FROM entity_profile_refresh_state WHERE entity_id=?", (peer_id,)
                    ).fetchone(),
                )
                if refresh is not None and str(refresh[0]) in {"complete", "rejected"}:
                    return None, False
                queued = EntityProfileRepository(conn, section_ttl_seconds=self._profile_detail_ttl_seconds)
                return name, queued.request_section_refresh(
                    peer_id,
                    ("full_profile",),
                    now=delivered_observed_at(),
                    pair_mode=self._profile_pair_mode,
                )

        name, queued = await self._writer.run(read_local_name)
        if queued:
            self._offer(DemandKind.ENTITY_PROFILE_REFRESH)
        return {peer_id: name} if name else {}

    async def _project_denied_new_message_metadata(
        self,
        dialog_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
        *,
        observation_order: int | None = None,
    ) -> None:
        if not self._metadata_realtime_allowed(coverage):
            return
        now = delivered_observed_at()

        def _persist_event_0(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                self._project_new_message_metadata(conn, dialog_id, msg, now, observation_order=observation_order)

        await self._writer.run(_persist_event_0)

    def _update_last_message_timestamp(
        self, conn: sqlite3.Connection, dialog_id: int, now: int, msg_date: datetime | None
    ) -> None:
        require_write_transaction(conn)
        if msg_date is not None:
            conn.execute(
                _UPDATE_DIALOG_LAST_MESSAGE_AT_SQL,
                (int(msg_date.timestamp()), now, dialog_id),
            )

    def _record_body_event(self, conn: sqlite3.Connection, dialog_id: int, now: int) -> None:
        require_write_transaction(conn)
        conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

    def _project_new_message_metadata(
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        msg: _MessageLike,
        now: int,
        *,
        observation_order: int | None = None,
    ) -> None:
        """Project dialog/topic metadata independently of message-body scope."""
        self._update_last_message_timestamp(conn, dialog_id, now, msg.date)
        self._handle_topic_message_action(conn, dialog_id, msg, now, observation_order=observation_order)

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_raw_topic_message(self, update: object) -> None:
        """Project topic service messages omitted by Telethon's NewMessage builder."""
        try:
            if not isinstance(update, (UpdateNewMessage, UpdateNewChannelMessage)):
                return
            msg = update.message
            if not isinstance(msg, MessageService):
                return
            action = getattr(msg, "action", None)
            if not isinstance(action, (MessageActionTopicCreate, MessageActionTopicEdit)):
                return
            peer = getattr(msg, "peer_id", None)
            if peer is None:
                return
            dialog_id = int(get_peer_id(cast(TypePeer, peer)))
            if not self._metadata_realtime_allowed(self._realtime_coverage(dialog_id)):
                return
            now = delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    observation_order = delivered_observation_order(conn)
                    if not self._metadata_realtime_allowed(
                        realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(dialog_id))
                    ):
                        return
                    self._handle_topic_message_action(
                        conn, dialog_id, cast(_MessageLike, msg), now, observation_order=observation_order
                    )

            await self._writer.run(_persist_event_0)
        except Exception:
            fail_delivered_update()
            logger.exception("event_raw_topic_message_failed update=%r", type(update).__name__)

    def _handle_topic_message_action(
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        msg: _MessageLike,
        now: int,
        *,
        observation_order: int | None = None,
    ) -> None:
        require_write_transaction(conn)
        action = cast(object | None, getattr(msg, "action", None))
        if isinstance(action, (MessageActionTopicCreate, MessageActionTopicEdit)):
            TopicCatalogRepository(conn).request(dialog_id, "topic_changed")
        if isinstance(action, MessageActionTopicCreate):
            self._handle_topic_create_action(conn, dialog_id, msg, now, action, observation_order=observation_order)
            return
        if isinstance(action, MessageActionTopicEdit):
            self._handle_topic_edit_action(conn, dialog_id, msg, now, action, observation_order=observation_order)

    def _handle_topic_create_action(  # noqa: PLR0913 - explicit worker connection
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        msg: _MessageLike,
        now: int,
        action: MessageActionTopicCreate,
        *,
        observation_order: int | None = None,
    ) -> None:
        topic_id = int(msg.id)
        if topic_id <= 0:
            return
        msg_date = msg.date
        topic_timestamp = int(msg_date.timestamp()) if msg_date is not None else now
        SQLiteTopicMetadataRepository(conn).apply_topic_create(
            dialog_id,
            topic_id,
            title=action.title or "Topic",
            icon_emoji_id=action.icon_emoji_id,
            date=topic_timestamp,
            observed_at=now,
            observation_order=observation_order,
        )
        logger.info(
            "event_topic_create dialog_id=%d topic_id=%d",
            dialog_id,
            topic_id,
        )

    def _handle_topic_edit_action(  # noqa: PLR0913 - explicit worker connection
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        msg: _MessageLike,
        now: int,
        action: MessageActionTopicEdit,
        *,
        observation_order: int | None = None,
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

        topic_id_raw = getattr(reply_to, "reply_to_top_id", None)
        if not _is_valid_nonnegative_int(topic_id_raw, allow_none=True):
            topic_id_raw = getattr(reply_to, "reply_to_msg_id", None)
        if not _is_valid_nonnegative_int(topic_id_raw):
            logger.debug(
                "event_topic_edit_skipped reason=no_reply_target dialog_id=%d",
                dialog_id,
            )
            return

        topic_id = int(cast(int, topic_id_raw))
        SQLiteTopicMetadataRepository(conn).apply_topic_edit(
            dialog_id,
            topic_id,
            title=action.title,
            icon_emoji_id=action.icon_emoji_id,
            hidden=action.hidden,
            observed_at=now,
            observation_order=observation_order,
        )
        if action.hidden:
            logger.info("event_topic_hidden dialog_id=%d topic_id=%d", dialog_id, topic_id)
            return
        logger.info(
            "event_topic_edit dialog_id=%d topic_id=%d",
            dialog_id,
            topic_id,
        )

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    @_acquisition(AcquisitionKind.ENTITY_LOOKUP)
    async def on_message_edited(self, event: _EditedMessageEvent) -> None:
        """Handle a MessageEdited event: version old text, update messages row.

        Three cases:
        1. Message not in sync.db yet: INSERT it with current text, no version history.
        2. Text unchanged: update composition facts and reaction deltas without text history.
        3. Text changed: insert old_text into message_versions, update messages.text.

        All operations in a single transaction.
        """
        dialog_id = event.chat_id
        if dialog_id is None:
            return

        try:
            observation_order = await self._writer.run(delivered_observation_order)
            msg = event.message
            message_id = int(msg.id)
            now = delivered_observed_at()
            coverage = self._realtime_coverage(dialog_id)
            if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
                return

            existing, existing_out = await self._writer.run(
                lambda conn: (
                    read_message_text(conn, dialog_id, message_id),
                    read_message_out(conn, dialog_id, message_id),
                )
            )

            if not existing.found:
                if await self._insert_missing_edited_message(dialog_id, msg, coverage, now, observation_order):
                    logger.info(
                        "event_edit_new dialog_id=%d message_id=%d (not in sync.db, inserted)",
                        dialog_id,
                        message_id,
                    )
                return

            old_text = existing.text
            next_ver = await self._persist_changed_edit(
                dialog_id,
                msg,
                coverage,
                existing_out.outgoing,
                old_text,
                now,
                observation_order,
            )
            if next_ver is None:
                return

            logger.debug(
                "event_edit dialog_id=%d message_id=%d version=%d",
                dialog_id,
                message_id,
                next_ver,
            )
        except RpcAdmissionClosedError:
            raise
        except Exception:
            fail_delivered_update()
            logger.exception("event_edit_failed dialog_id=%s", dialog_id)

    async def _insert_missing_edited_message(
        self,
        dialog_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
        now: int,
        observation_order: int,
    ) -> bool:
        outgoing = bool(getattr(msg, "out", False))
        if not allows_missing_body_insert(coverage, RealtimeBodyEvent.EDIT, outgoing=outgoing):
            return False
        entity_name_map = await self._forward_entity_name_map(msg)
        extracted = replace(
            extract_message_row(dialog_id, msg, entity_name_map=entity_name_map), observation_order=observation_order
        )
        coverage = self._realtime_coverage(dialog_id)
        if not allows_missing_body_insert(coverage, RealtimeBodyEvent.EDIT, outgoing=outgoing):
            return False

        def _persist_event_0(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                if not allows_missing_body_insert(
                    realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(dialog_id)),
                    RealtimeBodyEvent.EDIT,
                    outgoing=outgoing,
                ):
                    return False
                insert_messages_with_fts(
                    conn,
                    [extracted],
                    priority=HydrationPriority.FOREGROUND,
                    reaction_source=ReactionAggregateSource.MESSAGE_EDIT,
                    reaction_observed_at=now,
                )
                self._record_body_event(conn, dialog_id, now)
            return True

        if not await self._writer.run(_persist_event_0):
            return False
        self._offer_message_ingestion()
        return True

    async def _persist_changed_edit(  # noqa: PLR0913, PLR0917
        self,
        dialog_id: int,
        msg: _MessageLike,
        coverage: RealtimeHistoryCoverage,
        outgoing: bool,
        old_text: str | None,
        now: int,
        observation_order: int,
    ) -> int | None:
        edit_date_raw = msg.edit_date
        edit_date_unix = int(edit_date_raw.timestamp()) if edit_date_raw is not None else now
        entity_name_map = await self._forward_entity_name_map(msg)
        extracted = replace(
            extract_message_row(dialog_id, msg, entity_name_map=entity_name_map), observation_order=observation_order
        )
        extracted = replace(extracted, reactions_observed=msg.reactions is not None)
        coverage = self._realtime_coverage(dialog_id)
        if not allows_existing_body_update(coverage, RealtimeBodyEvent.EDIT, outgoing=outgoing):
            return None
        if coverage is RealtimeHistoryCoverage.OWN_OUTGOING:
            # The incoming edit object may omit Telethon's ``out`` flag;
            # preserve the canonical outgoing classification of the row.
            extracted = replace(extracted, message=replace(extracted.message, out=1))

        def _persist_event_0(conn: sqlite3.Connection) -> int | None:
            with write_transaction(conn):
                outgoing = read_message_out(conn, dialog_id, int(msg.id)).outgoing
                current_text = read_message_text(conn, dialog_id, int(msg.id)).text
                if not allows_existing_body_update(
                    realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(dialog_id)),
                    RealtimeBodyEvent.EDIT,
                    outgoing=outgoing,
                ):
                    return None
                next_ver = persist_edited_message(
                    conn,
                    extracted,
                    old_text=current_text,
                    edit_date=edit_date_unix,
                    priority=HydrationPriority.FOREGROUND,
                )
                if current_text != extracted.message.text or msg.edit_date is not None or msg.reactions is not None:
                    self._record_body_event(conn, dialog_id, now)
            return next_ver

        next_ver = await self._writer.run(_persist_event_0)
        if next_ver is None:
            return None
        self._offer_message_ingestion()
        if msg.reactions is not None and old_text == extracted.message.text:
            logger.debug(
                "event_edit_reactions dialog_id=%d message_id=%d count=%d",
                dialog_id,
                int(msg.id),
                len(extracted.reactions),
            )
        return next_ver

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_message_deleted(self, event: _DeletedMessagesEvent) -> None:
        """Handle a MessageDeleted event and preserve the last known text.

        Peerless nonchannel IDs retain terminal evidence; unique cached peers
        receive ordinary deletion cleanup. Ambiguous IDs remain unattributed.
        Preserves the last known text column.
        Only updates rows where is_deleted=0 to avoid re-stamping deleted_at.
        """
        dialog_id = event.chat_id

        if dialog_id is None:
            try:
                await self._writer.run(lambda conn: self._handle_peerless_deletions(conn, event.deleted_ids))
            except Exception:
                fail_delivered_update()
                logger.exception("event_delete_failed dialog_id=%s", dialog_id)
            return

        try:
            now = delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    allowed_ids = record_message_deletions(conn, dialog_id, event.deleted_ids)
                    coverage = realtime_history_coverage(
                        *_SQLiteRealtimeHistoryStatusReader(conn).read_status(dialog_id)
                    )
                    if coverage is RealtimeHistoryCoverage.NO_REALTIME_HISTORY:
                        return
                    if coverage is RealtimeHistoryCoverage.OWN_OUTGOING:
                        allowed_ids = tuple(
                            msg_id for msg_id in allowed_ids if read_message_out(conn, dialog_id, int(msg_id)).outgoing
                        )
                    if not allowed_ids:
                        return

                    for msg_id in allowed_ids:
                        mark_message_deleted(conn, dialog_id, msg_id, now)
                    conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))

            await self._writer.run(_persist_event_0)

            logger.info("event_delete dialog_id=%d count=%d", dialog_id, len(event.deleted_ids))
        except Exception:
            fail_delivered_update()
            logger.exception("event_delete_failed dialog_id=%s", dialog_id)

    def _handle_peerless_deletions(self, conn: sqlite3.Connection, deleted_ids: Sequence[int]) -> None:
        now = delivered_observed_at()
        resolved: list[tuple[int, int]] = []
        with write_transaction(conn):
            accepted_ids = record_nonchannel_deletions(conn, deleted_ids, now)
            candidate_dialogs = find_unique_nonchannel_message_dialogs(conn, accepted_ids)
            for raw_message_id in accepted_ids:
                message_id = int(raw_message_id)
                dialog_id = candidate_dialogs.get(message_id)
                if dialog_id is not None and mark_message_deleted(conn, dialog_id, message_id, now):
                    resolved.append((dialog_id, message_id))
            for dialog_id in {item[0] for item in resolved}:
                conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
        logger.info(
            "event_delete_peerless resolved=%d unresolved=%d",
            len(resolved),
            len(deleted_ids) - len(resolved),
        )

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
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

            def _persist_event_0(conn: sqlite3.Connection) -> bool:
                with write_transaction(conn):
                    upsert_scheduled_message(
                        conn,
                        dialog_id,
                        message,
                        now=delivered_observed_at(),
                        mutation_fence=self._scheduled_mutation_fence,
                        account_id=self._self_id,
                    )
                    return scheduled_dialog_has_due_work(conn, dialog_id)

            if await self._writer.run(_persist_event_0):
                self._offer(DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY)
            message_id_attr = "id"
            logger.info(
                "scheduled_new dialog_id=%d message_id=%d",
                dialog_id,
                int(cast(int, getattr(message, message_id_attr))),
            )
        except Exception:
            fail_delivered_update()
            logger.exception("scheduled_new_failed dialog_id=%s", dialog_id)

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_raw_delete_scheduled_messages(self, update: object) -> None:
        """Retain cancellation/publication evidence from a queue-removal update."""
        dialog_id = scheduled_dialog_id(getattr(update, "peer", None))
        message_ids = cast(Sequence[int] | None, getattr(update, "messages", None))
        if dialog_id is None or not message_ids:
            return
        sent_message_ids = cast(Sequence[int] | None, getattr(update, "sent_messages", None))
        try:

            def _persist_event_0(conn: sqlite3.Connection) -> bool:
                with write_transaction(conn):
                    mark_scheduled_messages_removed(
                        conn,
                        dialog_id,
                        message_ids,
                        sent_message_ids,
                        now=delivered_observed_at(),
                        mutation_fence=self._scheduled_mutation_fence,
                        account_id=self._self_id,
                    )
                    return scheduled_dialog_has_due_work(conn, dialog_id)

            if await self._writer.run(_persist_event_0):
                self._offer(DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY)
            logger.info("scheduled_removed dialog_id=%d count=%d", dialog_id, len(message_ids))
        except Exception:
            fail_delivered_update()
            logger.exception("scheduled_removed_failed dialog_id=%s", dialog_id)

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_outbox_read(self, event: _OutboxReadEvent) -> None:
        """Handle MessageRead(inbox=False): update read_outbox_max_id monotonically.

        Path A dispatch (LOCKED). Verified against
        ``.venv/lib/python3.15/site-packages/telethon/events/messageread.py``
        lines 37-48: ``MessageRead.build()`` returns
        ``cls.Event(update.peer, update.max_id, True)`` when the update is an
        ``UpdateReadHistoryOutbox`` (lines 41-42); ``filter()`` at lines 57-61
        enforces ``event.outbox == True`` when ``inbox=False``. So this
        callback only ever fires on outbox reads — same shape as
          the raw inbox path, just the mirrored direction.

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
            now = delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> int:
                with write_transaction(conn):
                    rowcount = apply_read_cursor(conn, dialog_id, "outbox", max_id)
                    conn.execute(_UPDATE_LAST_EVENT_SQL, (now, dialog_id))
                return rowcount

            rowcount = await self._writer.run(_persist_event_0)
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
            fail_delivered_update()
            logger.error(
                "event_outbox_read_failed dialog_id=%s max_id=%s error=%r",
                dialog_id,
                max_id,
                exc,
            )

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_raw_reaction_update(self, update: _RawReactionUpdate) -> None:
        """Handle raw UpdateMessageReactions for synced dialogs.

        Telethon contract (verified against
        .venv/lib/python3.15/site-packages/telethon/events/raw.py:22-23 and
        .venv/.../telethon/tl/types/__init__.py UpdateMessageReactions):
            async def handler(update)  # single-arg
        ``update`` is the raw TL Update with attributes:
            .peer (PeerUser | PeerChat | PeerChannel), .msg_id, .reactions

        For synced dialogs only, apply ``update.reactions`` directly.  The raw
        update is already the aggregate observation, including an authoritative
        empty value; it never performs a message lookup.
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

        try:
            aggregates = project_reaction_aggregates(update.reactions)

            def persist_reaction(conn: sqlite3.Connection) -> tuple[bool, bool]:
                previous_generation = self._reaction_generation(conn, dialog_id, msg_id)
                accepted = self._apply_reaction_event(
                    conn, dialog_id, msg_id, aggregates, can_see_list=project_reaction_visibility(update.reactions)
                )
                return accepted, self._reaction_generation(conn, dialog_id, msg_id) != previous_generation

            accepted, invalidated = await self._writer.run(persist_reaction)
            if not accepted:
                self._record_observation(
                    kind="reaction.aggregate",
                    outcome="rejected",
                    result_count=len(aggregates),
                    payload={"source": ReactionAggregateSource.RAW_UPDATE.value},
                )
                return
            self._record_observation(
                kind="reaction.aggregate",
                outcome="accepted",
                result_count=len(aggregates),
                payload={"source": ReactionAggregateSource.RAW_UPDATE.value},
            )
            if invalidated:
                self._record_observation(
                    kind="reaction.invalidation",
                    outcome="detail_stale",
                    payload={"source": ReactionAggregateSource.RAW_UPDATE.value},
                )
            logger.debug(
                "event_raw_reaction dialog_id=%d message_id=%d count=%d",
                dialog_id,
                msg_id,
                len(aggregates),
            )
        except Exception:
            fail_delivered_update()
            logger.exception(
                "event_raw_reaction_apply_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )

    def _reaction_generation(self, conn: sqlite3.Connection, dialog_id: int, msg_id: int) -> int | None:
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT generation FROM message_reaction_aggregate_state WHERE dialog_id=? AND message_id=?",
                (dialog_id, msg_id),
            ).fetchone(),
        )
        return None if row is None else int(cast(int, row[0]))

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

    def _apply_reaction_event(  # noqa: PLR0913 - explicit worker connection
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        msg_id: int,
        aggregates: Sequence[ReactionAggregate],
        boundary: ReactionObservationBoundary | None = None,
        *,
        can_see_list: bool | None = None,
    ) -> bool:
        with write_transaction(conn):
            if not hydration_observation_current(conn, dialog_id, msg_id, None):
                return False
            coverage = realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(dialog_id))
            existing_out = read_message_out(conn, dialog_id, msg_id)
            if not allows_existing_body_update(coverage, RealtimeBodyEvent.REACTION, outgoing=existing_out.outgoing):
                return False
            if coverage is not RealtimeHistoryCoverage.FULL_HISTORY and not existing_out.found:
                return False
            now = delivered_observed_at()
            if boundary is None:
                delivered = current_delivered_update()
                boundary = (
                    allocate_observation_boundary(conn, ReactionAggregateSource.RAW_UPDATE)
                    if delivered is None
                    else ReactionObservationBoundary(
                        observed_at=int(delivered.captured_at),
                        source_rank=ReactionAggregateSource.RAW_UPDATE.rank,
                        sequence=delivered.reaction_sequence,
                        source=ReactionAggregateSource.RAW_UPDATE.value,
                    )
                )
            accepted = replace_reaction_aggregates(
                conn,
                dialog_id,
                msg_id,
                aggregates,
                source=ReactionAggregateSource.RAW_UPDATE,
                observed_at=None if boundary is None else boundary.observed_at,
                observation_sequence=None if boundary is None else boundary.sequence,
                can_see_list=can_see_list,
            )
            if not accepted:
                return False
            self._record_body_event(conn, dialog_id, now)
            return True

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
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
            now = delivered_observed_at()

            def persist_transcription(conn: sqlite3.Connection) -> None:
                row = read_message_text(conn, dialog_id, msg_id)
                if row.found:
                    self._update_existing_transcription(conn, event_fields, row.text, coverage, now)
                elif coverage is RealtimeHistoryCoverage.FULL_HISTORY:
                    self._stage_missing_transcription(conn, event_fields, now)

            await self._writer.run(persist_transcription)
            logger.debug(
                "event_raw_transcribed_audio dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )
        except Exception:
            fail_delivered_update()
            logger.exception(
                "event_raw_transcribed_audio_failed dialog_id=%d message_id=%d",
                dialog_id,
                msg_id,
            )

    def _stage_missing_transcription(
        self,
        conn: sqlite3.Connection,
        event: _TranscriptionEvent,
        now: int,
    ) -> None:
        """Store a FULL_HISTORY fact only while capture authorization remains."""
        with write_transaction(conn):
            if (
                realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(event.dialog_id))
                is not RealtimeHistoryCoverage.FULL_HISTORY
            ):
                return
            if read_message_text(conn, event.dialog_id, event.message_id).found:
                return
            if not stage_message_transcription(
                conn,
                event.dialog_id,
                event.message_id,
                transcribed_text=event.text,
                transcription_id=event.transcription_id,
                received_at=now,
            ):
                return
            conn.execute(_UPDATE_LAST_EVENT_SQL, (now, event.dialog_id))

    def _update_existing_transcription(
        self,
        conn: sqlite3.Connection,
        event: _TranscriptionEvent,
        old_text: str | None,
        coverage: RealtimeHistoryCoverage,
        now: int,
    ) -> None:
        """Upsert a final fact and project changed text under current policy."""
        with write_transaction(conn):
            existing_out = read_message_out(conn, event.dialog_id, event.message_id)
            if not allows_existing_body_update(
                coverage,
                RealtimeBodyEvent.TRANSCRIPTION,
                outgoing=existing_out.outgoing,
            ):
                return
            old_text = read_message_text(conn, event.dialog_id, event.message_id).text
            if not allows_existing_body_update(
                realtime_history_coverage(*_SQLiteRealtimeHistoryStatusReader(conn).read_status(event.dialog_id)),
                RealtimeBodyEvent.TRANSCRIPTION,
                outgoing=existing_out.outgoing,
            ):
                return
            if not apply_message_transcription(
                conn,
                event.dialog_id,
                event.message_id,
                transcribed_text=event.text,
                transcription_id=event.transcription_id,
                received_at=now,
            ):
                return
            if old_text != event.text:
                conn.execute(_UPDATE_LAST_EVENT_SQL, (now, event.dialog_id))

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

    def _collect_pinned_dialog_ids(self, order: Sequence[object]) -> list[int]:
        pinned_ids: list[int] = []
        seen: set[int] = set()
        for dialog_peer in order:
            inner_peer = dialog_peer.peer if isinstance(dialog_peer, _PeerContainer) else dialog_peer
            try:
                dialog_id = int(cast(int, get_peer_id(inner_peer)))
            except TypeError, ValueError:
                continue
            if dialog_id not in seen:
                pinned_ids.append(dialog_id)
                seen.add(dialog_id)
        return pinned_ids

    @staticmethod
    def _published_pin_folder(folder_id: object) -> int | None:
        if folder_id is None or folder_id == 0:
            return 0
        if folder_id == 1:
            return 1
        return None

    def _apply_published_pin_mutation(
        self, conn: sqlite3.Connection, folder_id: int | None, dialog_id: int, pinned: int
    ) -> None:
        """Apply one published pin change within the caller's transaction."""
        require_write_transaction(conn)
        if folder_id is None:
            return
        current = [
            int(cast(int, pin[0]))
            for pin in cast(
                list[tuple[object, object]],
                conn.execute(
                    "SELECT dialog_id,position FROM dialog_directory_published_pins "
                    "WHERE folder_id=? ORDER BY position,dialog_id",
                    (folder_id,),
                ).fetchall(),
            )
        ]
        current = ordered_pin_overlay(conn, folder_id, current)
        conn.execute("DELETE FROM dialog_directory_published_pins WHERE folder_id=?", (folder_id,))
        conn.executemany(
            "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) VALUES (?,?,?)",
            [(folder_id, current_id, position) for position, current_id in enumerate(current)],
        )
        apply_active_generation_pin_delta(conn, folder_id, dialog_id, bool(pinned))
        record_realtime_pin_fence(
            conn,
            folder_id,
            {
                "kind": "delta",
                "dialog_id": dialog_id,
                "pinned": bool(pinned),
                "observation_order": delivered_observation_order(conn),
            },
        )

    def _project_pinned_eligibility(
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        folder_id: object,
        *,
        known_dialog: bool,
        now: int,
    ) -> None:
        """Project archive membership for a known dialog within the caller's transaction."""
        if not known_dialog or not isinstance(folder_id, int) or isinstance(folder_id, bool) or folder_id not in {0, 1}:
            return
        apply_realtime_eligibility(
            conn,
            dialog_id,
            archived=int(folder_id == 1),
            observed_at=now,
        )

    def _update_dialog_pinned(self, conn: sqlite3.Connection, update: UpdateDialogPinned, now: int) -> None:
        dialog_id = self._dialog_id_from_peer(update.peer)
        if dialog_id is None:
            return
        folder_id = getattr(update, "folder_id", None)
        published_folder = self._published_pin_folder(folder_id)
        pinned = 1 if update.pinned else 0
        with write_transaction(conn):
            observation_order = delivered_observation_order(conn)
            pin_accepted = published_folder is None or claim_pin_delta(
                conn, published_folder, dialog_id, bool(pinned), observation_order
            )
            known_dialog = conn.execute("SELECT 1 FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() is not None
            # ``dialogs.pinned`` is the projection for the main list.  A pin
            # update scoped to another folder must not change that bit.
            if pin_accepted:
                if known_dialog and published_folder == 0:
                    conn.execute(_UPDATE_DIALOG_PINNED_SQL, (pinned, now, dialog_id))
                self._apply_published_pin_mutation(conn, published_folder, dialog_id, pinned)
            self._project_pinned_eligibility(conn, dialog_id, folder_id, known_dialog=known_dialog, now=now)
        logger.info("event_dialog_pinned dialog_id=%d pinned=%d", dialog_id, pinned)

    def _rewrite_pinned_dialogs(self, conn: sqlite3.Connection, update: UpdatePinnedDialogs, now: int) -> None:
        order = update.order
        if order is None:
            logger.debug("event_pinned_dialogs_order_none — skip")
            return
        # folder_id=None means the main list; folder_id=1 means Archived, etc.
        # A folder-scoped update carries only pins *within* that folder, so we
        # must not use it to clear pins in other folders.
        folder_id = update.folder_id
        published_folder = self._published_pin_folder(folder_id)
        is_main_folder = published_folder == 0
        pinned_ids = self._collect_pinned_dialog_ids(cast(Sequence[object], order))
        with write_transaction(conn):
            if published_folder is not None:
                merged = claim_pin_snapshot(conn, published_folder, delivered_observation_order(conn), pinned_ids)
                if merged is None:
                    return
                pinned_ids = merged
            if is_main_folder:
                conn.execute(_CLEAR_ALL_PINS_SQL, (now,))
                for dialog_id in pinned_ids:
                    conn.execute(_UPDATE_DIALOG_PINNED_SQL, (1, now, dialog_id))
            if published_folder is not None:
                conn.execute("DELETE FROM dialog_directory_published_pins WHERE folder_id=?", (published_folder,))
                conn.executemany(
                    "INSERT INTO dialog_directory_published_pins(folder_id,dialog_id,position) VALUES (?,?,?)",
                    [(published_folder, dialog_id, position) for position, dialog_id in enumerate(pinned_ids)],
                )
                sync_active_generation_pins_from_publication(conn, published_folder)
                record_realtime_pin_fence(
                    conn,
                    published_folder,
                    {
                        "kind": "rewrite",
                        "dialog_ids": pinned_ids,
                        "observation_order": delivered_observation_order(conn),
                    },
                )
            # For folder-scoped updates (folder_id != None) we only set the
            # pinned=1 rows above; we do not clear other dialogs because the
            # update does not describe pins outside that folder.
        logger.info(
            "event_pinned_dialogs_rewrote pinned_count=%d folder_id=%s",
            len(pinned_ids),
            folder_id,
        )

    async def _update_dialog_unread_mark(self, update: UpdateDialogUnreadMark, now: int) -> None:
        dialog_id = self._dialog_id_from_peer(update.peer)
        if dialog_id is None:
            return

        def _persist_event_0(conn: sqlite3.Connection) -> int:
            with write_transaction(conn):
                rowcount = apply_unread_facts(
                    conn,
                    dialog_id,
                    unread_mark=getattr(update, "unread", None),
                    observed_at=now,
                    mark_needs_refresh=True,
                    create_missing=True,
                )
                apply_realtime_eligibility(
                    conn,
                    dialog_id,
                    unread=_current_unread_eligibility(conn, dialog_id),
                    observed_at=now,
                )
            return rowcount

        rowcount = await self._writer.run(_persist_event_0)
        if rowcount:
            self._offer(DemandKind.DIALOG_LIGHT_RECONCILIATION)
        logger.info(
            "event_dialog_unread_mark dialog_id=%d unread=%s updated=%d",
            dialog_id,
            getattr(update, "unread", None),
            rowcount,
        )

    @staticmethod
    def _username_update_name(update: UpdateUserName) -> ObservedText:
        if not hasattr(update, "first_name") and not hasattr(update, "last_name"):
            return IDENTITY_OMITTED
        return " ".join(part for part in (update.first_name, update.last_name) if part) or None

    @staticmethod
    def _username_update_alias(update: UpdateUserName) -> ObservedText:
        if not hasattr(update, "usernames"):
            return IDENTITY_OMITTED
        observed = observe_username(USERNAME_UNOBSERVED, update.usernames)
        return IDENTITY_OMITTED if observed is USERNAME_UNOBSERVED else cast(str | None, observed)

    def _update_realtime_username(self, conn: sqlite3.Connection, update: UpdateUserName, now: int) -> None:
        with write_transaction(conn):
            name = self._username_update_name(update)
            username = self._username_update_alias(update)
            dialog_id = int(update.user_id)
            observation_order = delivered_observation_order(conn)
            if name is not IDENTITY_OMITTED and not claim_dialog_fact(conn, dialog_id, "name", observation_order):
                name = IDENTITY_OMITTED
            if username is not IDENTITY_OMITTED and not claim_dialog_fact(
                conn, dialog_id, "username", observation_order
            ):
                username = IDENTITY_OMITTED
            baseline_revision = capture_identity_baseline(conn, dialog_id)
            publish_dialog_identity(
                conn,
                dialog_id,
                DialogIdentityObservation(
                    dialog_id=dialog_id,
                    name=name,
                    username=username,
                    source="realtime",
                    observed_at=now,
                ),
                baseline_revision,
            )
            if name is not IDENTITY_OMITTED or username is not IDENTITY_OMITTED:
                apply_partial_entity_identity(
                    conn,
                    int(update.user_id),
                    PartialEntityIdentity(
                        observed_at=now,
                        name_observed=name is not IDENTITY_OMITTED,
                        name=cast(str | None, name) if name is not IDENTITY_OMITTED else None,
                        username_observed=username is not IDENTITY_OMITTED,
                        username=cast(str | None, username) if username is not IDENTITY_OMITTED else None,
                    ),
                )

    def _update_realtime_notify(self, conn: sqlite3.Connection, update: UpdateNotifySettings, now: int) -> None:
        peer = getattr(getattr(update, "peer", None), "peer", None)
        if not isinstance(peer, (PeerUser, PeerChat, PeerChannel)):
            return
        try:
            dialog_id = int(get_peer_id(peer))
        except TypeError, ValueError:
            return
        settings = cast(object | None, getattr(update, "notify_settings", None))
        if settings is None:
            return
        mute_until = getattr(settings, "mute_until", None)
        if isinstance(mute_until, datetime):
            mute_until = int(mute_until.timestamp())
        if mute_until is None:
            with write_transaction(conn):
                if clear_realtime_mute(conn, dialog_id, observed_at=now):
                    SQLiteFolderSnapshotRepository(conn).reproject_current_rules_in_transaction(now=now)
            return
        if not _is_valid_nonnegative_int(mute_until):
            return
        with write_transaction(conn):
            apply_realtime_eligibility(conn, dialog_id, mute_until=mute_until, observed_at=now)
            SQLiteFolderSnapshotRepository(conn).reproject_current_rules_in_transaction(now=now)

    def _update_realtime_identity_or_notify(
        self, conn: sqlite3.Connection, update: UpdateUserName | UpdateNotifySettings, now: int
    ) -> None:
        if isinstance(update, UpdateUserName):
            self._update_realtime_username(conn, update, now)
            return
        self._update_realtime_notify(conn, update, now)

    def _apply_profile_push(
        self,
        profiles: EntityProfileRepository,
        push: ProfilePush,
        observation: ProfileFieldObservation,
    ) -> frozenset[str]:
        changed = profiles.apply_supplied_user(
            SuppliedProfileUser(
                push.entity_id,
                push.semantic_fields,
                push.semantic_coverage,
                push.fields,
                push.minimal,
                push.authoritative_photo,
            ),
            observation=observation,
        )
        if push.story is not None:
            changed |= profiles.apply_story_update(push.entity_id, push.story, observation=observation)
        return changed

    def _write_profile_pushes(
        self, conn: sqlite3.Connection, update: object, users: Sequence[object], topic_entities: Sequence[object]
    ) -> tuple[bool, bool, bool]:
        owner = self._self_id
        if owner is None:
            return False, False, False
        profiles = EntityProfileRepository(conn, section_ttl_seconds=self._profile_detail_ttl_seconds)
        annotations: list[tuple[object, ProfileFieldObservation]] = []
        handled_name = False
        invalidations: dict[int, tuple[set[str], ProfileFieldObservation, bool]] = {}
        pushes = [(user, project_user_profile(user)) for user in users]
        pushes.append((update, project_profile_update(update)))
        with write_transaction(conn):
            for source, push in pushes:
                if not self._eligible_profile_push(conn, push):
                    continue
                assert push is not None
                observation = self._profile_push_observation(conn, source, push)
                self._ensure_supplied_user_type(conn, source, push, observation)
                push = self._filter_retained_profile_identity(conn, push, observation)
                changed = self._apply_profile_push(profiles, push, observation)
                sections = self._owned_profile_push_sections(owner, push.entity_id, source, changed)
                if sections:
                    self._coalesce_profile_invalidation(
                        invalidations, push.entity_id, sections, observation, isinstance(source, UpdateUser)
                    )
                handled_name |= source is update and isinstance(update, UpdateUserName)
                annotations.append((source, observation))
            offered = self._flush_profile_invalidations(profiles, invalidations)
            topics_changed = self._write_topic_capabilities(conn, topic_entities, annotations)
        # Object lifetime, not an evictable cache: a repeated bundle keeps its original order.
        for source, observation in annotations:
            mark_push_applied(source, self._profile_push_owner, observation)
        return offered, handled_name, topics_changed

    @staticmethod
    def _ensure_supplied_user_type(
        conn: sqlite3.Connection, source: object, push: ProfilePush, observation: ProfileFieldObservation
    ) -> None:
        if not isinstance(source, User) or source.id != push.entity_id:
            return
        observed = classify_dialog_type(source)
        row = cast(
            tuple[str] | None, conn.execute("SELECT type FROM entities WHERE id=?", (push.entity_id,)).fetchone()
        )
        current = DialogType.parse(row[0]) if row is not None else DialogType.UNKNOWN
        if observed not in {DialogType.USER, DialogType.BOT} or current not in {
            DialogType.UNKNOWN,
            DialogType.USER,
            DialogType.BOT,
        }:
            raise ValueError("profile entity type conflict")
        if current is not DialogType.UNKNOWN and current is not observed and not source.min:
            raise ValueError("profile entity type conflict")
        if current is DialogType.UNKNOWN:
            apply_partial_entity_identity(
                conn,
                push.entity_id,
                PartialEntityIdentity(observed_at=observation.observed_at, observed_type=observed.value),
            )

    def _flush_profile_invalidations(
        self,
        profiles: EntityProfileRepository,
        invalidations: dict[int, tuple[set[str], ProfileFieldObservation, bool]],
    ) -> bool:
        offered = False
        for entity_id, (sections, observation, whole_peer) in invalidations.items():
            offered |= profiles.invalidate_profile_sections(
                entity_id,
                tuple(sorted(sections)),
                observation=observation,
                reason="user" if whole_peer else "supplied:" + ",".join(sorted(sections)),
                pair_mode=self._profile_pair_mode,
            )
        return offered

    @staticmethod
    def _coalesce_profile_invalidation(
        pending: dict[int, tuple[set[str], ProfileFieldObservation, bool]],
        entity_id: int,
        sections: tuple[str, ...],
        observation: ProfileFieldObservation,
        whole_peer: bool,
    ) -> None:
        previous = pending.get(entity_id)
        combined = set(sections)
        if previous is not None:
            combined.update(previous[0])
            whole_peer |= previous[2]
            if previous[1].order > observation.order:
                observation = previous[1]
        pending[entity_id] = combined, observation, whole_peer

    @staticmethod
    def _filter_retained_profile_identity(
        conn: sqlite3.Connection,
        push: ProfilePush,
        observation: ProfileFieldObservation,
    ) -> ProfilePush:
        if current_delivered_update() is None:
            return push
        newer = _newer_dialog_identity_fields(conn, push.entity_id, observation.order)
        excluded: set[str] = set()
        if "name" in newer:
            excluded.update(("name", "first_name", "last_name"))
        if "username" in newer:
            excluded.update(("username", "extra_usernames"))
        return _mask_profile_identity_fields(push, excluded)

    def _eligible_profile_push(self, conn: sqlite3.Connection, push: ProfilePush | None) -> bool:
        return (
            push is not None
            and self._self_id is not None
            and (
                personal_profile_peer(conn, self._self_id, push.entity_id)
                or (push.entity_id == self._self_id and bound_profile_account(conn) == self._self_id)
            )
        )

    def _profile_push_observation(
        self, conn: sqlite3.Connection, source: object, push: ProfilePush
    ) -> ProfileFieldObservation:
        assert self._self_id is not None
        return push_marker(source, self._profile_push_owner) or ProfileFieldObservation(
            owner_account_id=self._self_id,
            order=delivered_observation_order(conn),
            observed_at=delivered_observed_at(),
            source="min" if push.minimal else "update",
        )

    def _profile_push_sections(self, source: object, changed: frozenset[str]) -> tuple[str, ...]:
        if isinstance(source, UpdateUser):
            return (
                "full_profile",
                "personal_channel",
                "avatar_history",
                "common_chats",
                "saved_music",
                "saved_gifts",
                "current_stories",
                "pinned_stories",
            )
        if type(source).__name__ == "UpdateStory":
            return tuple(sorted(changed & {"current_stories", "pinned_stories"}))
        sections: set[str] = set()
        if "current_photo" in changed or "photo" in changed:
            sections.update(("avatar_history", "full_profile"))
        if "bot_info_version" in changed:
            sections.add("full_profile")
        if changed & {"stories_max_id", "stories_unavailable"}:
            sections.add("current_stories")
        return tuple(sorted(sections))

    def _owned_profile_push_sections(
        self, owner: int, entity_id: int, source: object, changed: frozenset[str]
    ) -> tuple[str, ...]:
        sections = self._profile_push_sections(source, changed)
        if entity_id == owner:
            return tuple(section for section in sections if section in {"full_profile", "personal_channel"})
        return sections

    async def _persist_profile_pushes(self, *, update: object = None, sender: object = None) -> bool:
        users: Sequence[object] = (sender,) if sender is not None else bundled_profile_users(update)
        topic_entities = self._topic_capability_entities(update, sender)
        if self._self_id is None or (not users and not topic_entities and profile_update_id(update) is None):
            return False
        offered, handled_name, topics_changed = await self._writer.run(
            lambda conn: self._write_profile_pushes(conn, update, users, topic_entities)
        )
        if offered:
            self._offer(DemandKind.ENTITY_PROFILE_REFRESH)
        if topics_changed:
            self._offer(DemandKind.TOPIC_SNAPSHOT)
        return handled_name

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_raw_identity_or_notify(self, update: object) -> None:
        """Materialize supplied profile facts and exact notification mute deadlines."""
        try:
            handled_name = await self._persist_profile_pushes(update=update)
            if not isinstance(update, (UpdateUserName, UpdateNotifySettings)) or handled_name:
                return
            await self._writer.run(
                lambda conn: self._update_realtime_identity_or_notify(conn, update, delivered_observed_at())
            )
        except Exception:
            fail_delivered_update()
            logger.exception("event_identity_or_notify_failed update=%r", type(update).__name__)

    def _topic_capability_entities(self, update: object, sender: object) -> tuple[object, ...]:
        entities: object = getattr(update, "_entities", None)
        if sender is not None and isinstance(sender, (User, Channel)):
            entities = {get_peer_id(sender): sender}
        if not isinstance(entities, Mapping):
            return ()
        return tuple(
            entity
            for key, entity in entities.items()
            if isinstance(entity, (User, Channel))
            and not bool(entity.min)
            and key == get_peer_id(entity)
            and full_history_enabled(self._conn, int(key))
        )

    def _write_topic_capabilities(
        self,
        conn: sqlite3.Connection,
        entities: Sequence[object],
        annotations: list[tuple[object, ProfileFieldObservation]],
    ) -> bool:
        require_write_transaction(conn)
        changed = False
        observations = {id(source): observation for source, observation in annotations}
        repository = TopicCatalogRepository(conn)
        for entity in entities:
            dialog_id = int(get_peer_id(cast(TypePeer, entity)))
            if not full_history_enabled(conn, dialog_id):
                continue
            observation = observations.get(id(entity)) or push_marker(entity, self._profile_push_owner)
            if observation is None:
                observation = ProfileFieldObservation(
                    owner_account_id=cast(int, self._self_id),
                    order=delivered_observation_order(conn),
                    observed_at=delivered_observed_at(),
                    source="update",
                )
            changed |= repository.observe_capability(dialog_id, is_topic_capable(entity), observation.order)
            if id(entity) not in observations:
                annotations.append((entity, observation))
        return changed

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
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
            now = delivered_observed_at()
            if isinstance(update, UpdateDialogPinned):
                await self._writer.run(lambda conn: self._update_dialog_pinned(conn, update, now))
            elif isinstance(update, UpdatePinnedDialogs):
                await self._writer.run(lambda conn: self._rewrite_pinned_dialogs(conn, update, now))
            elif isinstance(update, UpdateDialogUnreadMark):
                await self._update_dialog_unread_mark(update, now)
        except Exception:
            fail_delivered_update()
            logger.exception(
                "event_dialog_pinned_failed update=%r",
                type(update).__name__,
            )

    @staticmethod
    def _access_loss_cause(update: _ParticipantUpdateLike, self_id: int) -> str | None:
        participant = update.new_participant
        if isinstance(participant, ChannelParticipantBanned):
            rights = participant.banned_rights
            if not bool(getattr(rights, "view_messages", False)):
                return None
            return "self_left" if update.actor_id == self_id else "banned_by_admin"
        if participant is None or isinstance(participant, ChannelParticipantLeft):
            return "self_left" if update.actor_id == self_id else "removed_by_admin"
        return None

    @staticmethod
    def _participant_dialog_id(update: UpdateChannelParticipant | UpdateChatParticipant) -> int:
        if isinstance(update, UpdateChannelParticipant):
            return int(get_peer_id(PeerChannel(update.channel_id)))
        return int(get_peer_id(PeerChat(update.chat_id)))

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    @_acquisition(AcquisitionKind.ACCOUNT_SELF_PROFILE)
    async def on_raw_participant(self, update: UpdateChannelParticipant | UpdateChatParticipant) -> None:
        """Persist why this account lost a group, supergroup, or channel membership."""
        try:
            self_id = await self._resolve_self_id()
            if self_id is None or update.user_id != self_id:
                return
            cause = self._access_loss_cause(update, self_id)
            if cause is None:
                return
            dialog_id = self._participant_dialog_id(update)
            occurred_at = int(update.date.timestamp()) if update.date is not None else delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> bool:
                with write_transaction(conn):
                    return set_access_lost(
                        conn,
                        dialog_id,
                        occurred_at,
                        evidence=AccessLossEvidence("UpdateChannelParticipant", cause, update.actor_id),
                    )

            changed = await self._writer.run(_persist_event_0)
            if changed:
                self._synced_dialog_ids.discard(dialog_id)
                self._offer(DemandKind.DELTA_ACCESS_PROBE)
        except RpcAdmissionClosedError:
            raise
        except Exception:
            fail_delivered_update()
            logger.exception("event_channel_participant_failed")

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    @_acquisition(AcquisitionKind.ENTITY_LOOKUP)
    async def on_raw_channel_chat_update(self, update: _ChannelChatUpdateLike | _ChatUpdateLike) -> None:
        """Phase 42 EVENTS-03: UpdateChannel / UpdateChat → dialogs.needs_refresh=1.

        UpdateChannel for an already-resolved broadcast channel records durable
        linked-chat refresh demand; the shared owner performs the bounded RPC.

        Gated on _synced_dialog_ids; UPDATE-only.
        """
        try:
            dialog_id = _channel_chat_dialog_id(update)
            if dialog_id is None:
                return
            if dialog_id not in self._synced_dialog_ids:
                return
            now = delivered_observed_at()
            changed, row = await self._writer.run(
                lambda conn: self._mark_channel_chat_update(
                    conn,
                    dialog_id,
                    now=now,
                    invalidate_linked_chat=isinstance(update, UpdateChannel),
                )
            )
            if changed:
                self._offer(DemandKind.DIALOG_LIGHT_RECONCILIATION)
            logger.info("event_channel_chat_dirty dialog_id=%d", dialog_id)
        except RpcAdmissionClosedError:
            raise
        except Exception:
            fail_delivered_update()
            logger.exception(
                "event_channel_chat_update_failed update=%r",
                type(update).__name__,
            )
            return

        # Only previously resolved broadcast channels enter the refresh queue.
        if _should_refresh_linked_chat_id(update, row):
            self._offer(DemandKind.LINKED_CHAT_REFRESH)

    def _mark_channel_chat_update(
        self,
        conn: sqlite3.Connection,
        dialog_id: int,
        *,
        now: int,
        invalidate_linked_chat: bool = False,
    ) -> tuple[int, tuple[str | None, int | None] | None]:
        with write_transaction(conn):
            changed = conn.execute(_UPDATE_DIALOG_NEEDS_REFRESH_SQL, (now, dialog_id)).rowcount
            row = cast(
                tuple[str | None, int | None] | None,
                conn.execute(
                    "SELECT type, linked_chat_resolved_at FROM dialogs WHERE dialog_id = ?",
                    (dialog_id,),
                ).fetchone(),
            )
            if invalidate_linked_chat and row is not None and row[0] == "channel" and row[1] is not None:
                linked_chat_fact_owner.invalidate_from_update(conn, dialog_id, now)
        return changed, row

    async def retry_one_pending_linked_chat_fact(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Acquire only the selected generation and report its own accepted publication."""
        selected = budget.selected
        if selected is None:
            raise ValueError("linked-chat demand requires a selected revision")
        now = int(time.time())
        work = next(
            (
                item
                for item in linked_chat_fact_owner.pending_work(self._conn)
                if str(item.channel_id) == selected.key.target
                and repr((item.generation, item.generation)) == selected.revision
            ),
            None,
        )
        if work is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if work.retry_at > now:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        siblings_token = capture_channel_full_siblings_token(self._conn, work.channel_id)
        published = await self._acquire_linked_chat_work(work, work.generation, siblings_token, now)
        if published:
            return SliceOutcome(selected, SliceOutcomeKind.TERMINAL, repr((work.generation + 1, None)))
        kind = SliceOutcomeKind.NO_PROGRESS if published is None else SliceOutcomeKind.STALE
        return SliceOutcome(selected, kind)

    async def _acquire_linked_chat_work(
        self,
        work: LinkedChatWork,
        generation: int,
        siblings_token: ChannelFullSiblingsToken,
        now: int,
    ) -> bool | None:
        """Run local peer resolution, one RPC, then publish the accepted observation."""
        channel_id = work.channel_id
        input_channel = linked_chat_fact_owner.resolve_cached_input_peer(self._client, channel_id)
        if input_channel is None:
            await self._writer.run(lambda conn: self._defer_linked_chat_work(conn, work, now))
            logger.debug("linked_chat_refresh_no_input_peer dialog_id=%d", channel_id)
            return None

        full_result = await self._request_linked_chat_full(work, input_channel, now)
        if full_result is None:
            return None
        return await self._publish_linked_chat_observation(work, generation, siblings_token, full_result, now)

    async def _request_linked_chat_full(
        self,
        work: LinkedChatWork,
        input_channel: TypeInputChannel,
        now: int,
    ) -> object | None:
        """Acquire one full-channel result and persist recoverable failure state."""
        channel_id = work.channel_id
        try:
            return await linked_chat_fact_owner.acquire(self._client, input_channel)
        except (
            TelegramRpcAdmissionDeferred,
            TelegramRpcMethodRecoveryPending,
            TelegramRpcThrottled,
            RpcAdmissionClosedError,
        ):
            raise
        except ACCESS_LOST_ERRORS as exc:
            await self._mark_linked_chat_access_lost(channel_id, now, exc)
            return None
        except Exception:
            logger.debug("linked_chat_refresh_failed dialog_id=%d", channel_id, exc_info=True)
            raise

    async def _publish_linked_chat_observation(
        self,
        work: LinkedChatWork,
        generation: int,
        siblings_token: ChannelFullSiblingsToken,
        full_result: object,
        now: int,
    ) -> bool | None:
        """Validate and atomically publish canonical and sibling facts."""
        channel_id = work.channel_id
        try:
            validated_link = linked_chat_fact_owner.validate_observation(full_result, channel_id)
        except ValueError:
            await self._writer.run(lambda conn: self._defer_linked_chat_work(conn, work, now))
            logger.debug("linked_chat_refresh_invalid_observation dialog_id=%d", channel_id)
            return None

        def _persist_event_0(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                published = linked_chat_fact_owner.publish(
                    conn,
                    channel_id,
                    generation,
                    validated_link,
                    now,
                )
                if published:
                    write_channel_full_siblings(
                        conn,
                        channel_id,
                        full_result,
                        siblings_token,
                        observed_at=now,
                    )
            return published

        published = await self._writer.run(_persist_event_0)
        if published:
            self._offer(DemandKind.SCHEDULED_DISCOVERY)
            if validated_link is not None and validated_link != 0:
                self._offer(DemandKind.COLD_PEER_PAGE)
        logger.info(
            "linked_chat_refresh dialog_id=%d published=%s",
            channel_id,
            published,
        )
        return published

    def _defer_linked_chat_work(
        self,
        conn: sqlite3.Connection,
        work: LinkedChatWork,
        now: int,
        *,
        flood_wait_seconds: int | None = None,
    ) -> None:
        with write_transaction(conn):
            retry_at = linked_chat_fact_owner.retry_at_for_failure(
                conn,
                work,
                now,
                flood_wait_seconds=flood_wait_seconds,
            )
            linked_chat_fact_owner.defer(conn, work, retry_at)

    async def _mark_linked_chat_access_lost(self, channel_id: int, now: int, error: Exception) -> None:
        def _persist_event_0(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                return set_access_lost(conn, channel_id, now, reason=type(error).__name__)

        changed = await self._writer.run(_persist_event_0)
        if changed:
            self._synced_dialog_ids.discard(channel_id)
            self._offer(DemandKind.DELTA_ACCESS_PROBE)

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
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
        operation_id = uuid.uuid4().hex
        identity = _inbox_read_update_identity(update)
        if identity is None:
            return
        dialog_id, update_type = identity
        try:
            max_id_raw = getattr(update, "max_id", None)
            still_unread_raw = getattr(update, "still_unread_count", None)
            self._record_observation(
                kind="telegram.inbox_read_received",
                dialog_id=dialog_id,
                operation_id=operation_id,
                payload={
                    "update_type": update_type,
                    "max_id": max_id_raw if isinstance(max_id_raw, int) else None,
                    "still_unread_count": still_unread_raw if isinstance(still_unread_raw, int) else None,
                    "pts": getattr(update, "pts", None),
                },
            )
            if not _is_valid_nonnegative_int(still_unread_raw, allow_none=True):
                logger.debug(
                    "event_raw_inbox_read_invalid_still_unread_count dialog_id=%d value=%r",
                    dialog_id,
                    still_unread_raw,
                )
                still_unread = None
                partial_reason = "invalid_still_unread_count"
            else:
                still_unread = cast(int | None, still_unread_raw)
                partial_reason = None
            if not _is_valid_nonnegative_int(max_id_raw):
                logger.debug(
                    "event_raw_inbox_read_invalid_max_id dialog_id=%d max_id=%r",
                    dialog_id,
                    max_id_raw,
                )
                self._record_observation(
                    kind="sync.inbox_read_finished",
                    dialog_id=dialog_id,
                    operation_id=operation_id,
                    outcome="ignored",
                    reason_code="invalid_max_id",
                )
                return
            max_id = cast(int, max_id_raw)
            (cursor_rowcount, unread_rowcount), observation = await self._writer.run(
                lambda conn: _apply_observed_inbox_read(
                    conn,
                    dialog_id=dialog_id,
                    max_id=max_id,
                    still_unread=still_unread,
                    operation_id=operation_id,
                    partial_reason=partial_reason,
                )
            )
            self._record_observation(**observation)
            logger.debug(
                "event_raw_inbox_read dialog_id=%d max_id=%d still_unread_count=%s cursor_updated=%d unread_updated=%d",
                dialog_id,
                max_id,
                still_unread,
                cursor_rowcount,
                unread_rowcount,
            )
        except Exception:
            fail_delivered_update()
            self._record_observation(
                kind="sync.inbox_read_finished",
                dialog_id=dialog_id,
                operation_id=operation_id,
                outcome="failed",
                reason_code="handler_exception",
            )
            logger.exception(
                "event_raw_inbox_read_failed update=%r",
                type(update).__name__,
            )

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_raw_forum_topic_pinned(self, update: object) -> None:
        """Phase 42 EVENTS-05: UpdatePinnedForumTopic → topic_metadata.pinned.

        Gated on _synced_dialog_ids; UPDATE-only. Missing-row UPDATE matches
        0 rows without crashing — bootstrap / on_new_message UPSERT remain the
        sole row-creation paths.
        """
        try:
            if isinstance(update, UpdatePinnedForumTopics):
                await self.on_raw_forum_topics_pinned(cast(_ForumTopicsPinnedUpdateLike, update))
                return
            if not isinstance(update, UpdatePinnedForumTopic):
                return
            singular_update = cast(_ForumTopicPinnedUpdateLike, update)
            peer = singular_update.peer
            topic_id_raw = singular_update.topic_id
            if peer is None or topic_id_raw is None:
                return
            dialog_id = int(cast(int, get_peer_id(peer)))
            if dialog_id not in self._synced_dialog_ids:
                return
            if not self._metadata_realtime_allowed(self._realtime_coverage(dialog_id)):
                return
            if not _is_valid_nonnegative_int(topic_id_raw) or singular_update.pinned is None:
                return
            topic_id = int(topic_id_raw)
            now = delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    observation_order = delivered_observation_order(conn)
                    SQLiteTopicMetadataRepository(conn).apply_topic_pin(
                        dialog_id,
                        topic_id,
                        pinned=bool(singular_update.pinned),
                        observed_at=now,
                        observation_order=observation_order,
                    )
                    TopicCatalogRepository(conn).request(dialog_id, "topic_pin_changed")

            await self._writer.run(_persist_event_0)
            logger.info(
                "event_forum_topic_pinned dialog_id=%d topic_id=%d pinned=%d",
                dialog_id,
                topic_id,
                int(bool(singular_update.pinned)),
            )
        except Exception:
            fail_delivered_update()
            logger.exception(
                "event_forum_topic_pinned_failed update=%r",
                type(update).__name__,
            )

    @_demand_root(DemandKind.REALTIME_EVENT_ACQUISITION)
    async def on_raw_forum_topics_pinned(self, update: object) -> None:
        """Apply UpdatePinnedForumTopics as a complete known-topic membership set."""
        try:
            if not isinstance(update, UpdatePinnedForumTopics):
                return
            peer = update.peer
            order = _validated_topic_pin_order(update.order)
            if peer is None or order is None:
                return
            dialog_id = int(get_peer_id(peer))
            if dialog_id not in self._synced_dialog_ids:
                return
            if not self._metadata_realtime_allowed(self._realtime_coverage(dialog_id)):
                return
            now = delivered_observed_at()

            def _persist_event_0(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    observation_order = delivered_observation_order(conn)
                    SQLiteTopicMetadataRepository(conn).apply_topic_pins(
                        dialog_id,
                        order,
                        observed_at=now,
                        observation_order=observation_order,
                    )
                    TopicCatalogRepository(conn).request(dialog_id, "topic_pins_changed")

            await self._writer.run(_persist_event_0)
            logger.info(
                "event_forum_topics_pinned dialog_id=%d pinned_count=%d",
                dialog_id,
                len(order),
            )
        except Exception:
            fail_delivered_update()
            logger.exception(
                "event_forum_topics_pinned_failed update=%r",
                type(update).__name__,
            )

    # ------------------------------------------------------------------
    # DM gap scan
    # ------------------------------------------------------------------

    @_rpc_scope(TelegramRpcSource.DELTA_SYNC)
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
            except RpcAdmissionClosedError:
                raise
            except Exception:
                logger.warning(
                    "dm_gap_scan_dialog_failed dialog_id=%d",
                    dialog_id,
                    exc_info=True,
                )

        logger.info("dm_gap_scan marked_deleted=%d", total_marked)
        return total_marked

    @_rpc_scope(TelegramRpcSource.DELTA_SYNC)
    async def run_dm_gap_scan_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
        """Verify one bounded deletion page for the durable delta subqueue."""
        return await self._scan_dm_gap_message_page(dialog_id, message_ids)

    @_rpc_scope(TelegramRpcSource.DELTA_SYNC)
    async def _scan_dm_gap_dialog(self, dialog_id: int, scan_started_at: int) -> int:
        message_ids = list(list_undeleted_message_ids(self._conn, dialog_id, scan_started_at))
        if not message_ids:
            return 0

        marked = 0
        # Batch in groups of 100 (Telegram API limit)
        for batch_start in range(0, len(message_ids), 100):
            batch = message_ids[batch_start : batch_start + 100]
            marked += await self._scan_dm_gap_message_page(dialog_id, batch)
        return marked

    async def _scan_dm_gap_message_page(self, dialog_id: int, message_ids: Sequence[int]) -> int:
        results = cast(
            "Sequence[_MessageLike | None]",
            await self._client.get_messages(dialog_id, ids=list(message_ids)),
        )
        now = int(time.time())

        def _persist_event_0(conn: sqlite3.Connection) -> int:
            marked = 0
            with write_transaction(conn):  # atomic per-dialog batch
                for queried_id, returned_msg in zip(message_ids, results, strict=False):
                    if returned_msg is None and mark_message_deleted(conn, dialog_id, queried_id, now):
                        marked += 1
            return marked

        return await self._writer.run(_persist_event_0)


_EXPORTED_SYMBOLS = (
    UpdateProcessingBarrier,
    EventHandlerManager,
    EventHandlerManager.register,
    EventHandlerManager.unregister,
    EventHandlerManager.refresh_synced_dialogs,
    EventHandlerManager.run_dm_gap_scan,
    EventHandlerManager.run_dm_gap_scan_page,
)
