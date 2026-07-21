"""Telethon-to-message-contract extraction adapter."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, TypeVar, cast

from telethon import utils as tl_utils  # type: ignore[import-untyped]
from telethon.errors import (  # type: ignore[import-untyped]
    ChannelPrivateError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    PeerIdInvalidError,
    UserDeactivatedBanError,
    UserDeactivatedError,
    UserPrivacyRestrictedError,
)

from .. import message_contracts as _message_contracts
from ..telethon_media import describe_media
from ..telethon_message import is_service_message

logger = logging.getLogger(__name__)
T = TypeVar("T")


class PeerNameClient(Protocol):
    """Minimal Telegram client port used to resolve forwarded peer names."""

    async def get_entity(self, peer: object) -> object: ...


class _PeerLike(Protocol):
    channel_id: int | None
    chat_id: int | None
    user_id: int | None


class _ReactionKindLike(Protocol):
    emoticon: str | None


class _ReactionItemLike(Protocol):
    reaction: _ReactionKindLike | None
    count: int


class _ReactionsLike(Protocol):
    results: Sequence[_ReactionItemLike]


class _ReplyToLike(Protocol):
    reply_to_msg_id: int | None
    forum_topic: bool
    reply_to_reply_top_id: int | None
    reply_to_peer_id: _PeerLike | None


class _RepliesLike(Protocol):
    replies: int | None


class _ForwardLike(Protocol):
    from_name: str | None
    from_id: _PeerLike | None
    date: datetime | None
    channel_post: int | None


class _EntityLike(Protocol):
    id: int
    title: str | None
    first_name: str | None
    last_name: str | None
    username: str | None
    access_hash: int | None
    bot: bool
    broadcast: bool
    date: datetime | None


class _DraftLike(Protocol):
    message: str | None


class _MessageLike(Protocol):
    id: int
    date: datetime | None
    message: str | None
    sender_id: int | None
    sender: _EntityLike | None
    edit_date: datetime | None
    grouped_id: int | None
    reply_to: _ReplyToLike | None
    message_thread_id: int | None
    is_topic_message: bool
    out: bool
    post_author: str | None
    replies: _RepliesLike | None
    reactions: _ReactionsLike | None
    entities: Sequence[object] | None
    fwd_from: _ForwardLike | None
    media: object | None
    rich_message: object | None
    action: object | None


def _attr[T](obj: object, name: str, default: T) -> T:
    return cast(T, getattr(obj, name, default))


def _first_non_empty_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value != "":
            return value
    return None


def extract_reply_and_topic(msg: object) -> tuple[int | None, int | None]:
    """Extract reply_to_msg_id and forum_topic_id from a Telethon message.

    Shared between extract_message_row (sync path) and _msg_to_dict (API path)
    to avoid duplicating the forum_topic / reply_to_reply_top_id branching.

    Returns (reply_to_msg_id, forum_topic_id).
    """
    reply_to = _attr(msg, "reply_to", None)
    return _reply_message_id(reply_to), _reply_forum_topic_id(reply_to) or _message_thread_topic_id(msg)


def _reply_message_id(reply_to: object | None) -> int | None:
    if reply_to is None:
        return None
    _touch_reply_to_fields(cast(_ReplyToLike, reply_to))
    raw_reply_msg_id = _attr(reply_to, "reply_to_msg_id", None)
    return int(raw_reply_msg_id) if raw_reply_msg_id is not None else None


def _reply_forum_topic_id(reply_to: object | None) -> int | None:
    if reply_to is None or not _attr(reply_to, "forum_topic", False):
        return None
    reply_top_id = _attr(reply_to, "reply_to_reply_top_id", None)
    return int(reply_top_id) if reply_top_id is not None else 1


def _message_thread_topic_id(msg: object) -> int | None:
    message_thread_id = _attr(msg, "message_thread_id", None)
    if message_thread_id is not None:
        return int(message_thread_id)
    if _attr(msg, "is_topic_message", False):
        # Bot API-style topic messages may surface only a topic flag when the
        # thread id is absent. Telegram uses topic id 1 for the General topic.
        return 1
    return None


def _touch_reply_to_fields(reply_to: _ReplyToLike) -> None:
    if hasattr(reply_to, "forum_topic"):
        _ = reply_to.forum_topic
    if hasattr(reply_to, "reply_to_reply_top_id"):
        _ = reply_to.reply_to_reply_top_id


def _touch_forward_fields(fwd: _ForwardLike) -> None:
    if hasattr(fwd, "channel_post"):
        _ = fwd.channel_post


def _touch_message_fields(msg: _MessageLike) -> None:
    if hasattr(msg, "grouped_id"):
        _ = msg.grouped_id
    if hasattr(msg, "reply_to"):
        _ = msg.reply_to
    if hasattr(msg, "fwd_from"):
        _ = msg.fwd_from


def extract_reply_count(msg: object) -> int:
    """Extract Telegram's aggregate reply/comment count from a message."""
    replies = _attr(msg, "replies", None)
    if replies is None:
        return 0
    raw_count = _attr(replies, "replies", None)
    if isinstance(raw_count, int) and not isinstance(raw_count, bool):
        return max(0, raw_count)
    return 0


def extract_reactions_rows(
    dialog_id: int,
    message_id: int,
    reactions: object | None,
) -> list[_message_contracts.ReactionRecord]:
    """Extract reaction rows from a Telethon MessageReactions object.

    Returns empty list if reactions is None or has no results.
    """
    if reactions is None:
        return []
    results = cast(Sequence[object], _attr(reactions, "results", ()))
    if not results:
        return []
    rows: list[_message_contracts.ReactionRecord] = []
    for item in results:
        reaction = _attr(item, "reaction", None)
        emoticon = _attr(reaction, "emoticon", None) if reaction is not None else None
        count = _attr(item, "count", 0)
        if emoticon is not None:
            record = _message_contracts.ReactionRecord(
                dialog_id=dialog_id,
                message_id=message_id,
                emoji=emoticon,
                count=int(count),
            )
            rows.append(record)
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
    except UnicodeDecodeError, IndexError:
        return None


def _analytics_entity_type(entity: object) -> str | None:
    """Return the analytics entity type name for a Telethon entity."""
    for cls, type_name in _ANALYTICS_ENTITY_TYPES.items():
        if isinstance(entity, cls):
            return type_name
    return None


def _extract_entity_value(
    entity_type: str, text: str, entity: object, offset: int, length: int
) -> tuple[bool, str | None]:
    """Return (should_keep_row, value) for an analytics entity."""
    if entity_type in {"mention", "hashtag", "url"}:
        if not text:
            return True, None
        value = _utf16_slice(text, offset, length)
        return value is not None, value
    if entity_type == "mention_name":
        return True, str(_attr(entity, "user_id", ""))
    if entity_type == "text_url":
        return True, _attr(entity, "url", None)
    return False, None


def extract_entity_rows(dialog_id: int, message_id: int, msg: object) -> list[_message_contracts.EntityRecord]:
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
    entities: Sequence[object] | None = _attr(msg, "entities", None)
    if not entities:
        return []
    _init_entity_types()
    if not _ANALYTICS_ENTITY_TYPES:
        return []  # Telethon not available (test env)
    text = _attr(msg, "message", "") or ""
    rows: list[_message_contracts.EntityRecord] = []
    for entity in entities:
        entity_type = _analytics_entity_type(entity)
        if entity_type is None:
            continue
        offset = _attr(entity, "offset", 0)
        length = _attr(entity, "length", 0)
        should_keep_row, value = _extract_entity_value(entity_type, text, entity, offset, length)
        if not should_keep_row:
            continue
        rows.append(
            _message_contracts.EntityRecord(
                dialog_id=dialog_id,
                message_id=message_id,
                offset=offset,
                length=length,
                type=entity_type,
                value=value,
            )
        )
    return rows


def _marked_peer_id(from_id: object) -> int | None:
    """Marked id from a Telethon Peer (PeerUser/PeerChannel/PeerChat).

    Returns the *marked* id (e.g. -1001579759981 for a channel, -id for a legacy
    chat, +id for a user) — the same convention as ``dialogs.dialog_id`` /
    ``entities.id``, so the stored value is JOINable across the schema. Mirrors
    ``telethon.utils.get_peer_id`` but is duck-typed: it reads channel_id/chat_id/
    user_id attributes directly, so it also works on test doubles.

    NOTE: the marked id still encodes the kind, but for entity *resolution* pass
    the original Peer object, not this int (see _resolve_peer_name): get_entity
    treats a bare int as a user_id regardless of sign-stripping.
    """
    channel_id = _attr(from_id, "channel_id", None)
    if channel_id is not None:
        return -1000000000000 - int(channel_id)
    chat_id = _attr(from_id, "chat_id", None)
    if chat_id is not None:
        return -int(chat_id)
    user_id = _attr(from_id, "user_id", None)
    if user_id is not None:
        return int(user_id)
    return None


async def _resolve_peer_name(client: PeerNameClient, peer: _PeerLike) -> str | None:
    """Return display name for a Telegram peer.

    `peer` must be a Telethon Peer (PeerUser/PeerChannel/PeerChat), NOT a bare
    int: get_entity treats a bare positive id as a user_id, so a channel/chat id
    would be mis-resolved as a non-existent user and raise. Passing the typed
    Peer preserves the entity kind so the session cache lookup succeeds.

    Tries Telethon's session cache first; falls back to an API call when the
    entity is not cached. Returns None when the peer is permanently inaccessible
    (private/deleted/banned account, unknown ID).
    """
    log_id = tl_utils.get_peer_id(peer)
    try:
        entity = await client.get_entity(peer=peer)
        title = _attr(entity, "title", None)
        name = title or _attr(entity, "first_name", None) or ""
        last = _attr(entity, "last_name", None)
        if last:
            name = f"{name} {last}".strip()
        return name or None
    except FloodWaitError as e:
        logger.warning("resolve_peer_name_flood_wait peer_id=%d retry_after=%ds", log_id, e.seconds)
        return None
    except PeerFloodError:
        logger.warning("resolve_peer_name_peer_flood peer_id=%d", log_id)
        return None
    except (
        ChannelPrivateError,
        InputUserDeactivatedError,
        PeerIdInvalidError,
        UserDeactivatedBanError,
        UserDeactivatedError,
        UserPrivacyRestrictedError,
    ):
        logger.debug("resolve_peer_name_inaccessible peer_id=%d", log_id)
        return None
    except ValueError:
        # Telethon could not build an InputPeer: the typed peer is not in the
        # session cache and carries no usable access_hash (a stranger whose User
        # object never arrived in a processed response). The request never
        # reaches Telegram, so there is no finer server-side reason. Expected for
        # forwards from un-cached authors — record the fact, no stacktrace.
        logger.warning("resolve_peer_name_uncacheable peer_id=%d", log_id)
        return None
    except Exception:
        logger.warning("resolve_peer_name_unexpected peer_id=%d", log_id, exc_info=True)
        return None


async def _build_fwd_entity_map(msg: object, client: PeerNameClient) -> dict[int, str]:
    """Return {peer_id: name} for the forward source of a single message.

    Returns an empty dict when the message is not a forward, already has
    fwd_from.from_name, or the peer cannot be resolved.
    """
    fwd = _attr(msg, "fwd_from", None)
    if not fwd or fwd.from_name is not None:
        return {}
    from_id = fwd.from_id
    if from_id is None:
        return {}
    peer_id = _marked_peer_id(from_id)
    if peer_id is None:
        return {}
    # Resolve with the typed Peer (preserves channel/chat kind); key by marked id.
    name = await _resolve_peer_name(client, from_id)
    return {peer_id: name} if name else {}


def _extract_sent_at(msg: object) -> int:
    date = _attr(msg, "date", None)
    return int(date.timestamp()) if isinstance(date, datetime) else 0


def _extract_sender_first_name(msg: object) -> str | None:
    sender = _attr(msg, "sender", None)
    if sender is None:
        return None
    return _first_non_empty_str(_attr(sender, "first_name", None), _attr(sender, "title", None))


def _concrete_attr(obj: object, name: str) -> object | None:
    """Return an attribute only when it is already materialized on the object."""
    if type(obj).__module__.startswith("unittest.mock"):
        values = getattr(obj, "__dict__", {})
        return values.get(name) if isinstance(values, dict) else None
    try:
        values = vars(obj)
    except TypeError:
        return getattr(obj, name, None)
    return values.get(name)


def _extract_rich_text(value: object) -> str:
    """Best-effort plain-text extraction from Telegram rich-text objects."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return "".join(_extract_rich_text(item) for item in value)

    parts: list[str] = []
    for attr in ("text", "texts", "blocks", "items"):
        child = _concrete_attr(value, attr)
        if child is not None:
            parts.append(_extract_rich_text(child))
    return "".join(parts)


def _extract_message_text(msg: object) -> str | None:
    text = _attr(msg, "message", None)
    if isinstance(text, str) and text != "":
        return text
    rich_message = _concrete_attr(msg, "rich_message")
    if rich_message is None:
        return text if isinstance(text, str) else None
    rich_text = _extract_rich_text(rich_message).strip()
    return rich_text or (text if isinstance(text, str) else None)


def _extract_media_description(msg: object) -> str | None:
    media = _attr(msg, "media", None)
    if media is None:
        return None
    return describe_media(media)


def _extract_edit_date(msg: object) -> int | None:
    edit_date_raw = _attr(msg, "edit_date", None)
    return int(edit_date_raw.timestamp()) if edit_date_raw is not None else None


def _extract_grouped_id(msg: object) -> int | None:
    grouped_id_raw = _attr(msg, "grouped_id", None)
    return int(grouped_id_raw) if grouped_id_raw is not None else None


def _extract_reply_to_peer_id(msg: object) -> int | None:
    reply_to = _attr(msg, "reply_to", None)
    reply_to_peer_raw = _attr(reply_to, "reply_to_peer_id", None) if reply_to is not None else None
    if reply_to_peer_raw is None:
        return None
    for attr in ("user_id", "channel_id", "chat_id"):
        pid = _attr(reply_to_peer_raw, attr, None)
        if pid is not None:
            return int(pid)
    return None


def extract_fwd_row(
    dialog_id: int,
    message_id: int,
    msg: object,
    entity_name_map: dict[int, str] | None = None,
) -> _message_contracts.ForwardRecord | None:
    """Extract forward metadata from a Telethon message.

    entity_name_map is a {peer_id: name} dict built from the batch response
    before this call — used to populate fwd_from_name for public senders
    whose name lives in the batch's users/chats, not in fwd_from.from_name.

    Returns ForwardRecord or None if not a forward.
    """
    fwd = _attr(msg, "fwd_from", None)
    if fwd is None:
        return None
    _touch_forward_fields(cast(_ForwardLike, fwd))
    from_id = _attr(fwd, "from_id", None)
    fwd_from_peer_id = _marked_peer_id(from_id) if from_id is not None else None
    fwd_from_name = _attr(fwd, "from_name", None)
    if fwd_from_name is None and fwd_from_peer_id is not None and entity_name_map:
        fwd_from_name = entity_name_map.get(fwd_from_peer_id)
    fwd_date_raw = _attr(fwd, "date", None)
    fwd_date: int | None = None
    if fwd_date_raw is not None:
        try:
            fwd_date = int(fwd_date_raw.timestamp())
        except AttributeError, OverflowError, TypeError, ValueError:
            fwd_date = None
    fwd_channel_post = _attr(fwd, "channel_post", None)
    if fwd_channel_post is not None:
        fwd_channel_post = int(fwd_channel_post)
    return _message_contracts.ForwardRecord(
        dialog_id=dialog_id,
        message_id=message_id,
        fwd_from_peer_id=fwd_from_peer_id,
        fwd_from_name=fwd_from_name,
        fwd_date=fwd_date,
        fwd_channel_post=fwd_channel_post,
    )


def extract_message_row(
    dialog_id: int,
    msg: object,
    entity_name_map: dict[int, str] | None = None,
) -> _message_contracts.ExtractedMessage:
    """Extract sync.db row bundle from a Telethon message object.

    Returns an ExtractedMessage with a typed StoredMessage plus typed satellite
    records for atomic multi-table insert.
    """
    message_id = int(_attr(msg, "id", 0))
    _touch_message_fields(cast(_MessageLike, msg))

    reply_to_msg_id, forum_topic_id = extract_reply_and_topic(msg)

    stored = _message_contracts.StoredMessage(
        dialog_id=dialog_id,
        message_id=message_id,
        sent_at=_extract_sent_at(msg),
        text=_extract_message_text(msg),
        sender_id=_attr(msg, "sender_id", None),
        sender_first_name=_extract_sender_first_name(msg),
        media_description=_extract_media_description(msg),
        reply_to_msg_id=reply_to_msg_id,
        forum_topic_id=forum_topic_id,
        edit_date=_extract_edit_date(msg),
        grouped_id=_extract_grouped_id(msg),
        reply_to_peer_id=_extract_reply_to_peer_id(msg),
        out=1 if _attr(msg, "out", False) else 0,
        is_service=1 if is_service_message(msg) else 0,
        post_author=_attr(msg, "post_author", None),
    )
    reply_count = extract_reply_count(msg)
    reactions = extract_reactions_rows(dialog_id, message_id, _attr(msg, "reactions", None))
    entities = extract_entity_rows(dialog_id, message_id, msg)
    forward = extract_fwd_row(dialog_id, message_id, msg, entity_name_map=entity_name_map)

    return _message_contracts.ExtractedMessage(
        message=stored,
        reply_count=reply_count,
        reactions=reactions,
        entities=entities,
        forward=forward,
    )


async def build_forward_entity_name_map(message: object, client: PeerNameClient) -> dict[int, str]:
    """Resolve the forward-source name for one message, when needed."""

    return await _build_fwd_entity_map(message, client)


async def resolve_forward_entity_name_map(messages: Sequence[object], client: PeerNameClient) -> dict[int, str]:
    """Resolve reusable forward-source names for one fetched Telegram batch."""

    peers: dict[int, object] = {}
    for message in messages:
        fwd = _attr(message, "fwd_from", None)
        if fwd and _attr(fwd, "from_name", None) is None:
            from_id = _attr(fwd, "from_id", None)
            if from_id is not None:
                peer_id = _marked_peer_id(from_id)
                if peer_id is not None:
                    peers.setdefault(peer_id, from_id)
    names: dict[int, str] = {}
    for peer_id, peer in peers.items():
        name = await _resolve_peer_name(client, cast(_PeerLike, peer))
        if name:
            names[peer_id] = name
    return names
