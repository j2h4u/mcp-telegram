"""Message serialization helpers for daemon API responses."""

import logging
import sqlite3
from typing import Any

from .formatter import format_reaction_counts
from .sync_worker import extract_reply_and_topic

logger = logging.getLogger(__name__)


def _extract_sender_first_name(msg: Any) -> str | None:
    sender = getattr(msg, "sender", None)
    return getattr(sender, "first_name", None) if sender is not None else None


def _timestamp_to_int(value: Any, *, msg_id: Any = None) -> int:
    if value is None:
        return 0
    try:
        return int(value.timestamp())
    except Exception:
        logger.debug(
            "message_to_dict timestamp conversion failed msg_id=%s",
            msg_id if msg_id is not None else "?",
            exc_info=True,
        )
        return 0


def _get_media_description(msg: Any) -> str | None:
    media = getattr(msg, "media", None)
    if media is None:
        return None
    from .formatter import _describe_media

    return _describe_media(media)


def _extract_reactions_display(msg: Any) -> str:
    reactions_obj = getattr(msg, "reactions", None)
    if reactions_obj is None:
        return ""

    results_list = getattr(reactions_obj, "results", None) or []
    counts: list[tuple[str, int]] = []
    for item in results_list:
        reaction = getattr(item, "reaction", None)
        emoticon = getattr(reaction, "emoticon", None) if reaction else None
        if emoticon is not None:
            counts.append((emoticon, int(getattr(item, "count", 0))))

    return format_reaction_counts(counts)


def _to_unix_timestamp_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value.timestamp())
    except TypeError, ValueError, AttributeError:
        return None


def _is_service_message(msg: Any) -> int:
    try:
        from telethon.tl import types as _tl_types  # type: ignore[import-untyped]

        return 1 if isinstance(msg, _tl_types.MessageService) else 0
    except Exception:
        logger.debug(
            "message_to_dict: telethon MessageService isinstance check failed",
            exc_info=True,
        )
        return 0


def _resolve_effective_sender_id(
    raw_sender_id: int | None,
    dialog_id: int | None,
    self_id: int | None,
    is_service_flag: int,
    out_flag: int,
) -> int | None:
    if raw_sender_id is not None:
        return raw_sender_id
    if is_service_flag == 1:
        return None
    if dialog_id is not None and dialog_id > 0 and out_flag == 1 and self_id is not None:
        return self_id
    if dialog_id is not None and dialog_id > 0 and out_flag == 0:
        return dialog_id
    return None


def message_to_dict(
    msg: Any,
    dialog_id: int | None = None,
    self_id: int | None = None,
) -> dict[str, Any]:
    """Convert a Telethon message object to the standard message dict."""
    sender_first_name = _extract_sender_first_name(msg)
    sent_at = _timestamp_to_int(getattr(msg, "date", None), msg_id=getattr(msg, "id", None))
    media_description = _get_media_description(msg)
    reactions_display = _extract_reactions_display(msg)
    reply_to_msg_id, forum_topic_id = extract_reply_and_topic(msg)
    edit_date = _to_unix_timestamp_or_none(getattr(msg, "edit_date", None))
    is_service_flag = _is_service_message(msg)
    out_flag = 1 if getattr(msg, "out", False) else 0
    raw_sender_id = getattr(msg, "sender_id", None)
    effective_sender_id = _resolve_effective_sender_id(
        raw_sender_id=raw_sender_id,
        dialog_id=dialog_id,
        self_id=self_id,
        is_service_flag=is_service_flag,
        out_flag=out_flag,
    )

    return {
        "message_id": msg.id,
        "sent_at": sent_at,
        "text": getattr(msg, "message", None),
        "sender_id": raw_sender_id,
        "sender_first_name": sender_first_name,
        "media_description": media_description,
        "reply_to_msg_id": reply_to_msg_id,
        "forum_topic_id": forum_topic_id,
        "reactions_display": reactions_display,
        "is_deleted": 0,
        "edit_date": edit_date,
        "effective_sender_id": effective_sender_id,
        "is_service": is_service_flag,
        "out": out_flag,
        "dialog_id": dialog_id,
    }


def fetch_reaction_counts(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: list[int],
) -> dict[int, list[tuple[str, int]]]:
    """Return `{message_id: [(emoji, count), ...]}` for the given page."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT message_id, emoji, count FROM message_reactions "
        f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
        f"ORDER BY count DESC, emoji",
        [dialog_id, *message_ids],
    ).fetchall()
    result: dict[int, list[tuple[str, int]]] = {}
    for msg_id, emoji, count in rows:
        result.setdefault(int(msg_id), []).append((emoji, int(count)))
    return result
