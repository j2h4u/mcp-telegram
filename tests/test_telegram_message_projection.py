from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

from telethon.tl.types import MessageService

from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict


class _BrokenTimestamp:
    def timestamp(self) -> float:
        raise ValueError("no timestamp")


def _make_message(  # noqa: PLR0913
    *,
    sender_id: int | None = 101,
    out: bool = False,
    sender: object | None = None,
    date: object | None = None,
    edit_date: object | None = None,
    reactions: object | None = None,
    message: object | None = "hello",
    media: object | None = None,
) -> MessageLike:
    return cast(
        MessageLike,
        SimpleNamespace(
            id=1,
            date=date,
            edit_date=edit_date,
            message=message,
            media=media,
            out=out,
            sender_id=sender_id,
            sender=sender,
            reactions=reactions,
        ),
    )


def _make_service_message(**kwargs: object) -> MessageLike:
    """Return a MessageService-like object recognized by is_service_message."""
    msg = MagicMock(spec=MessageService)
    msg.id = kwargs.get("id", 1)
    msg.date = kwargs.get("date")
    msg.edit_date = kwargs.get("edit_date")
    msg.message = kwargs.get("message", "service")
    msg.media = kwargs.get("media")
    msg.out = kwargs.get("out", False)
    msg.sender_id = kwargs.get("sender_id")
    msg.sender = kwargs.get("sender")
    msg.reactions = kwargs.get("reactions")
    # MagicMock(spec=MessageService) reports True for isinstance(..., MessageService),
    # which is exactly what is_service_message checks.
    return cast(MessageLike, msg)


def test_message_to_dict_resolves_effective_sender_for_various_contexts() -> None:
    """effective_sender_id must reflect service flag, DM direction, and raw sender."""
    sender = SimpleNamespace(first_name="Alice", title=None)

    raw_sender = message_to_dict(
        _make_message(sender_id=77, out=False, sender=sender),
        dialog_id=42,
        self_id=100,
    )
    assert raw_sender["effective_sender_id"] == 77

    service = message_to_dict(
        _make_service_message(sender_id=None, out=False),
        dialog_id=42,
        self_id=100,
    )
    assert service["effective_sender_id"] is None

    outgoing_dm = message_to_dict(
        _make_message(sender_id=None, out=True),
        dialog_id=7,
        self_id=100,
    )
    assert outgoing_dm["effective_sender_id"] == 100

    incoming_dm = message_to_dict(
        _make_message(sender_id=None, out=False),
        dialog_id=7,
        self_id=100,
    )
    assert incoming_dm["effective_sender_id"] == 7

    group_without_sender = message_to_dict(
        _make_message(sender_id=None, out=False),
        dialog_id=-1007,
        self_id=100,
    )
    assert group_without_sender["effective_sender_id"] is None

    missing_self = message_to_dict(
        _make_message(sender_id=None, out=True),
        dialog_id=7,
        self_id=None,
    )
    assert missing_self["effective_sender_id"] is None


def test_message_to_dict_falls_back_when_sender_or_timestamp_is_broken() -> None:
    """Robust projection must survive missing sender info and broken timestamps."""
    sender_with_title = SimpleNamespace(first_name=None, title="Channel Name")
    with_title = message_to_dict(
        _make_message(sender_id=1, sender=sender_with_title),
        dialog_id=1,
        self_id=100,
    )
    assert with_title["sender_first_name"] == "Channel Name"

    no_sender = message_to_dict(
        _make_message(sender_id=1, sender=None),
        dialog_id=1,
        self_id=100,
    )
    assert no_sender["sender_first_name"] is None

    broken_date = message_to_dict(
        _make_message(sender_id=1, date=_BrokenTimestamp()),
        dialog_id=1,
        self_id=100,
    )
    assert broken_date["sent_at"] == 0

    broken_edit = message_to_dict(
        _make_message(sender_id=1, edit_date=_BrokenTimestamp()),
        dialog_id=1,
        self_id=100,
    )
    assert broken_edit["edit_date"] is None


def test_message_to_dict_skips_reactions_without_emoticon() -> None:
    """Only reactions that expose an emoticon string contribute to reactions_display."""
    reactions_with_gaps = SimpleNamespace(
        results=[
            SimpleNamespace(reaction=SimpleNamespace(emoticon="👍"), count=2),
            SimpleNamespace(reaction=SimpleNamespace(emoticon=None, document_id=12345), count=1),
            SimpleNamespace(reaction=None, count=5),
        ]
    )
    result = message_to_dict(
        _make_message(sender_id=1, reactions=reactions_with_gaps),
        dialog_id=1,
        self_id=100,
    )
    reactions_display = cast(str, result["reactions_display"])
    assert "👍" in reactions_display
    assert "12345" not in reactions_display
