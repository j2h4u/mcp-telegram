"""Shared adapters: daemon row dict -> MessageLike-compatible objects.

Used by reading.py and unread.py to bridge daemon API dicts to
format_messages() which expects the MessageLike protocol.
"""

from datetime import datetime, timezone


class _PreformattedReactions:
    """Carry pre-formatted reaction text through format_messages -> _format_reactions.

    When daemon_api returns reactions_display as a pre-formatted string
    (e.g. "[👍×3 ❤️×1]"), this wrapper makes it compatible with
    _format_reactions which normally expects a Telethon reactions object.
    _format_reactions detects the _display attribute and returns it directly.

    Protocol contract:
    - Has a `_display` attribute (str) containing the formatted reaction text
    - `_format_reactions` in formatter.py checks `getattr(reactions, "_display", None)`
    - If `_display` is not None, returns it directly without Telethon parsing
    - If `_display` is empty string, returns "" (no reactions shown)

    Temporary infrastructure. Concrete removal criterion: remove this class
    when `reaction_names_map` field is removed from the `MessageLike` protocol
    in models.py. At that point, the Telethon _format_reactions path is also
    gone, and DaemonMessage can carry reactions_display as a plain string
    without needing protocol compatibility.
    """

    __slots__ = ("_display",)

    def __init__(self, display: str) -> None:
        self._display = display


class DaemonMessage:
    """Lightweight MessageLike adapter for daemon API row dicts.

    format_messages() accesses: .id, .date, .message, .sender, .reply_to,
    .reactions, .media, and optionally .edit_date / .topic_title.
    """

    __slots__ = (
        "id", "date", "message", "sender", "reply_to", "reactions", "media",
        "edit_date", "topic_title", "dialog_name",
        # Phase 39.1-02: direction + service discriminators for DM rendering.
        # effective_sender_id collapses DM direction to a concrete user id;
        # is_service flags MessageService rows so formatter renders "System".
        "sender_id", "effective_sender_id", "is_service", "out", "dialog_id",
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
        reactions_display = row.get("reactions_display", "")
        self.reactions = _PreformattedReactions(reactions_display) if reactions_display else None
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
        self.sender_id: int | None = row.get("sender_id")
        self.effective_sender_id: int | None = row.get("effective_sender_id")
        self.is_service: int = int(row.get("is_service") or 0)
        self.out: int = int(row.get("out") or 0)
        self.dialog_id: int | None = row.get("dialog_id")


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
