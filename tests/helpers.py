"""Shared test helpers for sync/event/delta tests."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TypedDict, Unpack


class _BuildMockMessageKwargs(TypedDict, total=False):
    text: str | None
    sender_id: int | None
    sender_first_name: str | None
    media: object | None
    reply_to_msg_id: int | None
    forum_topic: bool
    reply_to_top_id: int | None
    message_thread_id: int | None
    is_topic_message: bool
    reply_count: int
    reactions: object | None
    edit_date: datetime | None


def build_mock_message(
    id: int,
    **kwargs: Unpack[_BuildMockMessageKwargs],
) -> SimpleNamespace:
    """Build a minimal Telethon-like message object."""
    text = kwargs.get("text", "hello")
    sender_id = kwargs.get("sender_id", 42)
    sender_first_name = kwargs.get("sender_first_name", "Alice")
    media = kwargs.get("media")
    reply_to_msg_id = kwargs.get("reply_to_msg_id")
    forum_topic = kwargs.get("forum_topic", False)
    reply_to_top_id = kwargs.get("reply_to_top_id")
    message_thread_id = kwargs.get("message_thread_id")
    is_topic_message = kwargs.get("is_topic_message", False)
    reply_count = kwargs.get("reply_count", 0)
    reactions = kwargs.get("reactions")
    edit_date = kwargs.get("edit_date")
    sender = SimpleNamespace(first_name=sender_first_name) if sender_first_name is not None else None

    reply_to_obj: SimpleNamespace | None = None
    if reply_to_msg_id is not None or forum_topic:
        reply_to_obj = SimpleNamespace(
            reply_to_msg_id=reply_to_msg_id,
            forum_topic=forum_topic,
            reply_to_reply_top_id=reply_to_top_id,
        )

    return SimpleNamespace(
        id=id,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
        message=text,
        sender_id=sender_id,
        sender=sender,
        media=media,
        reply_to=reply_to_obj,
        replies=SimpleNamespace(replies=reply_count) if reply_count else None,
        reactions=reactions,
        edit_date=edit_date,
        message_thread_id=message_thread_id,
        is_topic_message=is_topic_message,
    )


def build_mock_reactions(counts: dict[str, int]) -> SimpleNamespace:
    """Build a mock MessageReactions object."""
    results = [
        SimpleNamespace(reaction=SimpleNamespace(emoticon=emoji), count=count) for emoji, count in counts.items()
    ]
    return SimpleNamespace(results=results)


class MockTotalList(list):
    """List subclass with .total attribute, mimicking Telethon TotalList.

    Use in tests that switch from iter_messages to get_messages:
        mock_client.get_messages = AsyncMock(
            return_value=MockTotalList([msg1, msg2], total=500)
        )
    """

    def __init__(self, items: list, total: int | None = None) -> None:
        super().__init__(items)
        self.total = total if total is not None else len(items)
