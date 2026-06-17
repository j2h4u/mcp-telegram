import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, TypedDict, Unpack, cast
from zoneinfo import ZoneInfo

import telethon.tl.types as tl  # type: ignore[import-untyped]

from .models import DialogType, LinePrefixGetter, ReadMessage, ReadState, TopicNameGetter

logger = logging.getLogger(__name__)

# Messages separated by more than this gap get a visual session break marker.
# 60 min balances readability (avoids clutter in active chats) with context
# (flags meaningful pauses in conversation flow).
SESSION_BREAK_MINUTES = 60
SECONDS_PER_MINUTE = 60
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
DAYS_PER_WEEK = 7

# Label for outgoing DM messages. Bracketed form signals "role marker" to LLMs
# (ChatML/OpenAI convention: [user], [assistant], [system]), avoiding the
# first-person ambiguity of a bare "Я" when the model later paraphrases the log.
SELF_SENDER_LABEL = "[me]"
TELEGRAM_CONTENT_OPEN = "[Telegram content]"
TELEGRAM_CONTENT_CLOSE = "[/Telegram content]"


class _FormatMessagesKwargs(TypedDict, total=False):
    tz: ZoneInfo | None
    topic_name_getter: TopicNameGetter | None
    line_prefix_getter: LinePrefixGetter | None
    read_state: ReadState | dict | None
    dialog_type: str | None
    now_unix: int
    suppress_header: bool


@dataclass(slots=True)
class _MessageFormatOptions:
    tz: ZoneInfo
    topic_name_getter: TopicNameGetter | None
    line_prefix_getter: LinePrefixGetter | None
    read_state: ReadState | dict | None
    dialog_type: str | None
    now_unix: int
    suppress_header: bool


@dataclass(slots=True)
class _ReadStateSide:
    side_label: str
    direction_word: str
    read_word: str
    state: str | None
    count: int
    oldest: int | None


@dataclass(slots=True)
class _MessageRenderContext:
    effective_tz: ZoneInfo
    reply_map: dict[int, ReadMessage]
    topic_name_getter: TopicNameGetter | None
    line_prefix_getter: LinePrefixGetter | None


def frame_telegram_content(text: str) -> str:
    """Mark full Telegram-originated body text as untrusted content."""
    return f"{TELEGRAM_CONTENT_OPEN}\n{text}\n{TELEGRAM_CONTENT_CLOSE}"


def frame_telegram_snippet(text: str) -> str:
    """Mark compact one-line Telegram-originated snippets as untrusted content."""
    return f"{TELEGRAM_CONTENT_OPEN} {text} {TELEGRAM_CONTENT_CLOSE}"


# ---------------------------------------------------------------------------
# Phase 39.3: bidirectional read-state helpers
# ---------------------------------------------------------------------------


def _format_relative_delta(now_unix: int, then_unix: int) -> str:
    """Compact English delta: ``Xm``, ``Xh Ym``, ``Xd``, ``Xw`` (D-01).

    Rules:
    - delta < 1 hour     → ``{minutes}m``
    - delta < 1 day      → ``{hours}h {minutes}m``
    - delta < 7 days     → ``{days}d``
    - delta ≥ 7 days     → ``{weeks}w``

    Negative deltas (future timestamps — shouldn't happen) render as ``0m``.
    Clock skew between the daemon and the formatter (or server-side timestamps
    arriving slightly ahead of local ``time.time()``) is a known reality on
    Telegram; log at debug so the clamp isn't silent (IN-04).
    """
    delta_raw = int(now_unix) - int(then_unix)
    if delta_raw < 0:
        logger.debug(
            "read_state_header_negative_delta now=%d then=%d",
            int(now_unix),
            int(then_unix),
        )
    delta = max(0, delta_raw)
    minutes_total = delta // SECONDS_PER_MINUTE
    if minutes_total < MINUTES_PER_HOUR:
        return f"{minutes_total}m"
    hours_total = minutes_total // MINUTES_PER_HOUR
    if hours_total < HOURS_PER_DAY:
        return f"{hours_total}h {minutes_total % MINUTES_PER_HOUR}m"
    days_total = hours_total // HOURS_PER_DAY
    if days_total < DAYS_PER_WEEK:
        return f"{days_total}d"
    return f"{days_total // DAYS_PER_WEEK}w"


def _render_read_state_header(
    read_state: ReadState | dict | None,
    dialog_type: str | None,
    now_unix: int,
    tz: ZoneInfo | None = None,
) -> list[str]:
    """Return 0, 1, or 2 header lines for a DM read-state snapshot.

    Rules (AC-5/6/7, D-01..D-04):
    - read_state is None or dialog_type != 'User': return [] (AC-7).
    - Both sides cursor_state == 'populated' and both counts == 0:
      single '[read-state: all caught up]' (AC-5, D-02).
    - Otherwise: two lines, each side computed independently. Cursor='null'
      always renders 'unknown (sync pending)' — never 'all read' (D-03).
    """
    if read_state is None or DialogType.parse(dialog_type) != DialogType.USER:
        return []

    inbox_state = read_state.get("inbox_cursor_state")
    outbox_state = read_state.get("outbox_cursor_state")
    inbox_count = int(read_state.get("inbox_unread_count", 0) or 0)
    outbox_count = int(read_state.get("outbox_unread_count", 0) or 0)

    # Collapsed form: both sides populated AND both counts zero.
    if inbox_state == "populated" and outbox_state == "populated" and inbox_count == 0 and outbox_count == 0:
        return ["[read-state: all caught up]"]

    effective_tz = tz if tz is not None else ZoneInfo("UTC")
    inbox_line = _render_read_state_side(
        _ReadStateSide(
            side_label="inbox",
            direction_word="from peer",
            read_word="all read",
            state=inbox_state,
            count=inbox_count,
            oldest=read_state.get("inbox_oldest_unread_date"),
        ),
        now_unix=now_unix,
        effective_tz=effective_tz,
    )
    outbox_line = _render_read_state_side(
        _ReadStateSide(
            side_label="outbox",
            direction_word="by peer",
            read_word="all read by peer",
            state=outbox_state,
            count=outbox_count,
            oldest=read_state.get("outbox_oldest_unread_date"),
        ),
        now_unix=now_unix,
        effective_tz=effective_tz,
    )
    return [inbox_line, outbox_line]


def _render_read_state_side(
    side: _ReadStateSide,
    *,
    now_unix: int,
    effective_tz: ZoneInfo,
) -> str:
    if side.state == "null":
        return f"[{side.side_label}: unknown (sync pending)]"
    if side.count == 0:
        return f"[{side.side_label}: {side.read_word}]"
    if side.oldest is None:
        return f"[{side.side_label}: {side.count} unread {side.direction_word}]"
    dt_local = datetime.fromtimestamp(int(side.oldest), tz=UTC).astimezone(effective_tz)
    hh_mm = dt_local.strftime("%H:%M")
    delta = _format_relative_delta(now_unix, side.oldest)
    return f"[{side.side_label}: {side.count} unread {side.direction_word}, oldest {hh_mm} ({delta} ago)]"


def _compute_inline_markers(
    messages: list[ReadMessage],
    read_state: ReadState | dict | None,
) -> dict[int, str]:
    """Return {message_id: marker_text} for the four inline markers (AC-8/9/10).

    Marker placement is keyed by ``message.id`` (attribute), NOT by page-render
    order — ensuring stable behaviour regardless of ascending / descending
    iteration (codex MEDIUM).

    Per side (inbox=out=0 / outbox=out=1):
    - Boundary (last-seen): among page messages with ``message_id <= cursor``,
      pick the HIGHEST message_id; emit `[I read up to here]` / `[peer read up to here]`.
      NULL cursor or no qualifying message → no boundary marker (AC-10).
    - Tail-start (first-unseen): among page messages with (cursor IS NULL OR
      ``message_id > cursor``), pick the LOWEST message_id; emit `[unread by me]`
      / `[unread by peer]`. Recomputed per page (AC-9).
    """
    if read_state is None or not messages:
        return {}

    markers: dict[int, str] = {}

    def _side(
        out_flag: int, cursor_state: str | None, anchor: int | None, boundary_label: str, tail_label: str
    ) -> None:
        ids = [m.id for m in messages if m.out == out_flag]
        if not ids:
            return
        if cursor_state == "null":
            # Entire side is unread; tail-start on lowest id; no boundary.
            markers[min(ids)] = tail_label
            return
        if anchor is None:
            # Populated state but no anchor provided (e.g. all_read with count 0):
            # no markers — nothing unread here.
            return
        seen_ids = [i for i in ids if i <= anchor]
        unseen_ids = [i for i in ids if i > anchor]
        if seen_ids:
            markers[max(seen_ids)] = boundary_label
        if unseen_ids:
            markers[min(unseen_ids)] = tail_label

    _side(
        out_flag=0,
        cursor_state=read_state.get("inbox_cursor_state"),
        anchor=read_state.get("inbox_max_id_anchor"),
        boundary_label="[I read up to here]",
        tail_label="[unread by me]",
    )
    _side(
        out_flag=1,
        cursor_state=read_state.get("outbox_cursor_state"),
        anchor=read_state.get("outbox_max_id_anchor"),
        boundary_label="[peer read up to here]",
        tail_label="[unread by peer]",
    )
    return markers


def _resolve_message_format_options(kwargs: _FormatMessagesKwargs) -> _MessageFormatOptions:
    tz = kwargs.get("tz")
    now_unix = kwargs.get("now_unix")
    return _MessageFormatOptions(
        tz=tz if tz is not None else ZoneInfo("UTC"),
        topic_name_getter=kwargs.get("topic_name_getter"),
        line_prefix_getter=kwargs.get("line_prefix_getter"),
        read_state=kwargs.get("read_state"),
        dialog_type=kwargs.get("dialog_type"),
        now_unix=now_unix if now_unix is not None else int(datetime.now(tz=UTC).timestamp()),
        suppress_header=bool(kwargs.get("suppress_header", False)),
    )


def _format_message_body(msg: ReadMessage, effective_tz: ZoneInfo) -> str:
    text = frame_telegram_content(msg.text) if msg.text else _render_text(msg)
    if msg.edit_date is not None:
        ed_dt = datetime.fromtimestamp(msg.edit_date, tz=UTC).astimezone(effective_tz)
        text = f"{text} [edited {ed_dt.strftime('%H:%M')}]"
    reactions_str = _format_reactions(msg)
    if reactions_str:
        text = f"{text} {reactions_str}" if text else reactions_str
    return text


def _format_message_prefix(
    msg: ReadMessage,
    *,
    sender_name: str,
    context: _MessageRenderContext,
) -> str:
    author_prefix = f"[by {msg.post_author}] " if msg.post_author else ""
    fwd_prefix = f"[↪ fwd: {msg.fwd_from_name}] " if msg.fwd_from_name else ""
    reply_prefix = ""
    if msg.reply_to_msg_id and msg.reply_to_msg_id in context.reply_map:
        orig = context.reply_map[msg.reply_to_msg_id]
        orig_sender = _resolve_sender_name(orig)
        orig_dt = orig.date.astimezone(context.effective_tz)
        reply_prefix = f"[↑ {orig_sender} {orig_dt.strftime('%H:%M')}] "

    topic_prefix = ""
    if context.topic_name_getter is not None:
        topic_name = context.topic_name_getter(msg)
        if topic_name:
            topic_prefix = f"[topic: {topic_name}] "

    line_prefix = ""
    if context.line_prefix_getter is not None:
        resolved_prefix = context.line_prefix_getter(msg)
        if resolved_prefix:
            line_prefix = f"{resolved_prefix} "

    return (
        f"{line_prefix}{topic_prefix}{msg.date.astimezone(context.effective_tz).strftime('%H:%M')} "
        f"{sender_name}: {author_prefix}{fwd_prefix}{reply_prefix}"
    )


def _format_message_line(
    msg: ReadMessage,
    context: _MessageRenderContext,
    inline_marker: str | None,
) -> str:
    sender_name = _resolve_sender_name(msg)
    message_prefix = _format_message_prefix(
        msg,
        sender_name=sender_name,
        context=context,
    )
    text = _format_message_body(msg, context.effective_tz)
    line = f"{message_prefix.rstrip()}\n{text}" if msg.text else f"{message_prefix}{text}"
    if inline_marker:
        line = f"{line} {inline_marker}"
    return line


def format_messages(
    messages: list[ReadMessage],
    reply_map: dict[int, ReadMessage],
    **kwargs: Unpack[_FormatMessagesKwargs],
) -> str:
    """Format a list of ReadMessage objects into human-readable text.

    Output lines:
    - '--- YYYY-MM-DD ---'  on calendar day change
    - '--- N мин ---'       when gap between consecutive messages exceeds 60 min
    - 'HH:mm FirstName: text'  for each message

    Returns empty string for empty input.
    """
    if not messages:
        return ""

    options = _resolve_message_format_options(kwargs)

    # Phase 39.3: header + inline markers (only emit on DMs; AC-7).
    # WR-02: ``suppress_header`` lets callers (e.g. format_unread_messages_grouped)
    # emit the header themselves and reuse format_messages purely for the body,
    # avoiding fragile post-hoc header-dedup by string comparison.
    header_lines = (
        []
        if options.suppress_header
        else _render_read_state_header(options.read_state, options.dialog_type, options.now_unix, options.tz)
    )
    render_context = _MessageRenderContext(
        effective_tz=options.tz,
        reply_map=reply_map,
        topic_name_getter=options.topic_name_getter,
        line_prefix_getter=options.line_prefix_getter,
    )
    inline_markers = (
        _compute_inline_markers(messages, options.read_state)
        if DialogType.parse(options.dialog_type) == DialogType.USER
        else {}
    )

    lines: list[str] = list(header_lines)
    prev_date_str: str | None = None
    prev_dt: datetime | None = None

    for msg in reversed(messages):
        dt = msg.date.astimezone(options.tz)
        date_str = dt.strftime("%Y-%m-%d")

        if date_str != prev_date_str:
            lines.append(f"--- {date_str} ---")
            prev_date_str = date_str

        if prev_dt is not None:
            gap_seconds = (dt - prev_dt).total_seconds()
            gap_minutes = int(gap_seconds // 60)
            if gap_minutes > SESSION_BREAK_MINUTES:
                lines.append(f"--- {gap_minutes} мин ---")

        inline_marker = inline_markers.get(msg.id)
        lines.append(
            _format_message_line(
                msg,
                render_context,
                inline_marker=inline_marker,
            )
        )

        prev_dt = dt

    return "\n".join(lines)


def build_search_hit_window(
    hit: ReadMessage,
    *,
    context_messages_by_id: dict[int, ReadMessage],
    context_radius: int = 3,
) -> list[ReadMessage]:
    """Return one hit-local message window ordered for format_messages()."""
    hit_id = hit.id
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


def _hit_line_prefix_getter(hit_id: int) -> LinePrefixGetter:
    """Factory that binds ``hit_id`` so the returned closure is not tied to the loop
    variable (B023). Keeps type inference stable for mypy."""

    def _getter(message: ReadMessage) -> str | None:
        return "[HIT]" if message.id == hit_id else None

    return _getter


def format_search_message_groups(
    hits: list[ReadMessage],
    *,
    context_messages_by_id: dict[int, ReadMessage],
    context_radius: int = 3,
) -> str:
    """Return grouped search output with hit-local context and hit markers."""
    if not hits:
        return ""

    parts: list[str] = []
    total_hits = len(hits)

    for index, hit in enumerate(hits, start=1):
        group_text = format_messages(
            build_search_hit_window(
                hit,
                context_messages_by_id=context_messages_by_id,
                context_radius=context_radius,
            ),
            reply_map={},
            line_prefix_getter=_hit_line_prefix_getter(hit.id),
        )
        parts.append(f"--- hit {index}/{total_hits} ---\n{group_text}")

    return "\n\n".join(parts)


def resolve_sender_label(msg_or_row: object) -> str:
    """Return the sender label for a ReadMessage or a raw row dict.

    Five-branch decision:
      1. is_service == 1                             → "System"
      2. out == 1 AND dialog_id > 0 AND is_service=0 → SELF_SENDER_LABEL
      3. sender_first_name resolves (non-empty str)  → sender_first_name
      4. effective_sender_id OR sender_id known      → "(unknown user {id})"
      5. else                                        → "(unknown user)"

    Accepts ReadMessage objects and plain row dicts (used by search snippet
    formatting in reading.py which works with raw daemon response dicts).
    """
    if isinstance(msg_or_row, dict):
        get = msg_or_row.get
        is_service = int(get("is_service") or 0)
        out_flag = int(get("out") or 0)
        dialog_id = get("dialog_id") or 0
        first_name = get("sender_first_name")
        effective_sender_id = get("effective_sender_id")
        sender_id = get("sender_id")
    else:
        is_service = int(getattr(msg_or_row, "is_service", 0) or 0)
        out_flag = int(getattr(msg_or_row, "out", 0) or 0)
        dialog_id = getattr(msg_or_row, "dialog_id", 0) or 0
        first_name = getattr(msg_or_row, "sender_first_name", None)
        effective_sender_id = getattr(msg_or_row, "effective_sender_id", None)
        sender_id = getattr(msg_or_row, "sender_id", None)

    # Branch 1: service message wins regardless of other fields
    if is_service == 1:
        return "System"
    # Branch 2: DM outgoing → SELF_SENDER_LABEL (no self_id comparison needed at render)
    if out_flag == 1 and (dialog_id or 0) > 0:
        return SELF_SENDER_LABEL
    # Branch 3: known first_name
    if isinstance(first_name, str) and first_name:
        return first_name
    # Branch 4: id is known but name unresolved
    resolved_id = effective_sender_id if effective_sender_id is not None else sender_id
    if resolved_id is not None:
        return f"(unknown user {resolved_id})"
    # Branch 5: nothing to work with
    return "(unknown user)"


def _resolve_sender_name(msg: ReadMessage) -> str:
    return resolve_sender_label(msg)


def _render_text(msg: ReadMessage) -> str:
    """Return message text, or a pre-formatted media placeholder."""
    if msg.text:
        return msg.text
    return msg.media_description or ""


def _format_reactions(msg: ReadMessage) -> str:
    return msg.reactions_display


def format_reaction_counts(counts: list[tuple[str, int]]) -> str:
    """Format [(emoji, count), ...] as '[thumbsup×3 heart×1]'. Returns '' if empty.

    Used by daemon path where only aggregate counts are available
    (no reactor names). The on-demand Telethon path continues to use
    _format_reactions which can display reactor names when available.

    Counts are displayed in descending order (most reactions first),
    then by emoji Unicode code point for deterministic output when
    counts are tied. This sort is locked: ORDER BY count DESC, emoji ASC.

    IMPORTANT: Count is ALWAYS shown with × (U+00D7), including ×1.
    The locked display format from CONTEXT.md is [thumbsup×3 heart×1], NOT [thumbsup×3 heart].
    The emoji column stores actual Unicode glyphs from Telegram (e.g. 👍 not thumbs_up).
    """
    if not counts:
        return ""
    # Sort by count descending, then emoji ascending (Unicode code point)
    # for deterministic output. Addresses review Priority Action #5.
    sorted_counts = sorted(counts, key=lambda x: (-x[1], x[0]))
    parts = [f"{emoji}\u00d7{count}" for emoji, count in sorted_counts]
    return f"[{' '.join(parts)}]"


def _safe_attr_chain(obj: object, *attrs: str) -> object | None:
    """Traverse a chain of getattr calls, returning None if any link is missing."""
    for attr in attrs:
        if obj is None:
            return None
        obj = getattr(obj, attr, None)
    return obj


def _describe_poll(media: object) -> str:
    question = _safe_attr_chain(media, "poll", "question")
    if question is None:
        return "[опрос]"
    q_text = getattr(question, "text", None) or str(question)
    return f"[опрос: «{q_text}»]" if q_text else "[опрос]"


def _describe_geo(media: object) -> str:
    lat = _safe_attr_chain(media, "geo", "lat")
    lon = _safe_attr_chain(media, "geo", "long")
    if lat is not None and lon is not None:
        return f"[геолокация: {lat:.4f}, {lon:.4f}]"
    return "[геолокация]"


def _describe_venue(media: object) -> str:
    title = getattr(media, "title", None)
    address = getattr(media, "address", None)
    info = ", ".join(filter(None, [title, address]))
    return f"[место: {info}]" if info else "[место]"


def _describe_contact(media: object) -> str:
    first = getattr(media, "first_name", "") or ""
    last = getattr(media, "last_name", "") or ""
    name = " ".join(filter(None, [first, last]))
    phone = getattr(media, "phone_number", "") or ""
    info = ", ".join(filter(None, [name, phone]))
    return f"[контакт: {info}]" if info else "[контакт]"


def _describe_dice(media: object) -> str:
    emoticon = getattr(media, "emoticon", "🎲") or "🎲"
    value = getattr(media, "value", None)
    return f"[{emoticon} {value}]" if value is not None else f"[{emoticon}]"


def _describe_game(media: object) -> str:
    title = _safe_attr_chain(media, "game", "title")
    return f"[игра: {title}]" if title else "[игра]"


def _describe_invoice(media: object) -> str:
    title = getattr(media, "title", None)
    return f"[счёт: {title}]" if title else "[счёт]"


def _describe_web_page(media: object) -> str:
    url = _safe_attr_chain(media, "webpage", "url")
    return f"[ссылка: {url}]" if url else "[ссылка]"


def _describe_document_sticker(attr: object) -> str:
    alt = getattr(attr, "alt", "") or ""
    return f"[стикер: {alt}]" if alt else "[стикер]"


def _describe_document_round_video(attr: object) -> str:
    dur = getattr(attr, "duration", 0) or 0
    m, s = divmod(int(dur), 60)
    return f"[кружок: {m}:{s:02d}]"


def _describe_document_audio(attr: object) -> str:
    dur = getattr(attr, "duration", 0) or 0
    m, s = divmod(int(dur), 60)
    if getattr(attr, "voice", False):
        return f"[голосовое: {m}:{s:02d}]"
    title = getattr(attr, "title", None)
    performer = getattr(attr, "performer", None)
    info = " — ".join(filter(None, [performer, title]))
    return f"[аудио: {info}, {m}:{s:02d}]" if info else f"[аудио: {m}:{s:02d}]"


def _describe_document_video(attr: object) -> str:
    dur = getattr(attr, "duration", 0) or 0
    m, s = divmod(int(dur), 60)
    return f"[видео: {m}:{s:02d}]"


def _describe_document_filename(doc: object, attr: tl.DocumentAttributeFilename) -> str:
    size = getattr(doc, "size", None)
    size_str = f", {size // 1024}KB" if size else ""
    return f"[документ: {attr.file_name}{size_str}]"


class _DocumentLike(Protocol):
    attributes: Sequence[object]
    size: int | None


def _describe_media(media: object) -> str:
    """Return a human-readable placeholder for a media attachment.

    Covers all common Telegram media types explicitly; falls back to
    '[медиа: ClassName]' for unknown types so they are still distinguishable.
    """
    if isinstance(media, tl.MessageMediaEmpty):
        return ""

    if isinstance(media, tl.MessageMediaDocument):
        return _describe_document(media)

    handlers = (
        (tl.MessageMediaPhoto, lambda _: "[фото]"),
        (tl.MessageMediaPoll, _describe_poll),
        (tl.MessageMediaGeoLive, lambda _: "[геолокация live]"),
        (tl.MessageMediaGeo, _describe_geo),
        (tl.MessageMediaVenue, _describe_venue),
        (tl.MessageMediaContact, _describe_contact),
        (tl.MessageMediaDice, _describe_dice),
        (tl.MessageMediaGame, _describe_game),
        (tl.MessageMediaStory, lambda _: "[история]"),
        (tl.MessageMediaInvoice, _describe_invoice),
        (tl.MessageMediaWebPage, _describe_web_page),
        (tl.MessageMediaUnsupported, lambda _: "[неподдерживаемый тип]"),
    )
    for media_type, handler in handlers:
        if isinstance(media, media_type):
            return handler(media)

    return f"[медиа: {type(media).__name__}]"


def _describe_document(media: object) -> str:
    """Describe a MessageMediaDocument by inspecting its attributes.

    Priority order: sticker > round video > animation > audio > regular video > filename.
    Sticker checked first because sticker packs can carry a duration attribute.
    """
    doc = cast(object | None, getattr(media, "document", None))
    if doc is None:
        return "[документ]"
    attrs = list(cast(Sequence[object], getattr(doc, "attributes", [])) or [])
    description = "[документ]"
    sticker_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeSticker)), None)
    round_video_attr = next(
        (
            attr
            for attr in attrs
            if isinstance(attr, tl.DocumentAttributeVideo) and cast(bool, getattr(attr, "round_message", False))
        ),
        None,
    )
    audio_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeAudio)), None)
    video_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeVideo)), None)
    filename_attr = next((attr for attr in attrs if isinstance(attr, tl.DocumentAttributeFilename)), None)

    if sticker_attr is not None:
        description = _describe_document_sticker(sticker_attr)
    elif round_video_attr is not None:
        description = _describe_document_round_video(round_video_attr)
    elif any(isinstance(attr, tl.DocumentAttributeAnimated) for attr in attrs):
        description = "[анимация]"
    elif audio_attr is not None:
        description = _describe_document_audio(audio_attr)
    elif video_attr is not None:
        description = _describe_document_video(video_attr)
    elif filename_attr is not None:
        description = _describe_document_filename(doc, filename_attr)

    return description


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
    *,
    read_state_per_dialog: dict[int, ReadState | dict] | None = None,
    dialog_type_per_dialog: dict[int, str] | None = None,
    now_unix: int | None = None,
) -> str:
    """Format unread messages grouped by chat.

    Messages in each chat are already trimmed to budget by the caller.
    Adds "[и ещё N]" when total_in_chat > len(messages).

    Phase 39.3 (HIGH-3): when ``read_state_per_dialog`` is provided, each
    DM block is preceded by its read-state header (collapsed or split per
    AC-5/6). Non-DM blocks emit no header (AC-7). Per-chat headers are
    injected via ``_render_read_state_header`` and per-message inline
    markers via ``_compute_inline_markers`` when format_messages is called
    with ``read_state`` / ``dialog_type`` kwargs.
    """
    if not chats:
        return ""

    # WR-01: Resolve tz once so header and message lines agree by construction,
    # not by coincidence — guards against future drift if format_messages'
    # default changes.
    effective_tz = tz if tz is not None else ZoneInfo("UTC")
    resolved_now = now_unix if now_unix is not None else int(datetime.now(tz=UTC).timestamp())

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

        # Phase 39.3: per-chat read-state header (AC-5/6/7, D-03).
        chat_read_state = read_state_per_dialog.get(chat.chat_id) if read_state_per_dialog else None
        chat_dialog_type = dialog_type_per_dialog.get(chat.chat_id) if dialog_type_per_dialog else None
        read_state_header = _render_read_state_header(chat_read_state, chat_dialog_type, resolved_now, effective_tz)
        if read_state_header:
            parts.extend(read_state_header)

        if chat.is_channel:
            continue

        if chat.messages:
            formatted = format_messages(
                chat.messages,
                {},
                tz=effective_tz,
                read_state=chat_read_state,
                dialog_type=chat_dialog_type,
                now_unix=resolved_now,
                suppress_header=True,
            )
            if formatted:
                parts.append(formatted)

        shown = len(chat.messages)
        if shown < chat.total_in_chat:
            parts.append(f"[и ещё {chat.total_in_chat - shown}]")

    return "\n".join(parts)
