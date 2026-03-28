from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .models import LinePrefixGetter, MessageLike, TopicNameGetter

# Messages separated by more than this gap get a visual session break marker.
# 60 min balances readability (avoids clutter in active chats) with context
# (flags meaningful pauses in conversation flow).
SESSION_BREAK_MINUTES = 60


def format_messages(
    messages: list[MessageLike],
    reply_map: dict[int, MessageLike],
    reaction_names_map: dict[int, dict[str, list[str]]] | None = None,
    tz: ZoneInfo | None = None,
    topic_name_getter: TopicNameGetter | None = None,
    line_prefix_getter: LinePrefixGetter | None = None,
) -> str:
    """Format a list of messages into human-readable text.

    Output lines:
    - '--- YYYY-MM-DD ---'  on calendar day change
    - '--- N мин ---'       when gap between consecutive messages exceeds 60 min
    - 'HH:mm FirstName: text'  for each message

    Parameters
    ----------
    messages:
        Telethon Message objects, newest-first (as returned by iter_messages).
    reply_map:
        message_id → Message mapping for reply annotation lines.
    reaction_names_map:
        message_id → {emoji: [reactor_names]} for inline reaction display.
    tz:
        Timezone for display. Defaults to UTC.
    topic_name_getter:
        Callable(msg) → topic title or None; labels cross-topic forum pages.
    line_prefix_getter:
        Callable(msg) → prefix string or None; prepended to each message line.

    Returns empty string for empty input.
    """
    if not messages:
        return ""

    effective_tz = tz if tz is not None else ZoneInfo("UTC")

    lines: list[str] = []
    prev_date_str: str | None = None
    prev_dt = None

    for msg in reversed(messages):
        dt = msg.date.astimezone(effective_tz)
        date_str = dt.strftime("%Y-%m-%d")

        if date_str != prev_date_str:
            lines.append(f"--- {date_str} ---")
            prev_date_str = date_str

        if prev_dt is not None:
            gap_seconds = (dt - prev_dt).total_seconds()
            gap_minutes = int(gap_seconds // 60)
            if gap_minutes > SESSION_BREAK_MINUTES:
                lines.append(f"--- {gap_minutes} мин ---")

        sender_name = _resolve_sender_name(msg)
        text = _render_text(msg)
        edit_date_raw = getattr(msg, "edit_date", None)
        if edit_date_raw is not None:
            if isinstance(edit_date_raw, datetime):
                ed_dt = edit_date_raw.astimezone(effective_tz)
            else:
                ed_dt = datetime.fromtimestamp(int(edit_date_raw), tz=timezone.utc).astimezone(effective_tz)
            text = f"{text} [edited {ed_dt.strftime('%H:%M')}]"
        reaction_names = reaction_names_map.get(msg.id) if reaction_names_map else None
        reactions_str = _format_reactions(msg, reaction_names)
        if reactions_str:
            text = f"{text} {reactions_str}" if text else reactions_str

        reply_prefix = ""
        reply_to = getattr(msg, "reply_to", None)
        if reply_to:
            reply_id = getattr(reply_to, "reply_to_msg_id", None)
            if reply_id and reply_id in reply_map:
                orig = reply_map[reply_id]
                orig_sender = _resolve_sender_name(orig)
                orig_dt = orig.date.astimezone(effective_tz)
                reply_prefix = f"[↑ {orig_sender} {orig_dt.strftime('%H:%M')}] "

        topic_prefix = ""
        if topic_name_getter is not None:
            topic_name = topic_name_getter(msg)
            if topic_name:
                topic_prefix = f"[topic: {topic_name}] "

        line_prefix = ""
        if line_prefix_getter is not None:
            resolved_prefix = line_prefix_getter(msg)
            if resolved_prefix:
                line_prefix = f"{resolved_prefix} "

        lines.append(
            f"{line_prefix}{topic_prefix}{dt.strftime('%H:%M')} {sender_name}: {reply_prefix}{text}"
        )

        prev_dt = dt

    return "\n".join(lines)


def build_search_hit_window(
    hit: MessageLike,
    *,
    context_messages_by_id: dict[int, MessageLike],
    context_radius: int = 3,
) -> list[MessageLike]:
    """Return one hit-local message window ordered for format_messages()."""
    hit_id = getattr(hit, "id", None)
    if not isinstance(hit_id, int):
        return [hit]

    before = [
        context_messages_by_id[hit_id - offset]
        for offset in range(context_radius, 0, -1)
        if (hit_id - offset) in context_messages_by_id
    ]
    after = [
        context_messages_by_id[hit_id + offset]
        for offset in range(1, context_radius + 1)
        if (hit_id + offset) in context_messages_by_id
    ]
    return sorted([*before, hit, *after], key=lambda message: message.id, reverse=True)


def format_search_message_groups(
    hits: list[MessageLike],
    *,
    context_messages_by_id: dict[int, MessageLike],
    reaction_names_map: dict[int, dict[str, list[str]]] | None = None,
    context_radius: int = 3,
) -> str:
    """Return grouped search output with hit-local context and hit markers."""
    if not hits:
        return ""

    parts: list[str] = []
    total_hits = len(hits)

    for index, hit in enumerate(hits, start=1):
        hit_id = getattr(hit, "id", None)
        group_text = format_messages(
            build_search_hit_window(
                hit,
                context_messages_by_id=context_messages_by_id,
                context_radius=context_radius,
            ),
            reply_map={},
            reaction_names_map=reaction_names_map,
            line_prefix_getter=(
                (lambda message: "[HIT]" if getattr(message, "id", None) == hit_id else None)
                if isinstance(hit_id, int)
                else None
            ),
        )
        parts.append(f"--- hit {index}/{total_hits} ---\n{group_text}")

    return "\n\n".join(parts)


def _resolve_sender_name(msg: MessageLike) -> str:
    """Return the sender's first name, or 'Unknown' if not available."""
    sender = getattr(msg, "sender", None)
    if sender is None:
        return "Unknown"
    first_name = getattr(sender, "first_name", None)
    if not first_name:
        return "Unknown"
    return first_name


def _render_text(msg: MessageLike) -> str:
    """Return message text, or a media placeholder for media-only messages."""
    media = getattr(msg, "media", None)
    text = getattr(msg, "message", "") or ""
    if text:
        return text
    if media is not None:
        return _describe_media(media)
    return ""


def _format_reactions(msg: MessageLike, reaction_names: dict[str, list[str]] | None = None) -> str:
    """Return formatted reactions string like '[👍×3: Alice, Bob ❤️: Carol]', or empty string."""
    reactions = getattr(msg, "reactions", None)
    if reactions is None:
        return ""
    results = getattr(reactions, "results", None) or []
    parts: list[str] = []
    for r in results:
        reaction = getattr(r, "reaction", None)
        count = getattr(r, "count", 0)
        emoji = getattr(reaction, "emoticon", None) or str(reaction)
        names = reaction_names.get(emoji) if reaction_names else None
        if names:
            names_str = ", ".join(names)
            if count > len(names):
                parts.append(f"{emoji}×{count}: {names_str}…")
            elif count > 1:
                parts.append(f"{emoji}×{count}: {names_str}")
            else:
                parts.append(f"{emoji}: {names_str}")
        elif count > 1:
            parts.append(f"{emoji}×{count}")
        else:
            parts.append(emoji)
    return f"[{' '.join(parts)}]" if parts else ""


def _safe_attr_chain(obj: object, *attrs: str) -> object | None:
    """Traverse a chain of getattr calls, returning None if any link is missing."""
    for attr in attrs:
        if obj is None:
            return None
        obj = getattr(obj, attr, None)
    return obj


def _describe_media(media: object) -> str:
    """Return a human-readable placeholder for a media attachment.

    Covers all common Telegram media types explicitly; falls back to
    '[медиа: ClassName]' for unknown types so they are still distinguishable.
    """
    try:
        import telethon.tl.types as tl  # type: ignore[import-untyped]  # noqa: PLC0415

        if isinstance(media, tl.MessageMediaEmpty):
            return ""

        if isinstance(media, tl.MessageMediaPhoto):
            return "[фото]"

        if isinstance(media, tl.MessageMediaDocument):
            return _describe_document(media)

        if isinstance(media, tl.MessageMediaPoll):
            question = _safe_attr_chain(media, "poll", "question")
            q_text = (
                getattr(question, "text", None) or str(question)
                if question is not None else None
            )
            return f"[опрос: «{q_text}»]" if q_text else "[опрос]"

        if isinstance(media, tl.MessageMediaGeoLive):
            return "[геолокация live]"

        if isinstance(media, tl.MessageMediaGeo):
            lat = _safe_attr_chain(media, "geo", "lat")
            lon = _safe_attr_chain(media, "geo", "long")
            if lat is not None and lon is not None:
                return f"[геолокация: {lat:.4f}, {lon:.4f}]"
            return "[геолокация]"

        if isinstance(media, tl.MessageMediaVenue):
            title = getattr(media, "title", None)
            address = getattr(media, "address", None)
            info = ", ".join(filter(None, [title, address]))
            return f"[место: {info}]" if info else "[место]"

        if isinstance(media, tl.MessageMediaContact):
            first = getattr(media, "first_name", "") or ""
            last = getattr(media, "last_name", "") or ""
            name = " ".join(filter(None, [first, last]))
            phone = getattr(media, "phone_number", "") or ""
            info = ", ".join(filter(None, [name, phone]))
            return f"[контакт: {info}]" if info else "[контакт]"

        if isinstance(media, tl.MessageMediaDice):
            emoticon = getattr(media, "emoticon", "🎲") or "🎲"
            value = getattr(media, "value", None)
            return f"[{emoticon} {value}]" if value is not None else f"[{emoticon}]"

        if isinstance(media, tl.MessageMediaGame):
            title = _safe_attr_chain(media, "game", "title")
            return f"[игра: {title}]" if title else "[игра]"

        if isinstance(media, tl.MessageMediaStory):
            return "[история]"

        if isinstance(media, tl.MessageMediaInvoice):
            title = getattr(media, "title", None)
            return f"[счёт: {title}]" if title else "[счёт]"

        if isinstance(media, tl.MessageMediaWebPage):
            url = _safe_attr_chain(media, "webpage", "url")
            return f"[ссылка: {url}]" if url else "[ссылка]"

        if isinstance(media, tl.MessageMediaUnsupported):
            return "[неподдерживаемый тип]"

    except ImportError:
        pass

    return f"[медиа: {type(media).__name__}]"


def _describe_document(media: object) -> str:
    """Describe a MessageMediaDocument by inspecting its attributes.

    Priority order: sticker > round video > animation > audio > regular video > filename.
    Sticker checked first because sticker packs can carry a duration attribute.
    """
    try:
        import telethon.tl.types as tl  # type: ignore[import-untyped]  # noqa: PLC0415

        doc = getattr(media, "document", None)
        if doc is None:
            return "[документ]"
        attrs = getattr(doc, "attributes", []) or []

        has_animated = False
        video_attr = None
        audio_attr = None
        filename_attr = None

        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeSticker):
                alt = getattr(attr, "alt", "") or ""
                return f"[стикер: {alt}]" if alt else "[стикер]"
            if isinstance(attr, tl.DocumentAttributeVideo):
                if getattr(attr, "round_message", False):
                    dur = getattr(attr, "duration", 0) or 0
                    m, s = divmod(int(dur), 60)
                    return f"[кружок: {m}:{s:02d}]"
                video_attr = attr
            elif isinstance(attr, tl.DocumentAttributeAnimated):
                has_animated = True
            elif isinstance(attr, tl.DocumentAttributeAudio):
                audio_attr = attr
            elif isinstance(attr, tl.DocumentAttributeFilename):
                filename_attr = attr

        if has_animated:
            return "[анимация]"

        if audio_attr is not None:
            dur = getattr(audio_attr, "duration", 0) or 0
            m, s = divmod(int(dur), 60)
            if getattr(audio_attr, "voice", False):
                return f"[голосовое: {m}:{s:02d}]"
            title = getattr(audio_attr, "title", None)
            performer = getattr(audio_attr, "performer", None)
            info = " — ".join(filter(None, [performer, title]))
            return f"[аудио: {info}, {m}:{s:02d}]" if info else f"[аудио: {m}:{s:02d}]"

        if video_attr is not None:
            dur = getattr(video_attr, "duration", 0) or 0
            m, s = divmod(int(dur), 60)
            return f"[видео: {m}:{s:02d}]"

        if filename_attr is not None:
            size = getattr(doc, "size", None)
            size_str = f", {size // 1024}KB" if size else ""
            return f"[документ: {filename_attr.file_name}{size_str}]"

    except ImportError:
        pass

    return "[документ]"


# ---------------------------------------------------------------------------
# Unread messages grouped by chat
# ---------------------------------------------------------------------------


@dataclass
class UnreadChatData:
    """One chat's unread data for format_unread_messages_grouped()."""

    chat_id: int
    display_name: str
    unread_count: int
    unread_mentions_count: int = 0
    messages: list = field(default_factory=list)
    total_in_chat: int = 0
    is_channel: bool = False
    is_bot: bool = False


def format_unread_messages_grouped(
    chats: list[UnreadChatData],
    tz: ZoneInfo | None = None,
) -> str:
    """Format unread messages grouped by chat.

    Messages in each chat are already trimmed to budget by the caller.
    Adds "[и ещё N]" when total_in_chat > len(messages).
    """
    if not chats:
        return ""

    parts: list[str] = []

    for chat in chats:
        header_parts: list[str] = []
        if chat.is_bot:
            header_parts.append("бот")
        header_parts.append(f"{chat.unread_count} непрочитанных")
        if chat.unread_mentions_count > 0:
            n = chat.unread_mentions_count
            word = "упоминание" if n == 1 else "упоминания" if n % 10 in (2, 3, 4) else "упоминаний"
            header_parts.append(f"{n} {word}")
        header_parts.append(f"id={chat.chat_id}")
        parts.append(f"--- {chat.display_name} ({', '.join(header_parts)}) ---")

        if chat.is_channel:
            continue

        if chat.messages:
            formatted = format_messages(chat.messages, {}, tz=tz)
            if formatted:
                parts.append(formatted)

        shown = len(chat.messages)
        if shown < chat.total_in_chat:
            parts.append(f"[и ещё {chat.total_in_chat - shown}]")

    return "\n".join(parts)
