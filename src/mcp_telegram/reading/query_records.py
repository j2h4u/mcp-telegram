"""Canonical SQLite row decoders for the reading projection."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

from ..models import ContentKind, ReadMessage


def _row_value(row: object, key: str, default: object | None = None) -> object | None:
    try:
        return cast(object | None, row[key])  # type: ignore[index]
    except AttributeError, IndexError, KeyError, TypeError:
        return default


def _coerce_int(value: object | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(cast(int | str, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer value: {value!r}") from exc


def _coerce_optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    return _coerce_int(value, 0)


def read_message_from_row(row: Mapping[str, object] | object, *, reactions_display: str = "") -> ReadMessage:
    """Decode any reading SELECT row into the one canonical message record."""
    return ReadMessage(
        message_id=_coerce_int(_row_value(row, "message_id"), 0),
        sent_at=_coerce_int(_row_value(row, "sent_at"), 0),
        dialog_id=_coerce_int(_row_value(row, "dialog_id"), 0),
        text=cast(str | None, _row_value(row, "text")),
        sender_id=_coerce_optional_int(_row_value(row, "sender_id")),
        sender_first_name=cast(str | None, _row_value(row, "sender_first_name")),
        sender_username=cast(str | None, _row_value(row, "sender_username")),
        media_description=cast(str | None, _row_value(row, "media_description")),
        media_kind=cast(str | None, _row_value(row, "media_kind")),
        content_kind=cast(ContentKind, _row_value(row, "content_kind") or "none"),
        reply_to_msg_id=_coerce_optional_int(_row_value(row, "reply_to_msg_id")),
        forum_topic_id=_coerce_optional_int(_row_value(row, "forum_topic_id")),
        is_deleted=_coerce_int(_row_value(row, "is_deleted", 0), 0),
        deleted_at=_coerce_optional_int(_row_value(row, "deleted_at")),
        edit_date=_coerce_optional_int(_row_value(row, "edit_date")),
        topic_title=cast(str | None, _row_value(row, "topic_title")),
        effective_sender_id=_coerce_optional_int(_row_value(row, "effective_sender_id")),
        is_service=_coerce_int(_row_value(row, "is_service", 0), 0),
        out=_coerce_int(_row_value(row, "out", 0), 0),
        fwd_from_name=cast(str | None, _row_value(row, "fwd_from_name")),
        post_author=cast(str | None, _row_value(row, "post_author")),
        read_at=_coerce_optional_int(_row_value(row, "read_at")),
        reactions_display=reactions_display,
        dialog_name=cast(str | None, _row_value(row, "dialog_name")),
    )
