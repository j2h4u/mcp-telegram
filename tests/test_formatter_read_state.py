"""Tests for Phase 39.3 bidirectional read-state surfaces in formatter.

Covers:
- CursorState / ReadState type exports (models.py)
- `_render_read_state_header` — AC-5, AC-6, AC-7, D-01..D-04
- `_compute_inline_markers` — AC-8, AC-9, AC-10, D-05..D-07
- `format_messages` integration with `read_state` + `dialog_type` kwargs
- `format_unread_messages_grouped` per-chat headers (HIGH-3)
- `ListMessages` tool description literal-string audit (AC-13, D-11)

Markers are keyed by `message_id` (NOT page-render order) — verified by a
descending-render-order test (codex MEDIUM).
"""

from __future__ import annotations

from datetime import UTC, datetime

from mcp_telegram.models import ReadMessage

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_msg(
    mid: int,
    *,
    text: str = "hi",
    out: int = 0,
    dialog_id: int = 12345,
    edit_date: datetime | int | None = None,
    sender_first_name: str | None = "Alice",
    sent_at_dt: datetime | None = None,
) -> ReadMessage:
    """Create a ReadMessage for formatter tests with sensible defaults."""
    if sent_at_dt is None:
        sent_at_dt = _dt(12, 0)
    edit_date_int: int | None = None
    if isinstance(edit_date, datetime):
        edit_date_int = int(edit_date.timestamp())
    elif isinstance(edit_date, int):
        edit_date_int = edit_date
    return ReadMessage(
        message_id=mid,
        sent_at=int(sent_at_dt.timestamp()),
        dialog_id=dialog_id,
        text=text,
        out=out,
        sender_first_name=sender_first_name,
        sender_id=11,
        edit_date=edit_date_int,
    )


def _dt(h: int, m: int = 0, *, y: int = 2026, mo: int = 4, d: int = 21) -> datetime:
    return datetime(y, mo, d, h, m, 0, tzinfo=UTC)


def _unix(h: int, m: int = 0, *, y: int = 2026, mo: int = 4, d: int = 21) -> int:
    return int(_dt(h, m, y=y, mo=mo, d=d).timestamp())


# ---------------------------------------------------------------------------
# Model exports
# ---------------------------------------------------------------------------


def test_readstate_typeddict_exports() -> None:
    """models.py exports CursorState Literal + ReadState TypedDict."""
    # Literal alias: three allowed values
    import typing

    from mcp_telegram.models import CursorState, ReadState

    args = typing.get_args(CursorState)
    assert set(args) == {"populated", "null", "all_read"}

    # TypedDict: instantiable as a plain dict; required keys documented in code
    rs: ReadState = {  # type: ignore[typeddict-item]
        "inbox_unread_count": 0,
        "inbox_cursor_state": "all_read",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "all_read",
    }
    assert rs["inbox_unread_count"] == 0


# ---------------------------------------------------------------------------
# _render_read_state_header
# ---------------------------------------------------------------------------


def _collapsed_read_state() -> dict:
    return {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }


def test_header_collapsed_when_both_zero_and_populated() -> None:
    """AC-5: both sides populated AND both counts==0 → single '[read-state: all caught up]'."""
    from mcp_telegram.formatter import _render_read_state_header

    lines = _render_read_state_header(_collapsed_read_state(), "User", now_unix=_unix(12, 0))
    assert lines == ["[read-state: all caught up]"]


def test_header_split_when_inbox_unread_only() -> None:
    """AC-6: inbox unread only → 2 lines, outbox says 'all read by peer'."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 3,
        "inbox_oldest_unread_date": _unix(11, 13),  # 47m ago vs 12:00
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert len(lines) == 2
    assert lines[0] == "[inbox: 3 unread from peer, oldest 11:13 (47m ago)]"
    assert lines[1] == "[outbox: all read by peer]"


def test_header_split_when_outbox_unread_only() -> None:
    """AC-6 symmetric: outbox unread, inbox caught up."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 2,
        "outbox_oldest_unread_date": _unix(11, 13),
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert len(lines) == 2
    assert lines[0] == "[inbox: all read]"
    assert lines[1] == "[outbox: 2 unread by peer, oldest 11:13 (47m ago)]"


def test_header_split_when_both_unread() -> None:
    """AC-6: both sides unread → full split form."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 1,
        "inbox_oldest_unread_date": _unix(11, 30),
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 4,
        "outbox_oldest_unread_date": _unix(10, 0),
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert lines[0] == "[inbox: 1 unread from peer, oldest 11:30 (30m ago)]"
    assert lines[1] == "[outbox: 4 unread by peer, oldest 10:00 (2h 0m ago)]"


def test_header_inbox_null_renders_unknown() -> None:
    """D-03: NULL inbox cursor → '[inbox: unknown (sync pending)]'. Never 'all read'."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "null",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert len(lines) == 2
    assert lines[0] == "[inbox: unknown (sync pending)]"
    assert lines[1] == "[outbox: all read by peer]"


def test_header_outbox_null_renders_unknown() -> None:
    """D-03 symmetric: NULL outbox cursor."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "null",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert lines[0] == "[inbox: all read]"
    assert lines[1] == "[outbox: unknown (sync pending)]"


def test_header_both_null_renders_unknown_pair() -> None:
    """D-03: bootstrap pending both sides → two 'unknown (sync pending)' lines."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "null",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "null",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert lines == [
        "[inbox: unknown (sync pending)]",
        "[outbox: unknown (sync pending)]",
    ]


def test_header_omitted_for_non_dm_dialog_type() -> None:
    """AC-7: non-DM dialogs emit no header."""
    from mcp_telegram.formatter import _render_read_state_header

    for dt in ["Channel", "Group", "Forum", "Bot", "Chat"]:
        lines = _render_read_state_header(_collapsed_read_state(), dt, now_unix=_unix(12, 0))
        assert lines == [], f"non-DM dialog_type={dt} produced: {lines!r}"


def test_header_omitted_when_read_state_none() -> None:
    """Backward compat: read_state=None → []."""
    from mcp_telegram.formatter import _render_read_state_header

    assert _render_read_state_header(None, "User", now_unix=_unix(12, 0)) == []


def test_header_oldest_relative_delta_minutes() -> None:
    """D-01: Δ format under 1h → 'Xm ago'."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 1,
        "inbox_oldest_unread_date": _unix(11, 13),
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert "(47m ago)" in lines[0]


def test_header_oldest_relative_delta_hours_minutes() -> None:
    """D-01: 1h ≤ Δ < 1d → 'Xh Ym ago'."""
    from mcp_telegram.formatter import _render_read_state_header

    rs = {
        "inbox_unread_count": 1,
        "inbox_oldest_unread_date": _unix(9, 45),  # 2h 15m before 12:00
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=_unix(12, 0))
    assert "(2h 15m ago)" in lines[0]


def test_header_oldest_relative_delta_days() -> None:
    """D-01: 1d ≤ Δ < 7d → 'Xd ago'."""
    from mcp_telegram.formatter import _render_read_state_header

    now = _unix(12, 0)
    three_days_earlier = now - 3 * 86400
    rs = {
        "inbox_unread_count": 1,
        "inbox_oldest_unread_date": three_days_earlier,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=now)
    assert "(3d ago)" in lines[0]


def test_header_oldest_relative_delta_weeks() -> None:
    """D-01: Δ ≥ 7d → 'Xw ago'."""
    from mcp_telegram.formatter import _render_read_state_header

    now = _unix(12, 0)
    two_weeks_earlier = now - 14 * 86400
    rs = {
        "inbox_unread_count": 1,
        "inbox_oldest_unread_date": two_weeks_earlier,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    lines = _render_read_state_header(rs, "User", now_unix=now)
    assert "(2w ago)" in lines[0]


# ---------------------------------------------------------------------------
# _compute_inline_markers
# ---------------------------------------------------------------------------


def _mkmsg(mid: int, out: int = 0) -> ReadMessage:
    return _make_msg(mid, out=out)


def test_marker_inbox_boundary_on_highest_seen_incoming() -> None:
    """AC-8 quarter: [I read up to here] on highest incoming msg_id ≤ read_inbox_max_id."""
    from mcp_telegram.formatter import _compute_inline_markers

    msgs = [_mkmsg(1, 0), _mkmsg(2, 0), _mkmsg(3, 0), _mkmsg(4, 0)]
    rs = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 3,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    markers = _compute_inline_markers(msgs, rs)
    assert markers.get(3) == "[I read up to here]"


def test_marker_inbox_tail_on_lowest_unseen_incoming() -> None:
    """AC-8 quarter: [unread by me] on lowest incoming msg_id > read_inbox_max_id."""
    from mcp_telegram.formatter import _compute_inline_markers

    msgs = [_mkmsg(1, 0), _mkmsg(2, 0), _mkmsg(3, 0), _mkmsg(4, 0)]
    rs = {
        "inbox_unread_count": 2,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 2,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    markers = _compute_inline_markers(msgs, rs)
    # Lowest incoming > 2 is 3
    assert markers.get(3) == "[unread by me]"


def test_marker_outbox_boundary_on_highest_seen_outgoing() -> None:
    """AC-8 quarter: [peer read up to here]."""
    from mcp_telegram.formatter import _compute_inline_markers

    msgs = [_mkmsg(10, 1), _mkmsg(11, 1), _mkmsg(12, 1)]
    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 1,
        "outbox_cursor_state": "populated",
        "outbox_max_id_anchor": 11,
    }
    markers = _compute_inline_markers(msgs, rs)
    assert markers.get(11) == "[peer read up to here]"


def test_marker_outbox_tail_on_lowest_unseen_outgoing() -> None:
    """AC-8 quarter: [unread by peer]."""
    from mcp_telegram.formatter import _compute_inline_markers

    msgs = [_mkmsg(10, 1), _mkmsg(11, 1), _mkmsg(12, 1)]
    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 1,
        "outbox_cursor_state": "populated",
        "outbox_max_id_anchor": 11,
    }
    markers = _compute_inline_markers(msgs, rs)
    assert markers.get(12) == "[unread by peer]"


def test_marker_full_set_on_mixed_page() -> None:
    """AC-8: page with one of each → exactly four trailing markers."""
    from mcp_telegram.formatter import _compute_inline_markers

    # Incoming: 1 seen, 2 unseen (inbox anchor = 1). Outgoing: 10 seen, 11 unseen.
    msgs = [
        _mkmsg(1, 0),
        _mkmsg(2, 0),
        _mkmsg(10, 1),
        _mkmsg(11, 1),
    ]
    rs = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 1,
        "outbox_unread_count": 1,
        "outbox_cursor_state": "populated",
        "outbox_max_id_anchor": 10,
    }
    markers = _compute_inline_markers(msgs, rs)
    assert markers == {
        1: "[I read up to here]",
        2: "[unread by me]",
        10: "[peer read up to here]",
        11: "[unread by peer]",
    }


def test_marker_per_page_tail_with_anchor_absent() -> None:
    """AC-9: if every message on page is unread on a side, tail-start still emits."""
    from mcp_telegram.formatter import _compute_inline_markers

    # All incoming messages have id > inbox_max_id_anchor=0; every message unseen
    msgs = [_mkmsg(5, 0), _mkmsg(6, 0), _mkmsg(7, 0)]
    rs = {
        "inbox_unread_count": 3,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 0,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    markers = _compute_inline_markers(msgs, rs)
    # Tail-start = lowest unseen = 5. No boundary (no qualifying seen incoming).
    assert markers.get(5) == "[unread by me]"
    assert not any(v == "[I read up to here]" for v in markers.values())


def test_marker_boundary_omitted_when_anchor_not_on_page() -> None:
    """AC-10: boundary anchor off-page → boundary marker omitted."""
    from mcp_telegram.formatter import _compute_inline_markers

    # Page shows msg_ids 5,6,7. inbox_max_id_anchor=3 (off-page).
    msgs = [_mkmsg(5, 0), _mkmsg(6, 0), _mkmsg(7, 0)]
    rs = {
        "inbox_unread_count": 3,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 3,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    markers = _compute_inline_markers(msgs, rs)
    # No incoming msg on page has id ≤ 3 → no boundary.
    assert not any(v == "[I read up to here]" for v in markers.values())
    # Tail-start still fires on 5.
    assert markers.get(5) == "[unread by me]"


def test_marker_zero_for_non_dm() -> None:
    """AC-7: inline markers zero when read_state is None (non-DM has read_state=None)."""
    from mcp_telegram.formatter import _compute_inline_markers

    msgs = [_mkmsg(1, 0), _mkmsg(2, 1)]
    assert _compute_inline_markers(msgs, None) == {}


def test_marker_null_inbox_cursor_treats_all_incoming_as_unread() -> None:
    """D-03 marker side: NULL inbox cursor → no boundary, tail on lowest incoming."""
    from mcp_telegram.formatter import _compute_inline_markers

    msgs = [_mkmsg(5, 0), _mkmsg(6, 0), _mkmsg(7, 0)]
    rs = {
        "inbox_unread_count": 3,
        "inbox_cursor_state": "null",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    markers = _compute_inline_markers(msgs, rs)
    assert markers.get(5) == "[unread by me]"
    assert not any(v == "[I read up to here]" for v in markers.values())


def test_marker_anchor_uses_message_id_not_render_order() -> None:
    """codex MEDIUM: Pass messages in descending render order; markers still keyed by message_id.

    The marker dict from _compute_inline_markers must be identical regardless of input order
    because it is keyed by msg.id, not position.
    """
    from mcp_telegram.formatter import _compute_inline_markers

    msgs_asc = [_mkmsg(1, 0), _mkmsg(2, 0), _mkmsg(3, 0), _mkmsg(4, 0)]
    msgs_desc = list(reversed(msgs_asc))
    rs = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 3,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    assert _compute_inline_markers(msgs_asc, rs) == _compute_inline_markers(msgs_desc, rs)


# ---------------------------------------------------------------------------
# format_messages integration
# ---------------------------------------------------------------------------


def test_format_messages_emits_header_when_dm() -> None:
    """DM + read_state → output starts with header line(s)."""
    from mcp_telegram.formatter import format_messages

    msgs = [_make_msg(1, text="hi", out=0)]
    rs = _collapsed_read_state()
    result = format_messages(msgs, reply_map={}, read_state=rs, dialog_type="User", now_unix=_unix(12, 0))
    assert result.splitlines()[0] == "[read-state: all caught up]"


def test_format_messages_no_header_when_non_dm() -> None:
    """AC-7: Channel dialog → no header regardless of read_state."""
    from mcp_telegram.formatter import format_messages

    msgs = [_make_msg(1, text="hi", out=0)]
    rs = _collapsed_read_state()
    result = format_messages(msgs, reply_map={}, read_state=rs, dialog_type="Channel", now_unix=_unix(12, 0))
    for banned in ["[read-state:", "[inbox:", "[outbox:"]:
        assert banned not in result, f"non-DM output contained {banned!r}: {result!r}"


def test_format_messages_backward_compat_without_kwargs() -> None:
    """Default call (no new kwargs) produces byte-identical output to today."""
    from mcp_telegram.formatter import format_messages

    msgs = [_make_msg(1, text="hi there", sent_at_dt=_dt(14, 30))]
    baseline = format_messages(msgs, reply_map={})
    # Also callable with read_state=None, dialog_type=None → identical
    with_none = format_messages(msgs, reply_map={}, read_state=None, dialog_type=None)
    assert baseline == with_none
    assert "[read-state:" not in baseline
    assert "[inbox:" not in baseline


def test_format_messages_emits_inline_marker_trailing() -> None:
    """Marker appended at end of the message line."""
    from mcp_telegram.formatter import format_messages

    msgs = [
        _make_msg(2, text="newest", out=0, sent_at_dt=_dt(12, 5)),
        _make_msg(1, text="oldest", out=0),
    ]
    rs = {
        "inbox_unread_count": 1,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 1,  # msg 1 seen, msg 2 unseen
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    result = format_messages(msgs, reply_map={}, read_state=rs, dialog_type="User", now_unix=_unix(12, 10))
    # Msg 1 line should end with "[I read up to here]"
    lines = result.splitlines()
    boundary_line = next(l for l in lines if "oldest" in l)
    tail_line = next(l for l in lines if "newest" in l)
    assert boundary_line.endswith("[I read up to here]"), boundary_line
    assert tail_line.endswith("[unread by me]"), tail_line


def test_marker_trails_after_existing_edited_metadata() -> None:
    """D-06: marker goes AFTER [edited HH:mm]."""
    from mcp_telegram.formatter import format_messages

    edit_dt = datetime(2026, 4, 21, 12, 30, tzinfo=UTC)
    msg = _make_msg(1, text="hi", out=0, edit_date=edit_dt)
    rs = {
        "inbox_unread_count": 0,
        "inbox_cursor_state": "populated",
        "inbox_max_id_anchor": 1,
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    result = format_messages([msg], reply_map={}, read_state=rs, dialog_type="User", now_unix=_unix(13, 0))
    line = [l for l in result.splitlines() if "hi" in l][0]
    assert "[edited 12:30]" in line
    assert line.endswith("[I read up to here]")
    # Order: ... [edited 12:30] [I read up to here]
    assert line.index("[edited 12:30]") < line.index("[I read up to here]")


# ---------------------------------------------------------------------------
# format_unread_messages_grouped per-chat headers (HIGH-3)
# ---------------------------------------------------------------------------


def test_format_unread_messages_grouped_emits_per_chat_header() -> None:
    """HIGH-3: each DM block gets exactly one header, positioned before the block's messages."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    chat_a = UnreadChatData(
        chat_id=111,
        display_name="Alice",
        unread_count=1,
        messages=[_make_msg(1, text="hey A", dialog_id=111)],
        total_in_chat=1,
    )
    chat_b = UnreadChatData(
        chat_id=222,
        display_name="Bob",
        unread_count=0,
        messages=[_make_msg(2, text="hey B", dialog_id=222)],
        total_in_chat=1,
    )
    rs_a = {
        "inbox_unread_count": 1,
        "inbox_oldest_unread_date": _unix(11, 30),
        "inbox_cursor_state": "populated",
        "outbox_unread_count": 0,
        "outbox_cursor_state": "populated",
    }
    rs_b = _collapsed_read_state()
    result = format_unread_messages_grouped(
        [chat_a, chat_b],
        read_state_per_dialog={111: rs_a, 222: rs_b},
        dialog_type_per_dialog={111: "User", 222: "User"},
        now_unix=_unix(12, 0),
    )
    # chat_a gets split header (inbox unread), chat_b gets collapsed header
    assert "[inbox: 1 unread from peer" in result
    assert "[read-state: all caught up]" in result
    # Each header appears before its block's message body
    idx_a_header = result.index("[inbox: 1 unread from peer")
    idx_a_body = result.index("hey A")
    assert idx_a_header < idx_a_body
    idx_b_header = result.index("[read-state: all caught up]")
    idx_b_body = result.index("hey B")
    assert idx_b_header < idx_b_body


def test_format_unread_messages_grouped_no_header_for_non_dm_block() -> None:
    """AC-7: non-DM group → zero header lines for that block."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    chat = UnreadChatData(
        chat_id=-100500,
        display_name="Some Channel",
        unread_count=3,
        messages=[],
        total_in_chat=3,
        is_channel=True,
    )
    result = format_unread_messages_grouped(
        [chat],
        read_state_per_dialog={-100500: _collapsed_read_state()},
        dialog_type_per_dialog={-100500: "Channel"},
        now_unix=_unix(12, 0),
    )
    for banned in ["[read-state:", "[inbox:", "[outbox:"]:
        assert banned not in result


def test_format_unread_messages_grouped_backward_compat_without_kwargs() -> None:
    """Backward compat: old call signature produces unchanged output."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    chat = UnreadChatData(
        chat_id=111,
        display_name="Alice",
        unread_count=1,
        messages=[_make_msg(1, text="hey", dialog_id=111)],
        total_in_chat=1,
    )
    baseline = format_unread_messages_grouped([chat])
    assert "[read-state:" not in baseline
    assert "[inbox:" not in baseline


# ---------------------------------------------------------------------------
# Tool description audit
# ---------------------------------------------------------------------------


def test_tool_description_contains_all_seven_ac13_literals() -> None:
    """AC-13: ListMessages description contains all 7 literal strings."""
    from mcp_telegram.tools.reading import ListMessages

    doc = ListMessages.__doc__ or ""
    for literal in [
        "[I read up to here]",
        "[unread by me]",
        "[peer read up to here]",
        "[unread by peer]",
        "[read-state:",
        "[inbox:",
        "[outbox:",
    ]:
        assert literal in doc, f"ListMessages doc missing AC-13 literal: {literal!r}"


def test_tool_description_read_state_section_under_8_lines() -> None:
    """D-11: The 'Read-state annotations' section is ≤ 8 lines."""
    from mcp_telegram.tools.reading import ListMessages

    doc = ListMessages.__doc__ or ""
    assert "Read-state annotations" in doc
    # Extract the section starting at its heading up to next blank-line gap
    lines = doc.splitlines()
    start = next(i for i, l in enumerate(lines) if "Read-state annotations" in l)
    section: list[str] = []
    for l in lines[start:]:
        if not l.strip():
            break
        section.append(l)
    assert len(section) <= 8, f"Section has {len(section)} lines: {section}"
