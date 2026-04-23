from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass
class MockSender:
    first_name: str
    last_name: str | None = None


@dataclass
class MockMessage:
    id: int
    date: datetime  # must be timezone-aware (UTC)
    message: str = ""
    sender: MockSender | None = None
    sender_id: int | None = None
    media: object = None
    reactions: object = None
    reply_to: object = None
    edit_date: int | datetime | None = None


def _make_msg(
    id: int,
    dt: datetime,
    text: str = "hello",
    first_name: str = "Alice",
    media: object = None,
    reactions: object = None,
    reply_to: object = None,
    sender_id: int = 1,
) -> MockMessage:
    return MockMessage(
        id=id,
        date=dt,
        message=text,
        sender=MockSender(first_name=first_name),
        sender_id=sender_id,
        media=media,
        reactions=reactions,
        reply_to=reply_to,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_basic_format() -> None:
    """Single message → 'HH:mm FirstName: text' plus a date header."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="hi there", first_name="Bob")
    # Telethon returns newest-first; single message is both newest and oldest
    result = format_messages([msg], {})
    lines = result.strip().splitlines()
    # First line must be a date header
    assert lines[0] == "--- 2024-06-15 ---", f"Expected date header, got: {lines[0]!r}"
    # Second line: message
    assert lines[1] == "14:30 Bob: hi there", f"Unexpected message line: {lines[1]!r}"


def test_date_header() -> None:
    """Two messages on different calendar days → date header between them."""
    from mcp_telegram.formatter import format_messages

    dt1 = datetime(2024, 6, 15, 23, 0, 0, tzinfo=UTC)
    dt2 = datetime(2024, 6, 16, 1, 0, 0, tzinfo=UTC)  # next day, 2h later
    # newest-first: dt2 first, dt1 second
    msgs = [
        _make_msg(2, dt2, text="good morning", first_name="Alice"),
        _make_msg(1, dt1, text="good night", first_name="Alice"),
    ]
    result = format_messages(msgs, {})
    lines = result.strip().splitlines()
    # Expect: date_header_june15, msg1, date_header_june16, msg2
    assert "--- 2024-06-15 ---" in lines, f"Missing June 15 header. Lines: {lines}"
    assert "--- 2024-06-16 ---" in lines, f"Missing June 16 header. Lines: {lines}"
    idx15 = lines.index("--- 2024-06-15 ---")
    idx16 = lines.index("--- 2024-06-16 ---")
    assert idx15 < idx16, "June 15 header should come before June 16 header"
    # msg1 (23:00) between the two headers
    assert any("23:00" in l and "good night" in l for l in lines[idx15:idx16]), (
        "23:00 good night not found between date headers"
    )
    # msg2 (01:00) after June 16 header
    assert any("01:00" in l and "good morning" in l for l in lines[idx16:]), (
        "01:00 good morning not found after June 16 header"
    )


def test_session_break() -> None:
    """Two messages 90 minutes apart → session-break line '--- N мин ---'."""
    from mcp_telegram.formatter import format_messages

    dt1 = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
    dt2 = datetime(2024, 6, 15, 11, 30, 0, tzinfo=UTC)  # 90 min later
    # newest-first
    msgs = [
        _make_msg(2, dt2, text="later", first_name="Alice"),
        _make_msg(1, dt1, text="earlier", first_name="Alice"),
    ]
    result = format_messages(msgs, {})
    lines = result.strip().splitlines()
    # There must be a session-break line containing 'мин'
    break_lines = [l for l in lines if "мин" in l and l.startswith("---")]
    assert break_lines, f"Expected session-break line, got none. Lines: {lines}"
    # The break should indicate 90 minutes
    assert "90" in break_lines[0], f"Expected 90 мин in break line, got: {break_lines[0]!r}"


def test_no_session_break_within_60_min() -> None:
    """Two messages 30 minutes apart → no session-break line."""
    from mcp_telegram.formatter import format_messages

    dt1 = datetime(2024, 6, 15, 10, 0, 0, tzinfo=UTC)
    dt2 = datetime(2024, 6, 15, 10, 30, 0, tzinfo=UTC)  # 30 min later
    # newest-first
    msgs = [
        _make_msg(2, dt2, text="second", first_name="Alice"),
        _make_msg(1, dt1, text="first", first_name="Alice"),
    ]
    result = format_messages(msgs, {})
    lines = result.strip().splitlines()
    break_lines = [l for l in lines if "мин" in l and l.startswith("---")]
    assert not break_lines, f"Unexpected session-break line for 30-min gap. Lines: {lines}"


def test_empty_message_list() -> None:
    """Empty list returns empty string without raising."""
    from mcp_telegram.formatter import format_messages

    result = format_messages([], {})
    assert result == "", f"Expected empty string, got: {result!r}"


def test_newest_first_ordering() -> None:
    """Messages passed newest-first are displayed oldest-first in output."""
    from mcp_telegram.formatter import format_messages

    dt1 = datetime(2024, 6, 15, 9, 0, 0, tzinfo=UTC)
    dt2 = datetime(2024, 6, 15, 9, 5, 0, tzinfo=UTC)  # 5 min later
    dt3 = datetime(2024, 6, 15, 9, 10, 0, tzinfo=UTC)  # 10 min from start
    # newest-first order: dt3, dt2, dt1
    msgs = [
        _make_msg(3, dt3, text="third", first_name="Alice"),
        _make_msg(2, dt2, text="second", first_name="Alice"),
        _make_msg(1, dt1, text="first", first_name="Alice"),
    ]
    result = format_messages(msgs, {})
    # "first" should appear before "second" before "third" in output
    pos_first = result.index("first")
    pos_second = result.index("second")
    pos_third = result.index("third")
    assert pos_first < pos_second < pos_third, "Messages not displayed oldest-first"


def test_unknown_sender() -> None:
    """Service message (is_service=1) renders as 'System' (Phase 39.1-02 contract)."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    msg = MockMessage(id=1, date=dt, message="anonymous", sender=None, sender_id=None)
    # Phase 39.1-02: "System" now requires is_service=1 (not just sender_id=None).
    # MockMessage lacks is_service; set via direct attribute assignment.
    msg.is_service = 1  # type: ignore[attr-defined]
    result = format_messages([msg], {})
    assert "System: anonymous" in result, f"Expected 'System: anonymous', got: {result!r}"


def test_media_fallback() -> None:
    """Message with unknown media type shows '[медиа: ClassName]'."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="", first_name="Carol", media=object())
    result = format_messages([msg], {})
    assert "[медиа:" in result, f"Expected '[медиа: ...]' for unknown media, got: {result!r}"


def test_reply_annotation() -> None:
    """Message with reply_to shows '[↑ SenderName HH:mm]' prefix."""
    from mcp_telegram.formatter import format_messages

    dt_orig = datetime(2024, 6, 15, 9, 0, 0, tzinfo=UTC)
    dt_reply = datetime(2024, 6, 15, 9, 5, 0, tzinfo=UTC)
    orig = _make_msg(1, dt_orig, text="original", first_name="Alice")
    reply_msg = _make_msg(2, dt_reply, text="reply text", first_name="Bob")

    @dataclass
    class FakeReplyTo:
        reply_to_msg_id: int

    reply_msg.reply_to = FakeReplyTo(reply_to_msg_id=1)

    result = format_messages([reply_msg, orig], reply_map={1: orig})
    assert "[↑ Alice 09:00]" in result, f"Expected reply annotation, got: {result!r}"
    assert "reply text" in result


def test_reactions_display() -> None:
    """Message with reactions shows '[emoji×count: name]' when names provided."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    @dataclass
    class FakeReaction:
        emoticon: str

    @dataclass
    class FakeReactionCount:
        reaction: FakeReaction
        count: int

    @dataclass
    class FakeReactions:
        results: list

    reactions = FakeReactions(
        results=[
            FakeReactionCount(reaction=FakeReaction(emoticon="👍"), count=2),
            FakeReactionCount(reaction=FakeReaction(emoticon="❤️"), count=1),
        ]
    )
    msg = _make_msg(1, dt, text="hello", first_name="Alice", reactions=reactions)
    reaction_names_map = {1: {"👍": ["Bob", "Carol"], "❤️": ["Dave"]}}
    result = format_messages([msg], {}, reaction_names_map=reaction_names_map)
    assert "[👍×2: Bob, Carol ❤️: Dave]" in result, f"Expected reactions with names, got: {result!r}"


def test_reactions_count_only() -> None:
    """Message with reactions shows count-only '[emoji×N]' when no names provided."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    @dataclass
    class FakeReaction:
        emoticon: str

    @dataclass
    class FakeReactionCount:
        reaction: FakeReaction
        count: int

    @dataclass
    class FakeReactions:
        results: list

    reactions = FakeReactions(
        results=[
            FakeReactionCount(reaction=FakeReaction(emoticon="🔥"), count=42),
        ]
    )
    msg = _make_msg(1, dt, text="hot", first_name="Alice", reactions=reactions)
    result = format_messages([msg], {})
    assert "[🔥×42]" in result, f"Expected count-only reactions, got: {result!r}"


# ---------------------------------------------------------------------------
# Tests for format_unread_messages_grouped
# ---------------------------------------------------------------------------


def test_format_unread_grouped_single_chat() -> None:
    """Single chat formatted with header and messages."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="hello", first_name="Alice")

    result = format_unread_messages_grouped(
        [
            UnreadChatData(chat_id=123, display_name="Alice", unread_count=1, messages=[msg], total_in_chat=1),
        ]
    )
    assert "--- Alice (1 непрочитанных, id=123) ---" in result
    assert "hello" in result


def test_format_unread_grouped_with_mentions() -> None:
    """Chat with mentions shows mention count in header."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="@User hello", first_name="Bob")

    result = format_unread_messages_grouped(
        [
            UnreadChatData(
                chat_id=456,
                display_name="Рабочий чат",
                unread_count=3,
                unread_mentions_count=2,
                messages=[msg],
                total_in_chat=3,
            ),
        ]
    )
    assert "2 упоминания" in result or "2 упоминаний" in result
    assert "id=456" in result


def test_format_unread_grouped_trim_marker() -> None:
    """When messages < total_in_chat, shows '[и ещё N]' marker."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="msg1", first_name="Alice")

    result = format_unread_messages_grouped(
        [
            UnreadChatData(chat_id=789, display_name="Big Chat", unread_count=10, messages=[msg], total_in_chat=10),
        ]
    )
    assert "[и ещё 9]" in result


def test_format_unread_grouped_channel_no_messages() -> None:
    """Channel shows count only, no messages."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="news", first_name="NewsBot")

    result = format_unread_messages_grouped(
        [
            UnreadChatData(
                chat_id=-1001234567890,
                display_name="TechNews",
                unread_count=47,
                messages=[msg],
                total_in_chat=47,
                is_channel=True,
            ),
        ]
    )
    assert "TechNews (47 непрочитанных" in result
    assert "news" not in result


def test_format_unread_grouped_empty() -> None:
    """Empty input returns empty string."""
    from mcp_telegram.formatter import format_unread_messages_grouped

    result = format_unread_messages_grouped([])
    assert result == ""


def test_format_unread_grouped_multiple_chats() -> None:
    """Multiple chats formatted separately."""
    from mcp_telegram.formatter import UnreadChatData, format_unread_messages_grouped

    dt1 = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    dt2 = datetime(2024, 6, 15, 14, 40, 0, tzinfo=UTC)
    msg1 = _make_msg(1, dt1, text="hi from alice", first_name="Alice")
    msg2 = _make_msg(2, dt2, text="hi from bob", first_name="Bob")

    result = format_unread_messages_grouped(
        [
            UnreadChatData(chat_id=111, display_name="Alice", unread_count=2, messages=[msg1], total_in_chat=2),
            UnreadChatData(chat_id=222, display_name="Bob", unread_count=1, messages=[msg2], total_in_chat=1),
        ]
    )
    assert "Alice" in result
    assert "Bob" in result
    assert "id=111" in result
    assert "id=222" in result
    assert "[и ещё 1]" in result  # Alice's remaining


# ---------------------------------------------------------------------------
# Edited marker tests (Phase 22)
# ---------------------------------------------------------------------------


def test_edited_marker_shown_when_edit_date_is_int() -> None:
    """[edited HH:mm] appears when edit_date is an integer Unix timestamp (CachedMessage style)."""
    from mcp_telegram.formatter import format_messages

    # 1718464800 = 2024-06-15 15:20:00 UTC
    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = MockMessage(
        id=1,
        date=dt,
        message="edited text",
        sender=MockSender(first_name="Alice"),
        sender_id=1,
        edit_date=1718464800,
    )
    result = format_messages([msg], {})
    assert "[edited 15:20]" in result, f"Expected '[edited 15:20]' in output, got: {result!r}"


def test_edited_marker_shown_when_edit_date_is_datetime() -> None:
    """[edited HH:mm] appears when edit_date is a datetime (Telethon style)."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    edit_dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = MockMessage(
        id=1,
        date=dt,
        message="edited text",
        sender=MockSender(first_name="Alice"),
        sender_id=1,
        edit_date=edit_dt,
    )
    result = format_messages([msg], {})
    assert "[edited 14:30]" in result, f"Expected '[edited 14:30]' in output, got: {result!r}"


def test_edited_marker_absent_when_edit_date_none() -> None:
    """No [edited ...] marker when edit_date is absent or None."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    msg = _make_msg(1, dt, text="no edit")
    result = format_messages([msg], {})
    assert "[edited" not in result, f"Unexpected '[edited' in output: {result!r}"


def test_edited_marker_before_reactions() -> None:
    """[edited HH:mm] appears before reactions bracket in the output line."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)

    @dataclass
    class FakeReaction:
        emoticon: str

    @dataclass
    class FakeReactionCount:
        reaction: FakeReaction
        count: int

    @dataclass
    class FakeReactions:
        results: list

    reactions = FakeReactions(
        results=[
            FakeReactionCount(reaction=FakeReaction(emoticon="👍"), count=2),
        ]
    )
    msg = MockMessage(
        id=1,
        date=dt,
        message="hello",
        sender=MockSender(first_name="Alice"),
        sender_id=1,
        reactions=reactions,
        edit_date=1718460000,
    )
    reaction_names_map = {1: {"👍": ["Bob", "Carol"]}}
    result = format_messages([msg], {}, reaction_names_map=reaction_names_map)
    assert "[edited" in result, f"Expected '[edited' in output: {result!r}"
    assert "[👍" in result, f"Expected '[👍' in output: {result!r}"
    assert result.index("[edited") < result.index("[👍"), f"Expected '[edited' to appear before '[👍' in: {result!r}"


# ---------------------------------------------------------------------------
# _resolve_sender_name three-way fallback tests (Phase 39)
# ---------------------------------------------------------------------------

from types import SimpleNamespace


def _rsn_msg(
    sender_id=None,
    sender_first_name=...,
    *,
    is_service=0,
    out=0,
    dialog_id=0,
    effective_sender_id=None,
):
    """Build a minimal message-like object for resolve_sender_label / _resolve_sender_name.

    Phase 39.1-02: carries is_service, out, dialog_id, effective_sender_id in
    addition to sender_id/sender_first_name to exercise the 5-branch decision.
    """
    if sender_first_name is ...:
        sender = None
    else:
        sender = SimpleNamespace(first_name=sender_first_name)
    return SimpleNamespace(
        sender_id=sender_id,
        sender=sender,
        is_service=is_service,
        out=out,
        dialog_id=dialog_id,
        effective_sender_id=effective_sender_id,
    )


# --- Phase 39.1-02: 5-branch decision tree tests ---


def test_resolve_sender_name_service_message_renders_system():
    from mcp_telegram.formatter import _resolve_sender_name

    # is_service=1 → "System" regardless of other fields
    assert _resolve_sender_name(_rsn_msg(sender_id=None, is_service=1)) == "System"


def test_resolve_sender_name_service_message_ignores_first_name():
    from mcp_telegram.formatter import _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=42, sender_first_name="Alice", is_service=1)) == "System"


def test_resolve_sender_name_dm_outgoing_renders_self_label():
    """DM outgoing (out=1, dialog_id>0, is_service=0) → SELF_SENDER_LABEL."""
    from mcp_telegram.formatter import SELF_SENDER_LABEL, _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=None, out=1, dialog_id=268071163, is_service=0)) == SELF_SENDER_LABEL
    assert SELF_SENDER_LABEL == "[me]"


def test_resolve_sender_name_dm_incoming_with_first_name():
    from mcp_telegram.formatter import _resolve_sender_name

    assert (
        _resolve_sender_name(
            _rsn_msg(
                sender_id=None,
                sender_first_name="Alice",
                out=0,
                dialog_id=268071163,
                is_service=0,
                effective_sender_id=268071163,
            )
        )
        == "Alice"
    )


def test_resolve_sender_name_dm_incoming_unknown_renders_unknown_user_with_id():
    """DM incoming with no first_name → '(unknown user <effective_sender_id>)'."""
    from mcp_telegram.formatter import _resolve_sender_name

    assert (
        _resolve_sender_name(
            _rsn_msg(
                sender_id=None,
                out=0,
                dialog_id=268071163,
                is_service=0,
                effective_sender_id=268071163,
            )
        )
        == "(unknown user 268071163)"
    )


def test_resolve_sender_name_group_unknown_sender_renders_unknown_user():
    """Group unknown sender (no id anywhere) → '(unknown user)'."""
    from mcp_telegram.formatter import _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=None, out=0, dialog_id=-100123, is_service=0)) == "(unknown user)"


# --- Legacy Phase 39 tests, migrated to Phase 39.1-02 contract ---


def test_resolve_sender_name_returns_first_name_when_present():
    from mcp_telegram.formatter import _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=42, sender_first_name="Alice")) == "Alice"


def test_resolve_sender_name_returns_unknown_user_with_id_when_sender_missing():
    from mcp_telegram.formatter import _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=42)) == "(unknown user 42)"


def test_resolve_sender_name_returns_unknown_user_with_id_when_first_name_none():
    from mcp_telegram.formatter import _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=42, sender_first_name=None)) == "(unknown user 42)"


def test_resolve_sender_name_returns_unknown_user_with_id_when_first_name_empty():
    from mcp_telegram.formatter import _resolve_sender_name

    assert _resolve_sender_name(_rsn_msg(sender_id=42, sender_first_name="")) == "(unknown user 42)"


# ---------------------------------------------------------------------------
# Golden output test — daemon path (fwd, post_author, edit, reactions)
# ---------------------------------------------------------------------------


def test_golden_daemon_fields() -> None:
    """Golden contract for format_messages on the daemon path.

    Covers the four fields that arrive via the daemon API and are absent from
    the Telethon MockMessage helpers above: fwd_from_name, post_author,
    edit_date, and pre-formatted reactions_display.  The expected string pins
    the exact output format; update it only when the format intentionally
    changes (e.g. after Wave 2 ReadMessage migration).
    """
    from types import SimpleNamespace
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=UTC)
    edit_dt = datetime(2024, 6, 15, 15, 0, 0, tzinfo=UTC)

    msg = SimpleNamespace(
        id=1,
        date=dt,
        message="Interesting article",
        sender=SimpleNamespace(first_name="Olga"),
        sender_id=101,
        effective_sender_id=101,
        # Pre-formatted reactions_display simulates _PreformattedReactions
        # (formatter checks getattr(reactions, "_display", None)).
        reactions=SimpleNamespace(_display="[👍×2]"),
        media=None,
        reply_to=None,
        edit_date=edit_dt,
        post_author="Olga Smith",
        fwd_from_name="Tech News",
        is_service=0,
        out=0,
        dialog_id=100,
        topic_title=None,
    )

    result = format_messages([msg], {})

    assert "--- 2024-06-15 ---" in result
    expected_line = (
        "14:30 Olga: [by Olga Smith] [↪ fwd: Tech News] "
        "Interesting article [edited 15:00] [👍×2]"
    )
    assert expected_line in result, f"Golden output mismatch.\nGot:\n{result}"
