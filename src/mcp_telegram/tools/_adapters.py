"""Shared adapters: daemon row dict -> MessageLike-compatible objects.

Used by reading.py and unread.py to bridge daemon API dicts to
format_messages() which expects the MessageLike protocol.
"""
from __future__ import annotations

from datetime import datetime, timezone


class DaemonMessage:
    """Lightweight MessageLike adapter for daemon API row dicts.

    format_messages() accesses: .id, .date, .message, .sender, .reply_to,
    .reactions, .media, and optionally .edit_date / .topic_title.
    """

    __slots__ = (
        "id", "date", "message", "sender", "reply_to", "reactions", "media",
        "edit_date", "topic_title", "dialog_name",
    )

    def __init__(self, row: dict) -> None:
        self.id: int = row["message_id"]
        sent_at = row.get("sent_at") or 0
        self.date = datetime.fromtimestamp(int(sent_at), tz=timezone.utc)
        self.message: str | None = row.get("text")
        sender_name = row.get("sender_first_name")
        self.sender = Sender(sender_name) if sender_name else None
        reply_id = row.get("reply_to_msg_id")
        self.reply_to = ReplyHeader(reply_id) if reply_id else None
        self.reactions = row.get("reactions")  # JSON string or None
        media_desc = row.get("media_description")
        self.media = MediaPlaceholder(media_desc) if media_desc else None
        edit_date_raw = row.get("edit_date")
        if edit_date_raw is not None:
            self.edit_date: datetime | None = datetime.fromtimestamp(
                int(edit_date_raw), tz=timezone.utc
            )
        else:
            self.edit_date = None
        self.topic_title: str | None = row.get("topic_title")
        self.dialog_name: str | None = row.get("dialog_name")


class Sender:
    __slots__ = ("first_name",)

    def __init__(self, name: str | None) -> None:
        self.first_name = name


class ReplyHeader:
    __slots__ = ("reply_to_msg_id",)

    def __init__(self, msg_id: int | None) -> None:
        self.reply_to_msg_id = msg_id


class MediaPlaceholder:
    """Wraps a pre-formatted media description string from sync.db."""

    __slots__ = ("_description",)

    def __init__(self, description: str) -> None:
        self._description = description

    def __str__(self) -> str:
        return self._description
