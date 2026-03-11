from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta


@dataclass
class MockSender:
    first_name: str
    last_name: str | None = None


@dataclass
class MockMessage:
    id: int
    date: datetime          # must be timezone-aware (UTC)
    message: str = ""
    sender: MockSender | None = None
    media: object = None
    reactions: object = None
    reply_to: object = None


def _make_msg(
    id: int,
    dt: datetime,
    text: str = "hello",
    first_name: str = "Alice",
    media: object = None,
    reactions: object = None,
    reply_to: object = None,
) -> MockMessage:
    return MockMessage(
        id=id,
        date=dt,
        message=text,
        sender=MockSender(first_name=first_name),
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

    dt = datetime(2024, 6, 15, 14, 30, 0, tzinfo=timezone.utc)
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

    dt1 = datetime(2024, 6, 15, 23, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2024, 6, 16, 1, 0, 0, tzinfo=timezone.utc)  # next day, 2h later
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

    dt1 = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2024, 6, 15, 11, 30, 0, tzinfo=timezone.utc)  # 90 min later
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

    dt1 = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)  # 30 min later
    # newest-first
    msgs = [
        _make_msg(2, dt2, text="second", first_name="Alice"),
        _make_msg(1, dt1, text="first", first_name="Alice"),
    ]
    result = format_messages(msgs, {})
    lines = result.strip().splitlines()
    break_lines = [l for l in lines if "мин" in l and l.startswith("---")]
    assert not break_lines, (
        f"Unexpected session-break line for 30-min gap. Lines: {lines}"
    )


def test_empty_message_list() -> None:
    """Empty list returns empty string without raising."""
    from mcp_telegram.formatter import format_messages

    result = format_messages([], {})
    assert result == "", f"Expected empty string, got: {result!r}"


def test_newest_first_ordering() -> None:
    """Messages passed newest-first are displayed oldest-first in output."""
    from mcp_telegram.formatter import format_messages

    dt1 = datetime(2024, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
    dt2 = datetime(2024, 6, 15, 9, 5, 0, tzinfo=timezone.utc)   # 5 min later
    dt3 = datetime(2024, 6, 15, 9, 10, 0, tzinfo=timezone.utc)  # 10 min from start
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
    assert pos_first < pos_second < pos_third, (
        "Messages not displayed oldest-first"
    )


def test_unknown_sender() -> None:
    """Message with no sender falls back to 'Unknown'."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    msg = MockMessage(id=1, date=dt, message="anonymous", sender=None)
    result = format_messages([msg], {})
    assert "Unknown: anonymous" in result, f"Expected 'Unknown: anonymous', got: {result!r}"


def test_media_fallback() -> None:
    """Message with non-None media and no text shows '[медиа]'."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    msg = _make_msg(1, dt, text="", first_name="Carol", media=object())
    result = format_messages([msg], {})
    assert "[медиа]" in result, f"Expected '[медиа]' for media message, got: {result!r}"


def test_reply_annotation() -> None:
    """Message with reply_to shows '[↑ SenderName HH:mm]' prefix."""
    from mcp_telegram.formatter import format_messages

    dt_orig = datetime(2024, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
    dt_reply = datetime(2024, 6, 15, 9, 5, 0, tzinfo=timezone.utc)
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

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

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

    reactions = FakeReactions(results=[
        FakeReactionCount(reaction=FakeReaction(emoticon="👍"), count=2),
        FakeReactionCount(reaction=FakeReaction(emoticon="❤️"), count=1),
    ])
    msg = _make_msg(1, dt, text="hello", first_name="Alice", reactions=reactions)
    reaction_names_map = {1: {"👍": ["Bob", "Carol"], "❤️": ["Dave"]}}
    result = format_messages([msg], {}, reaction_names_map=reaction_names_map)
    assert "[👍×2: Bob, Carol ❤️: Dave]" in result, f"Expected reactions with names, got: {result!r}"


def test_reactions_count_only() -> None:
    """Message with reactions shows count-only '[emoji×N]' when no names provided."""
    from mcp_telegram.formatter import format_messages

    dt = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

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

    reactions = FakeReactions(results=[
        FakeReactionCount(reaction=FakeReaction(emoticon="🔥"), count=42),
    ])
    msg = _make_msg(1, dt, text="hot", first_name="Alice", reactions=reactions)
    result = format_messages([msg], {})
    assert "[🔥×42]" in result, f"Expected count-only reactions, got: {result!r}"
