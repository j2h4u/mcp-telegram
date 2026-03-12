from __future__ import annotations

import typing as t
from zoneinfo import ZoneInfo

SESSION_BREAK_MINUTES = 60


def format_messages(
    messages: list,
    reply_map: dict[int, object],
    reaction_names_map: dict[int, dict[str, list[str]]] | None = None,
    tz: ZoneInfo | None = None,
    topic_name_getter: t.Callable[[object], str | None] | None = None,
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
        message_id -> Message mapping for reply annotation (unused in Phase 1).
    tz:
        Timezone for display. Defaults to UTC.

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

        # Date header on day change
        if date_str != prev_date_str:
            lines.append(f"--- {date_str} ---")
            prev_date_str = date_str

        # Session-break line when gap exceeds threshold
        if prev_dt is not None:
            gap_seconds = (dt - prev_dt).total_seconds()
            gap_minutes = int(gap_seconds // 60)
            if gap_minutes > SESSION_BREAK_MINUTES:
                lines.append(f"--- {gap_minutes} мин ---")

        sender_name = _resolve_sender_name(msg)
        text = _render_text(msg)
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

        lines.append(f"{topic_prefix}{dt.strftime('%H:%M')} {sender_name}: {reply_prefix}{text}")

        prev_dt = dt

    return "\n".join(lines)


def _resolve_sender_name(msg: object) -> str:
    """Return the sender's first name, or 'Unknown' if not available."""
    sender = getattr(msg, "sender", None)
    if sender is None:
        return "Unknown"
    first_name = getattr(sender, "first_name", None)
    if not first_name:
        return "Unknown"
    return first_name


def _render_text(msg: object) -> str:
    """Return message text, or a media placeholder for media-only messages."""
    media = getattr(msg, "media", None)
    text = getattr(msg, "message", "") or ""
    if text:
        return text
    if media is not None:
        return _describe_media(media)
    return ""


def _format_reactions(msg: object, reaction_names: dict[str, list[str]] | None = None) -> str:
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


def _describe_media(media: object) -> str:
    """Return a human-readable placeholder for a media attachment.

    Covers all common Telegram media types explicitly; falls back to
    '[медиа: ClassName]' for unknown types so they are still distinguishable.
    """
    try:
        import telethon.tl.types as tl  # noqa: PLC0415

        if isinstance(media, tl.MessageMediaEmpty):
            return ""

        if isinstance(media, tl.MessageMediaPhoto):
            return "[фото]"

        if isinstance(media, tl.MessageMediaDocument):
            return _describe_document(media)

        if isinstance(media, tl.MessageMediaPoll):
            poll = getattr(media, "poll", None)
            question = getattr(poll, "question", None) if poll else None
            q_text = (
                getattr(question, "text", None) or str(question)
                if question is not None else None
            )
            return f"[опрос: «{q_text}»]" if q_text else "[опрос]"

        if isinstance(media, tl.MessageMediaGeoLive):
            return "[геолокация live]"

        if isinstance(media, tl.MessageMediaGeo):
            geo = getattr(media, "geo", None)
            lat = getattr(geo, "lat", None)
            lon = getattr(geo, "long", None)
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
            game = getattr(media, "game", None)
            title = getattr(game, "title", None) if game else None
            return f"[игра: {title}]" if title else "[игра]"

        if isinstance(media, tl.MessageMediaStory):
            return "[история]"

        if isinstance(media, tl.MessageMediaInvoice):
            title = getattr(media, "title", None)
            return f"[счёт: {title}]" if title else "[счёт]"

        if isinstance(media, tl.MessageMediaWebPage):
            webpage = getattr(media, "webpage", None)
            url = getattr(webpage, "url", None) if webpage else None
            return f"[ссылка: {url}]" if url else "[ссылка]"

        if isinstance(media, tl.MessageMediaUnsupported):
            return "[неподдерживаемый тип]"

    except ImportError:
        pass

    return f"[медиа: {type(media).__name__}]"


def _describe_document(media: object) -> str:
    """Describe a MessageMediaDocument by inspecting its attributes."""
    try:
        import telethon.tl.types as tl  # noqa: PLC0415

        doc = getattr(media, "document", None)
        if doc is None:
            return "[документ]"
        attrs = getattr(doc, "attributes", []) or []

        # Sticker (check before video/audio — sticker packs can have duration)
        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeSticker):
                alt = getattr(attr, "alt", "") or ""
                return f"[стикер: {alt}]" if alt else "[стикер]"

        # Round video (video note / circle message)
        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeVideo):
                if getattr(attr, "round_message", False):
                    dur = getattr(attr, "duration", 0) or 0
                    m, s = divmod(int(dur), 60)
                    return f"[кружок: {m}:{s:02d}]"

        # GIF / animation
        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeAnimated):
                return "[анимация]"

        # Voice message
        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeAudio):
                dur = getattr(attr, "duration", 0) or 0
                m, s = divmod(int(dur), 60)
                if getattr(attr, "voice", False):
                    return f"[голосовое: {m}:{s:02d}]"
                title = getattr(attr, "title", None)
                performer = getattr(attr, "performer", None)
                info = " — ".join(filter(None, [performer, title]))
                return f"[аудио: {info}, {m}:{s:02d}]" if info else f"[аудио: {m}:{s:02d}]"

        # Regular video
        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeVideo):
                dur = getattr(attr, "duration", 0) or 0
                m, s = divmod(int(dur), 60)
                return f"[видео: {m}:{s:02d}]"

        # Named file
        for attr in attrs:
            if isinstance(attr, tl.DocumentAttributeFilename):
                size = getattr(doc, "size", None)
                size_str = f", {size // 1024}KB" if size else ""
                return f"[документ: {attr.file_name}{size_str}]"

    except ImportError:
        pass

    return "[документ]"
