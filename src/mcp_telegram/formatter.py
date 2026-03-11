from __future__ import annotations

from zoneinfo import ZoneInfo

SESSION_BREAK_MINUTES = 60


def format_messages(
    messages: list,
    reply_map: dict[int, object],
    reaction_names_map: dict[int, dict[str, list[str]]] | None = None,
    tz: ZoneInfo | None = None,
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

        lines.append(f"{dt.strftime('%H:%M')} {sender_name}: {reply_prefix}{text}")

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
    """Return a human-readable description of a media attachment.

    Phase 1: basic type detection via isinstance against Telethon types when
    available; falls back to '[медиа]' for unknown types. Telethon is duck-typed
    here — no import at module level.
    """
    try:
        # Lazy import so formatter has no hard dependency on Telethon at import time
        import telethon.tl.types as tl  # noqa: PLC0415

        if isinstance(media, tl.MessageMediaPhoto):
            return "[фото]"
        if isinstance(media, tl.MessageMediaDocument):
            doc = getattr(media, "document", None)
            if doc is not None:
                attrs = getattr(doc, "attributes", [])
                for attr in attrs:
                    # MessageMediaVoice / MessageMediaAudio
                    if hasattr(attr, "duration"):
                        dur = attr.duration
                        minutes, seconds = divmod(int(dur), 60)
                        return f"[голосовое: {minutes}:{seconds:02d}]"
                # Generic document — try to find filename
                for attr in attrs:
                    if hasattr(attr, "file_name") and attr.file_name:
                        size = getattr(doc, "size", None)
                        size_str = f", {size // 1024}KB" if size else ""
                        return f"[документ: {attr.file_name}{size_str}]"
            return "[документ]"
    except ImportError:
        pass

    return "[медиа]"
