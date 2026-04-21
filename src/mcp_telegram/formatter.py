
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import telethon.tl.types as tl  # type: ignore[import-untyped]

from .models import LinePrefixGetter, MessageLike, ReadState, TopicNameGetter

# Messages separated by more than this gap get a visual session break marker.
# 60 min balances readability (avoids clutter in active chats) with context
# (flags meaningful pauses in conversation flow).
SESSION_BREAK_MINUTES = 60

# Label for outgoing DM messages. Bracketed form signals "role marker" to LLMs
# (ChatML/OpenAI convention: [user], [assistant], [system]), avoiding the
# first-person ambiguity of a bare "Я" when the model later paraphrases the log.
SELF_SENDER_LABEL = "[me]"


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
    """
    delta = max(0, int(now_unix) - int(then_unix))
    minutes_total = delta // 60
    if minutes_total < 60:
        return f"{minutes_total}m"
    hours_total = minutes_total // 60
    if hours_total < 24:
        return f"{hours_total}h {minutes_total % 60}m"
    days_total = hours_total // 24
    if days_total < 7:
        return f"{days_total}d"
    return f"{days_total // 7}w"


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
    if read_state is None or dialog_type != "User":
        return []

    inbox_state = read_state.get("inbox_cursor_state")
    outbox_state = read_state.get("outbox_cursor_state")
    inbox_count = int(read_state.get("inbox_unread_count", 0) or 0)
    outbox_count = int(read_state.get("outbox_unread_count", 0) or 0)

    # Collapsed form: both sides populated AND both counts zero.
    if (
        inbox_state == "populated"
        and outbox_state == "populated"
        and inbox_count == 0
        and outbox_count == 0
    ):
        return ["[read-state: all caught up]"]

    effective_tz = tz if tz is not None else ZoneInfo("UTC")

    def _side(
        *,
        side_label: str,     # "inbox" or "outbox"
        direction_word: str, # "from peer" or "by peer"
        read_word: str,      # "all read" or "all read by peer"
        state: str | None,
        count: int,
        oldest: int | None,
    ) -> str:
        if state == "null":
            return f"[{side_label}: unknown (sync pending)]"
        if count == 0:
            return f"[{side_label}: {read_word}]"
        # populated with count > 0
        if oldest is None:
            # Defensive: no timestamp but count > 0 — render without Δ
            return f"[{side_label}: {count} unread {direction_word}]"
        dt_local = datetime.fromtimestamp(int(oldest), tz=timezone.utc).astimezone(effective_tz)
        hh_mm = dt_local.strftime("%H:%M")
        delta = _format_relative_delta(now_unix, oldest)
        return f"[{side_label}: {count} unread {direction_word}, oldest {hh_mm} ({delta} ago)]"

    inbox_line = _side(
        side_label="inbox",
        direction_word="from peer",
        read_word="all read",
        state=inbox_state,
        count=inbox_count,
        oldest=read_state.get("inbox_oldest_unread_date"),
    )
    outbox_line = _side(
        side_label="outbox",
        direction_word="by peer",
        read_word="all read by peer",
        state=outbox_state,
        count=outbox_count,
        oldest=read_state.get("outbox_oldest_unread_date"),
    )
    return [inbox_line, outbox_line]


def _compute_inline_markers(
    messages: list,
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

    def _side(out_flag: int, cursor_state: str | None, anchor: int | None,
              boundary_label: str, tail_label: str) -> None:
        # Select ids for this side; guard against missing .id / .out.
        ids = [
            int(getattr(m, "id", 0) or 0)
            for m in messages
            if int(getattr(m, "out", 0) or 0) == out_flag
            and getattr(m, "id", None) is not None
        ]
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


def format_messages(
    messages: list[MessageLike],
    reply_map: dict[int, MessageLike],
    reaction_names_map: dict[int, dict[str, list[str]]] | None = None,
    tz: ZoneInfo | None = None,
    topic_name_getter: TopicNameGetter | None = None,
    line_prefix_getter: LinePrefixGetter | None = None,
    *,
    read_state: ReadState | dict | None = None,
    dialog_type: str | None = None,
    now_unix: int | None = None,
    suppress_header: bool = False,
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
        Should contain only messages from the current page — replies to
        messages outside the page produce no annotation.
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

    # Phase 39.3: header + inline markers (only emit on DMs; AC-7).
    # WR-02: ``suppress_header`` lets callers (e.g. format_unread_messages_grouped)
    # emit the header themselves and reuse format_messages purely for the body,
    # avoiding fragile post-hoc header-dedup by string comparison.
    resolved_now = now_unix if now_unix is not None else int(datetime.now(tz=timezone.utc).timestamp())
    header_lines = (
        []
        if suppress_header
        else _render_read_state_header(read_state, dialog_type, resolved_now, effective_tz)
    )
    inline_markers = (
        _compute_inline_markers(messages, read_state) if dialog_type == "User" else {}
    )

    lines: list[str] = list(header_lines)
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

        line = (
            f"{line_prefix}{topic_prefix}{dt.strftime('%H:%M')} {sender_name}: {reply_prefix}{text}"
        )
        # Phase 39.3: append inline marker for this message_id (D-06 — trailing).
        msg_id_attr = getattr(msg, "id", None)
        if inline_markers and msg_id_attr in inline_markers:
            line = f"{line} {inline_markers[msg_id_attr]}"
        lines.append(line)

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


def resolve_sender_label(msg_or_row: object) -> str:
    """Return the sender label for a message-like object or daemon row dict.

    Five-branch decision (Phase 39.1-02 contract; supersedes Phase 39 three-way):
      1. is_service == 1                           → "System"
      2. out == 1 AND dialog_id > 0 AND is_service=0 → SELF_SENDER_LABEL (DM outgoing)
      3. first_name resolves (non-empty str)       → first_name
      4. effective_sender_id OR sender_id known    → "(unknown user {id})"
      5. else                                      → "(unknown user)"

    Accepts both MessageLike objects (attribute access) and row dicts (key access)
    — uses getattr with safe defaults so both shapes work. Reading-tool callers
    pass row dicts through a SimpleNamespace or call directly with a dict-like
    wrapper; formatter callers pass MessageLike/DaemonMessage objects.
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
        sender_obj = getattr(msg_or_row, "sender", None)
        first_name = getattr(sender_obj, "first_name", None) if sender_obj is not None else None
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


def _resolve_sender_name(msg: MessageLike) -> str:
    """Backward-compatible wrapper delegating to resolve_sender_label."""
    return resolve_sender_label(msg)


def _render_text(msg: MessageLike) -> str:
    """Return message text, or a media placeholder for media-only messages."""
    text = getattr(msg, "message", "") or ""
    if text:
        return text
    media = getattr(msg, "media", None)
    if media is None:
        return ""
    # Pre-formatted daemon description (has _description attr from _MediaPlaceholder)
    if hasattr(media, "_description"):
        return str(media)
    return _describe_media(media)


def _format_reactions(msg: MessageLike, reaction_names: dict[str, list[str]] | None = None) -> str:
    """Return formatted reactions string like '[👍×3: Alice, Bob ❤️: Carol]', or empty string."""
    reactions = getattr(msg, "reactions", None)
    if reactions is None:
        return ""
    # Pre-formatted daemon reactions: pass through directly.
    # Temporary path -- remove when reaction_names_map is removed from
    # MessageLike protocol in models.py.
    preformatted = getattr(reactions, "_display", None)
    if preformatted is not None:
        return str(preformatted)
    # Telethon path: extract from reactions.results
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


def _describe_media(media: object) -> str:
    """Return a human-readable placeholder for a media attachment.

    Covers all common Telegram media types explicitly; falls back to
    '[медиа: ClassName]' for unknown types so they are still distinguishable.
    """
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

    return f"[медиа: {type(media).__name__}]"


def _describe_document(media: object) -> str:
    """Describe a MessageMediaDocument by inspecting its attributes.

    Priority order: sticker > round video > animation > audio > regular video > filename.
    Sticker checked first because sticker packs can carry a duration attribute.
    """
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
    resolved_now = now_unix if now_unix is not None else int(datetime.now(tz=timezone.utc).timestamp())

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
        chat_read_state = (
            read_state_per_dialog.get(chat.chat_id) if read_state_per_dialog else None
        )
        chat_dialog_type = (
            dialog_type_per_dialog.get(chat.chat_id) if dialog_type_per_dialog else None
        )
        read_state_header = _render_read_state_header(
            chat_read_state, chat_dialog_type, resolved_now, effective_tz
        )
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
