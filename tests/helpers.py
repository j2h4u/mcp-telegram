"""Shared test helpers for sync/event/delta tests."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace


def build_mock_message(
    id: int,  # noqa: A002
    text: str | None = "hello",
    sender_id: int | None = 42,
    sender_first_name: str | None = "Alice",
    media: object | None = None,
    reply_to_msg_id: int | None = None,
    forum_topic: bool = False,
    reply_to_top_id: int | None = None,
    reactions: object | None = None,
    edit_date: datetime | None = None,
) -> SimpleNamespace:
    """Build a minimal Telethon-like message object."""
    sender = (
        SimpleNamespace(first_name=sender_first_name)
        if sender_first_name is not None
        else None
    )

    reply_to_obj: SimpleNamespace | None = None
    if reply_to_msg_id is not None or forum_topic:
        reply_to_obj = SimpleNamespace(
            reply_to_msg_id=reply_to_msg_id,
            forum_topic=forum_topic,
            reply_to_top_id=reply_to_top_id,
        )

    return SimpleNamespace(
        id=id,
        date=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        message=text,
        sender_id=sender_id,
        sender=sender,
        media=media,
        reply_to=reply_to_obj,
        reactions=reactions,
        edit_date=edit_date,
    )


def build_mock_reactions(counts: dict[str, int]) -> SimpleNamespace:
    """Build a mock MessageReactions object."""
    results = [
        SimpleNamespace(reaction=SimpleNamespace(emoticon=emoji), count=count)
        for emoji, count in counts.items()
    ]
    return SimpleNamespace(results=results)
