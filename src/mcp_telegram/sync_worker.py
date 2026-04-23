"""FullSyncWorker — bulk history fetch engine for v1.5 Persistent Sync.

Fetches all historical messages for marked dialogs in batches of 100,
checkpointing progress after each batch so restarts resume without
re-scanning from scratch.

FloodWait causes an interruptible sleep — progress is never lost on
rate limits.

DM bootstrap auto-enrolls all User-type dialogs at daemon startup.

Architecture:
- Standalone module so daemon.py stays focused on process lifecycle.
- FullSyncWorker is a stateful class instantiated once per daemon run.
- Plugs into daemon.py sync_main() between heartbeat ticks.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any

from telethon.errors import (  # type: ignore[import-untyped]
    ChannelBannedError,
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    FloodWaitError,  # type: ignore[import-untyped]
    InputUserDeactivatedError,
    PeerFloodError,
    PeerIdInvalidError,
    RPCError,  # type: ignore[import-untyped]
    UserBannedInChannelError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserKickedError,
    UserPrivacyRestrictedError,
)
from telethon.tl import types  # type: ignore[import-untyped]

from .fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from .resolver import latinize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ExtractedMessage dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMessage:
    """Row shape for INSERT OR REPLACE INTO messages.

    Field names are the single source of truth for column names and INSERT SQL.
    is_deleted is always 0 at insert time (hardcoded in INSERT_MESSAGE_SQL).
    """

    dialog_id: int
    message_id: int
    sent_at: int
    text: str | None
    sender_id: int | None
    sender_first_name: str | None
    media_description: str | None
    reply_to_msg_id: int | None
    forum_topic_id: int | None
    edit_date: int | None
    grouped_id: int | None
    reply_to_peer_id: int | None
    out: int
    is_service: int
    post_author: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReactionRecord:
    """One row in message_reactions."""

    dialog_id: int
    message_id: int
    emoji: str
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityRecord:
    """One row in message_entities."""

    dialog_id: int
    message_id: int
    offset: int
    length: int
    type: str
    value: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardRecord:
    """One row in message_forwards."""

    dialog_id: int
    message_id: int
    fwd_from_peer_id: int | None
    fwd_from_name: str | None
    fwd_date: int | None
    fwd_channel_post: int | None


@dataclass
class ExtractedMessage:
    """Bundle of extracted rows for atomic multi-table insert."""

    message: StoredMessage
    reactions: list[ReactionRecord] = field(default_factory=list)
    entities: list[EntityRecord] = field(default_factory=list)
    forward: ForwardRecord | None = None


# ---------------------------------------------------------------------------
# SQL constants — generated from dataclass field names (single source of truth)
# ---------------------------------------------------------------------------


def _insert_sql(table: str, dc_type: type) -> str:
    """Return INSERT OR REPLACE SQL with named params derived from a dataclass."""
    col_names = tuple(f.name for f in fields(dc_type))
    return (
        f"INSERT OR REPLACE INTO {table} ({', '.join(col_names)}) "
        f"VALUES ({', '.join(':' + n for n in col_names)})"
    )


_SM_FIELDS = tuple(f.name for f in fields(StoredMessage))
INSERT_MESSAGE_SQL = (
    f"INSERT OR REPLACE INTO messages ({', '.join(_SM_FIELDS)}, is_deleted) "
    f"VALUES ({', '.join(':' + n for n in _SM_FIELDS)}, 0)"
)

INSERT_REACTION_SQL = _insert_sql("message_reactions", ReactionRecord)
INSERT_ENTITY_SQL = _insert_sql("message_entities", EntityRecord)
INSERT_FORWARD_SQL = _insert_sql("message_forwards", ForwardRecord)

_DELETE_REACTIONS_SQL = "DELETE FROM message_reactions WHERE dialog_id = ? AND message_id = ?"


def apply_reactions_delta(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    reaction_rows: list[ReactionRecord],
) -> None:
    """Per-message reaction delta primitive.

    DELETE existing rows for ``(dialog_id, message_id)`` then INSERT OR REPLACE
    the supplied rows. Empty ``reaction_rows`` still performs the DELETE — this
    is the reaction-removal path (Phase 39.2 AC-2 / AC-2-RAW).

    The caller controls transaction boundary (e.g. ``with conn:``); this helper
    does NOT open its own transaction.

    Per-message primitive used by event handlers and JIT freshen path.
    FullSyncWorker retains its own batched ``executemany`` insert path
    (insert_messages_with_fts) for bulk history inserts; that code path is
    intentionally NOT refactored to share this helper.
    """
    conn.execute(_DELETE_REACTIONS_SQL, (dialog_id, message_id))
    if reaction_rows:
        conn.executemany(INSERT_REACTION_SQL, [asdict(r) for r in reaction_rows])


_DELETE_ENTITIES_SQL = "DELETE FROM message_entities WHERE dialog_id = ? AND message_id = ?"

_DELETE_FORWARD_SQL = "DELETE FROM message_forwards WHERE dialog_id = ? AND message_id = ?"


def insert_messages_with_fts(
    conn: sqlite3.Connection,
    extracted: list[ExtractedMessage],
) -> None:
    """Insert message rows and all related tables atomically.

    Writes to: messages, messages_fts, message_reactions, message_entities,
    message_forwards. Callers wrap with `with conn:` for transaction control.

    IMPORTANT: Child tables (reactions, entities, forwards) are DELETE'd
    before INSERT to ensure edit idempotency. Without this, an edited
    message would accumulate stale child rows from prior versions.
    """
    msgs = [em.message for em in extracted]
    conn.executemany(INSERT_MESSAGE_SQL, [asdict(m) for m in msgs])
    conn.executemany(DELETE_FTS_SQL, ((m.dialog_id, m.message_id) for m in msgs))
    conn.executemany(
        INSERT_FTS_SQL,
        ((m.dialog_id, m.message_id, stem_text(m.text)) for m in msgs),
    )

    # Delete existing child rows before re-inserting (edit idempotency).
    id_pairs = [(m.dialog_id, m.message_id) for m in msgs]
    conn.executemany(_DELETE_REACTIONS_SQL, id_pairs)
    conn.executemany(_DELETE_ENTITIES_SQL, id_pairs)
    conn.executemany(_DELETE_FORWARD_SQL, id_pairs)

    # Insert fresh child rows
    all_reactions = [r for em in extracted for r in em.reactions]
    if all_reactions:
        conn.executemany(INSERT_REACTION_SQL, [asdict(r) for r in all_reactions])
    all_entities = [e for em in extracted for e in em.entities]
    if all_entities:
        conn.executemany(INSERT_ENTITY_SQL, [asdict(e) for e in all_entities])
    all_forwards = [em.forward for em in extracted if em.forward is not None]
    if all_forwards:
        conn.executemany(INSERT_FORWARD_SQL, [asdict(f) for f in all_forwards])


_NEXT_PENDING_SQL = (
    "SELECT dialog_id, sync_progress FROM synced_dialogs "
    "WHERE status IN ('syncing', 'not_synced') "
    "ORDER BY rowid LIMIT 1"
)

_UPDATE_PROGRESS_SQL = "UPDATE synced_dialogs SET sync_progress = ?, status = ?, total_messages = ? WHERE dialog_id = ?"
# Params: (progress, status, total_messages, dialog_id) — 4 params

_UPDATE_PROGRESS_DONE_SQL = (
    "UPDATE synced_dialogs SET sync_progress = ?, status = ?, total_messages = ?, "
    "last_synced_at = ? WHERE dialog_id = ?"
)
# Params: (progress, status, total_messages, last_synced_at, dialog_id) — 5 params

INSERT_DIALOG_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'syncing')"

UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities (id, type, name, username, name_normalized, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
)

_ACCESS_LOST_ERRORS = (
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserKickedError,
    ChannelBannedError,
)

_SET_ACCESS_LOST_SQL = "UPDATE synced_dialogs SET status = 'access_lost', access_lost_at = ? WHERE dialog_id = ?"


# ---------------------------------------------------------------------------
# Module-level field extraction helpers (shared with DeltaSyncWorker)
# ---------------------------------------------------------------------------


def extract_reply_and_topic(msg: Any) -> tuple[int | None, int | None]:
    """Extract reply_to_msg_id and forum_topic_id from a Telethon message.

    Shared between extract_message_row (sync path) and _msg_to_dict (API path)
    to avoid duplicating the forum_topic / reply_to_reply_top_id branching.

    Returns (reply_to_msg_id, forum_topic_id).
    """
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is None:
        return None, None
    raw_reply_msg_id = getattr(reply_to, "reply_to_msg_id", None)
    reply_to_msg_id = int(raw_reply_msg_id) if raw_reply_msg_id is not None else None
    forum_topic_id: int | None = None
    if getattr(reply_to, "forum_topic", False):
        reply_top_id = getattr(reply_to, "reply_to_reply_top_id", None)
        forum_topic_id = int(reply_top_id) if reply_top_id is not None else 1
    return reply_to_msg_id, forum_topic_id


def extract_reactions_rows(dialog_id: int, message_id: int, reactions: Any | None) -> list[ReactionRecord]:
    """Extract reaction rows from a Telethon MessageReactions object.

    Returns empty list if reactions is None or has no results.
    """
    if reactions is None:
        return []
    results = getattr(reactions, "results", None)
    if not results:
        return []
    rows: list[ReactionRecord] = []
    for item in results:
        reaction = getattr(item, "reaction", None)
        emoticon = getattr(reaction, "emoticon", None) if reaction is not None else None
        count = getattr(item, "count", 0)
        if emoticon is not None:
            rows.append(ReactionRecord(
                dialog_id=dialog_id,
                message_id=message_id,
                emoji=emoticon,
                count=int(count),
            ))
    return rows


# Telethon entity types worth capturing for analytics.
# Populated lazily because telethon may not be installed in test env.
_ANALYTICS_ENTITY_TYPES: dict[type, str] = {}


def _init_entity_types() -> None:
    """Lazily populate _ANALYTICS_ENTITY_TYPES from Telethon types.

    Safe to call multiple times -- no-op after first initialization.
    Thread-safety: daemon is single-threaded asyncio, no concurrent mutation.
    """
    if _ANALYTICS_ENTITY_TYPES:
        return
    try:
        from telethon.tl import types as tl  # type: ignore[import-untyped]

        _ANALYTICS_ENTITY_TYPES.update(
            {
                tl.MessageEntityMention: "mention",
                tl.MessageEntityMentionName: "mention_name",
                tl.MessageEntityHashtag: "hashtag",
                tl.MessageEntityUrl: "url",
                tl.MessageEntityTextUrl: "text_url",
            }
        )
    except ImportError:
        pass  # Tests run without telethon


def _utf16_slice(text: str, offset: int, length: int) -> str | None:
    """Extract text span using UTF-16 code unit offsets.

    Telegram entity offsets are UTF-16 code unit offsets. Python strings
    use UTF-32 (one index per codepoint). For non-BMP characters (emoji,
    supplementary plane), a naive text[offset:offset+length] produces wrong
    results because a single supplementary character occupies 2 UTF-16 code
    units but 1 Python str index.

    This helper encodes to UTF-16-LE, slices at the byte level (2 bytes
    per code unit), then decodes back. This correctly handles all Unicode.

    Returns None on decode error -- caller should SKIP the entity row
    rather than store incorrect data. Addresses review round 3
    Priority Action #4.
    """
    try:
        encoded = text.encode("utf-16-le")
        byte_offset = offset * 2
        byte_length = length * 2
        return encoded[byte_offset : byte_offset + byte_length].decode("utf-16-le")
    except (UnicodeDecodeError, IndexError):
        return None


def extract_entity_rows(dialog_id: int, message_id: int, msg: Any) -> list[EntityRecord]:
    """Extract analytics-valuable entity rows from a Telethon message.

    Captures: mention, mention_name, hashtag, url, text_url.
    Skips: bold, italic, code, strikethrough (no analytics value).

    Entity value population (addresses review Priority Action #1):
    - mention: value = @username text span (e.g. "@alice"). Note: CONTEXT.md
      specified value=peer_id for mention, but Telethon's MessageEntityMention
      does NOT carry a peer_id -- it only marks a text span. Resolving
      @username to peer_id would require a separate API call not available at
      sync time. The @username text span IS the correct value for mention
      analytics (e.g. "who is mentioned most" = GROUP BY value).
      MessageEntityMentionName (a different entity type) DOES carry user_id.
    - mention_name: value = str(user_id) from entity attribute
    - hashtag: value = text span (e.g. "#topic")
    - url: value = text span (e.g. "https://example.com")
    - text_url: value = entity.url attribute (hyperlink URL, different from display text)

    Uses isinstance() for entity type matching (not type(e)==).
    Uses _utf16_slice for correct Unicode handling. Skips entity on decode
    error (Priority Action #4) -- does NOT fallback to naive slicing.
    """
    entities = getattr(msg, "entities", None)
    if not entities:
        return []
    _init_entity_types()
    if not _ANALYTICS_ENTITY_TYPES:
        return []  # Telethon not available (test env)
    text = getattr(msg, "message", "") or ""
    rows: list[EntityRecord] = []
    for e in entities:
        entity_type: str | None = None
        for cls, type_name in _ANALYTICS_ENTITY_TYPES.items():
            if isinstance(e, cls):
                entity_type = type_name
                break
        if entity_type is None:
            continue
        offset = getattr(e, "offset", 0)
        length = getattr(e, "length", 0)
        value: str | None = None
        if entity_type == "mention":
            # @username text span (peer_id not available on MessageEntityMention)
            value = _utf16_slice(text, offset, length) if text else None
            if value is None and text:
                continue  # Skip row on decode error (Priority Action #4)
        elif entity_type == "mention_name":
            # user_id from entity attribute (not from text)
            value = str(getattr(e, "user_id", ""))
        elif entity_type == "hashtag":
            # #topic text span
            value = _utf16_slice(text, offset, length) if text else None
            if value is None and text:
                continue  # Skip row on decode error (Priority Action #4)
        elif entity_type == "url":
            # URL text span
            value = _utf16_slice(text, offset, length) if text else None
            if value is None and text:
                continue  # Skip row on decode error (Priority Action #4)
        elif entity_type == "text_url":
            # Hyperlink URL from entity attribute (display text is different)
            value = getattr(e, "url", None)
        rows.append(EntityRecord(
            dialog_id=dialog_id,
            message_id=message_id,
            offset=offset,
            length=length,
            type=entity_type,
            value=value,
        ))
    return rows


async def _resolve_peer_name(client: Any, peer_id: int) -> str | None:
    """Return display name for a Telegram peer.

    Tries Telethon's session cache first; falls back to an API call when the
    entity is not cached. Returns None when the peer is permanently inaccessible
    (private/deleted/banned account, unknown ID).
    """
    try:
        entity = await client.get_entity(peer_id)
        name = getattr(entity, "title", None) or getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None)
        if last:
            name = f"{name} {last}".strip()
        return name or None
    except FloodWaitError as e:
        logger.warning("resolve_peer_name_flood_wait peer_id=%d retry_after=%ds", peer_id, e.seconds)
        return None
    except PeerFloodError:
        logger.warning("resolve_peer_name_peer_flood peer_id=%d", peer_id)
        return None
    except (
        ChannelPrivateError,
        InputUserDeactivatedError,
        PeerIdInvalidError,
        UserDeactivatedBanError,
        UserDeactivatedError,
        UserPrivacyRestrictedError,
    ):
        logger.debug("resolve_peer_name_inaccessible peer_id=%d", peer_id)
        return None
    except Exception:
        logger.warning("resolve_peer_name_unexpected peer_id=%d", peer_id, exc_info=True)
        return None


async def _build_fwd_entity_map(msg: Any, client: Any) -> dict[int, str]:
    """Return {peer_id: name} for the forward source of a single message.

    Returns an empty dict when the message is not a forward, already has
    fwd_from.from_name, or the peer cannot be resolved.
    """
    fwd = getattr(msg, "fwd_from", None)
    if not fwd or getattr(fwd, "from_name", None) is not None:
        return {}
    from_id = getattr(fwd, "from_id", None)
    if from_id is None:
        return {}
    peer_id: int | None = None
    for attr in ("user_id", "channel_id", "chat_id"):
        pid = getattr(from_id, attr, None)
        if pid is not None:
            peer_id = int(pid)
            break
    if peer_id is None:
        return {}
    name = await _resolve_peer_name(client, peer_id)
    return {peer_id: name} if name else {}


def extract_fwd_row(
    dialog_id: int,
    message_id: int,
    msg: Any,
    entity_name_map: dict[int, str] | None = None,
) -> ForwardRecord | None:
    """Extract forward metadata from a Telethon message.

    entity_name_map is a {peer_id: name} dict built from the batch response
    before this call — used to populate fwd_from_name for public senders
    whose name lives in the batch's users/chats, not in fwd_from.from_name.

    Returns ForwardRecord or None if not a forward.
    """
    fwd = getattr(msg, "fwd_from", None)
    if fwd is None:
        return None
    from_id = getattr(fwd, "from_id", None)
    fwd_from_peer_id: int | None = None
    if from_id is not None:
        for attr in ("user_id", "channel_id", "chat_id"):
            pid = getattr(from_id, attr, None)
            if pid is not None:
                fwd_from_peer_id = int(pid)
                break
    fwd_from_name = getattr(fwd, "from_name", None)
    if fwd_from_name is None and fwd_from_peer_id is not None and entity_name_map:
        fwd_from_name = entity_name_map.get(fwd_from_peer_id)
    fwd_date_raw = getattr(fwd, "date", None)
    fwd_date: int | None = None
    if fwd_date_raw is not None:
        try:
            fwd_date = int(fwd_date_raw.timestamp())
        except Exception:
            fwd_date = None
    fwd_channel_post = getattr(fwd, "channel_post", None)
    if fwd_channel_post is not None:
        fwd_channel_post = int(fwd_channel_post)
    return ForwardRecord(
        dialog_id=dialog_id,
        message_id=message_id,
        fwd_from_peer_id=fwd_from_peer_id,
        fwd_from_name=fwd_from_name,
        fwd_date=fwd_date,
        fwd_channel_post=fwd_channel_post,
    )


def extract_message_row(dialog_id: int, msg: Any, entity_name_map: dict[int, str] | None = None) -> ExtractedMessage:
    """Extract sync.db row bundle from a Telethon message object.

    Returns an ExtractedMessage with a typed StoredMessage plus typed satellite
    records for atomic multi-table insert.
    """
    message_id = int(getattr(msg, "id", 0))

    date = getattr(msg, "date", None)
    sent_at = int(date.timestamp()) if isinstance(date, datetime) else 0

    text = getattr(msg, "message", None)

    sender_id = getattr(msg, "sender_id", None)
    sender = getattr(msg, "sender", None)
    sender_first_name = getattr(sender, "first_name", None) if sender is not None else None

    media = getattr(msg, "media", None)
    media_description: str | None = type(media).__name__ if media is not None else None

    reply_to_msg_id, forum_topic_id = extract_reply_and_topic(msg)

    # -- New v7 columns --
    edit_date_raw = getattr(msg, "edit_date", None)
    edit_date: int | None = int(edit_date_raw.timestamp()) if edit_date_raw is not None else None
    grouped_id_raw = getattr(msg, "grouped_id", None)
    grouped_id: int | None = int(grouped_id_raw) if grouped_id_raw is not None else None

    reply_to = getattr(msg, "reply_to", None)
    reply_to_peer_raw = getattr(reply_to, "reply_to_peer_id", None) if reply_to is not None else None
    reply_to_peer_id: int | None = None
    if reply_to_peer_raw is not None:
        for attr in ("user_id", "channel_id", "chat_id"):
            pid = getattr(reply_to_peer_raw, attr, None)
            if pid is not None:
                reply_to_peer_id = int(pid)
                break

    # -- Phase 39.1 v9 columns: DM sender discriminators --
    # `out` carries direction for DMs (sender is implicit — either self or peer).
    # `is_service` flags MessageService rows (chat events) so "System" rendering
    # is reserved for them rather than any row with sender_id IS NULL.
    is_service = 1 if isinstance(msg, types.MessageService) else 0
    out = 1 if getattr(msg, "out", False) else 0
    post_author: str | None = getattr(msg, "post_author", None)

    stored = StoredMessage(
        dialog_id=dialog_id,
        message_id=message_id,
        sent_at=sent_at,
        text=text,
        sender_id=sender_id,
        sender_first_name=sender_first_name,
        media_description=media_description,
        reply_to_msg_id=reply_to_msg_id,
        forum_topic_id=forum_topic_id,
        edit_date=edit_date,
        grouped_id=grouped_id,
        reply_to_peer_id=reply_to_peer_id,
        out=out,
        is_service=is_service,
        post_author=post_author,
    )
    reactions = extract_reactions_rows(dialog_id, message_id, getattr(msg, "reactions", None))
    entities = extract_entity_rows(dialog_id, message_id, msg)
    forward = extract_fwd_row(dialog_id, message_id, msg, entity_name_map=entity_name_map)

    return ExtractedMessage(message=stored, reactions=reactions, entities=entities, forward=forward)


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

        Handles FloodWaitError with interruptible sleep and RPCError
        gracefully — a transient Telegram error does not kill the daemon.

        Returns:
            Count of newly enrolled dialogs (0 if all already present).
        """
        enrolled = 0
        now = int(time.time())
        try:
            async for dialog in self._client.iter_dialogs():
                if not isinstance(dialog.entity, types.User):
                    continue
                cursor = self._conn.execute(INSERT_DIALOG_SQL, (dialog.id,))
                if cursor.rowcount > 0:
                    enrolled += 1
                entity = dialog.entity
                first = getattr(entity, "first_name", None) or ""
                last = getattr(entity, "last_name", None) or ""
                name: str | None = f"{first} {last}".strip() or None
                entity_type_str = "Bot" if getattr(entity, "bot", False) else "User"
                self._conn.execute(
                    UPSERT_ENTITY_SQL,
                    (
                        dialog.id,
                        entity_type_str,
                        name,
                        getattr(entity, "username", None),
                        latinize(name) if name else None,
                        now,
                    ),
                )
        except FloodWaitError as exc:
            wait_seconds = getattr(exc, "seconds", 60)
            logger.warning(
                "dm_bootstrap flood_wait=%ds enrolled_so_far=%d — committing partial progress",
                wait_seconds,
                enrolled,
            )
        except RPCError as exc:
            logger.warning(
                "dm_bootstrap rpc_error=%s enrolled_so_far=%d — committing partial progress",
                exc,
                enrolled,
            )
        except (TimeoutError, OSError) as exc:
            logger.warning(
                "dm_bootstrap network_error=%s enrolled_so_far=%d — committing partial progress",
                exc,
                enrolled,
            )
        self._conn.commit()
        logger.info("dm_bootstrap enrolled=%d new DM dialogs", enrolled)
        return enrolled

    async def process_one_batch(self) -> bool:
        """Fetch one batch of messages for the next pending dialog.

        Picks the first dialog with status in ('syncing', 'not_synced'),
        fetches up to 100 messages from where it left off, stores them,
        and updates sync_progress atomically.

        Returns:
            True  — all dialogs are fully synced (idle mode safe).
            False — more work remains (same dialog or other pending dialogs).
        """
        pending = self._next_pending_dialog()
        if pending is None:
            return True  # nothing to do — all synced

        dialog_id, sync_progress = pending
        _, is_done = await self._fetch_batch(dialog_id, sync_progress)
        if not is_done:
            return False  # more batches needed for this dialog
        # Dialog done — check if more pending dialogs remain
        return self._next_pending_dialog() is None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _next_pending_dialog(self) -> tuple[int, int] | None:
        """Return (dialog_id, sync_progress) for the next pending dialog.

        Selects in rowid (insertion) order — no prioritization.
        Returns None when no dialogs have status in ('syncing', 'not_synced').
        """
        row = self._conn.execute(_NEXT_PENDING_SQL).fetchone()
        if row is None:
            return None
        return int(row[0]), int(row[1]) if row[1] is not None else 0

    async def _fetch_batch(self, dialog_id: int, sync_progress: int) -> tuple[int, bool]:
        """Fetch up to 100 messages for dialog_id older than sync_progress.

        Uses offset_id=sync_progress (exclusive) so each batch fetches
        messages strictly older than the last committed checkpoint.
        After a full batch (100 msgs), sync_progress advances to the min
        message_id; a partial or empty batch marks the dialog 'synced'.

        On FloodWaitError: sleep interruptibly, return (same_progress, False).
        On other RPCError: log ERROR, return (same_progress, False) — dialog stays
        in-progress for retry on the next sync cycle.

        Returns:
            (new_progress, is_done)
        """
        if sync_progress == 0:
            logger.info("sync_start dialog_id=%d", dialog_id)
        try:
            result = await self._client.get_messages(entity=dialog_id, limit=100, offset_id=sync_progress)
            total_messages = result.total  # Telegram-side count from TotalList
            batch = list(result)
            # Note: batch size 100 keeps memory bounded; get_messages needed for .total
        except FloodWaitError as exc:
            logger.warning("FloodWait dialog_id=%d — sleeping %ds", dialog_id, exc.seconds)
            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=float(exc.seconds))
            except TimeoutError:
                pass  # slept the full duration; retry same batch next call
            return sync_progress, False
        except _ACCESS_LOST_ERRORS as exc:
            logger.warning("access_lost dialog_id=%d — %s: %s", dialog_id, type(exc).__name__, exc)
            now = int(time.time())
            with self._conn:
                self._conn.execute(_SET_ACCESS_LOST_SQL, (now, dialog_id))
            return sync_progress, True
        except RPCError as exc:
            logger.error(
                "sync_batch_rpc_error dialog_id=%d error=%s — dialog NOT marked synced, will retry",
                dialog_id,
                exc,
                exc_info=True,
            )
            return sync_progress, False  # leave dialog in-progress for retry

        if not batch:
            # No more messages — dialog fully synced
            now = int(time.time())
            with self._conn:
                self._conn.execute(
                    _UPDATE_PROGRESS_DONE_SQL,
                    (sync_progress, "synced", total_messages, now, dialog_id),
                )
            logger.info("sync_done dialog_id=%d status=synced (empty batch)", dialog_id)
            return sync_progress, True

        # Resolve forward-source names from the batch entity cache.
        # Telegram includes users/chats for forward sources in the same
        # GetHistory response, so get_entity() hits the local cache — no
        # extra API round-trips in the common case.
        fwd_peer_ids: set[int] = set()
        for msg in batch:
            fwd = getattr(msg, "fwd_from", None)
            if fwd and getattr(fwd, "from_name", None) is None:
                from_id = getattr(fwd, "from_id", None)
                if from_id is not None:
                    for attr in ("user_id", "channel_id", "chat_id"):
                        pid = getattr(from_id, attr, None)
                        if pid is not None:
                            fwd_peer_ids.add(int(pid))
                            break
        entity_name_map: dict[int, str] = {}
        for peer_id in fwd_peer_ids:
            name = await _resolve_peer_name(self._client, peer_id)
            if name:
                entity_name_map[peer_id] = name

        rows = [extract_message_row(dialog_id, msg, entity_name_map=entity_name_map) for msg in batch]
        new_progress = min(int(getattr(msg, "id", 0)) for msg in batch)
        is_done = len(batch) < 100  # partial batch = last batch
        new_status = "synced" if is_done else "syncing"

        # Single atomic transaction: messages + FTS + progress update
        with self._conn:
            insert_messages_with_fts(self._conn, rows)
            if is_done:
                now = int(time.time())
                self._conn.execute(
                    _UPDATE_PROGRESS_DONE_SQL,
                    (new_progress, new_status, total_messages, now, dialog_id),
                )
            else:
                self._conn.execute(
                    _UPDATE_PROGRESS_SQL,
                    (new_progress, new_status, total_messages, dialog_id),
                )

        logger.debug(
            "sync_batch dialog_id=%d fetched=%d progress=%d done=%s",
            dialog_id,
            len(batch),
            new_progress,
            is_done,
        )
        if is_done:
            logger.info("sync_done dialog_id=%d status=synced total_messages=%d", dialog_id, total_messages)
        return new_progress, is_done
