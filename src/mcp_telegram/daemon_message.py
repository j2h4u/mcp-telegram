"""Message serialization helpers for daemon API responses."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from .formatter import format_reaction_counts
from .sync_worker import extract_reply_and_topic

logger = logging.getLogger(__name__)


def message_to_dict(
    msg: Any,
    dialog_id: int | None = None,
    self_id: int | None = None,
) -> dict[str, Any]:
    """Convert a Telethon message object to the standard message dict."""
    sender_first_name: str | None = None
    if getattr(msg, "sender", None) is not None:
        sender_first_name = getattr(msg.sender, "first_name", None)
    sent_at = 0
    if getattr(msg, "date", None) is not None:
        try:
            sent_at = int(msg.date.timestamp())
        except Exception:
            logger.debug(
                "message_to_dict timestamp conversion failed msg_id=%s",
                getattr(msg, "id", "?"),
                exc_info=True,
            )
            sent_at = 0

    media = getattr(msg, "media", None)
    media_description: str | None = None
    if media is not None:
        from .formatter import _describe_media

        media_description = _describe_media(media)

    reactions_obj = getattr(msg, "reactions", None)
    reactions_display = ""
    if reactions_obj is not None:
        results_list = getattr(reactions_obj, "results", None) or []
        counts: list[tuple[str, int]] = []
        for item in results_list:
            reaction = getattr(item, "reaction", None)
            emoticon = getattr(reaction, "emoticon", None) if reaction else None
            count = getattr(item, "count", 0)
            if emoticon is not None:
                counts.append((emoticon, int(count)))
        reactions_display = format_reaction_counts(counts)

    reply_to_msg_id, forum_topic_id = extract_reply_and_topic(msg)

    edit_date_raw = getattr(msg, "edit_date", None)
    edit_date: int | None = None
    if edit_date_raw is not None:
        try:
            edit_date = int(edit_date_raw.timestamp())
        except TypeError, ValueError, AttributeError:
            edit_date = None

    # Mirror the SQL EFFECTIVE_SENDER_ID_SQL CASE tree in Python so fallback
    # rows keep the same discriminator behavior.
    is_service_flag = 0
    try:
        from telethon.tl import types as _tl_types  # type: ignore[import-untyped]

        if isinstance(msg, _tl_types.MessageService):
            is_service_flag = 1
    except Exception:
        logger.debug("message_to_dict: telethon MessageService isinstance check failed", exc_info=True)

    out_flag = 1 if getattr(msg, "out", False) else 0

    raw_sender_id = getattr(msg, "sender_id", None)
    effective_sender_id: int | None
    if raw_sender_id is not None:
        effective_sender_id = raw_sender_id
    elif is_service_flag == 1:
        effective_sender_id = None
    elif dialog_id is not None and dialog_id > 0 and out_flag == 1 and self_id is not None:
        effective_sender_id = self_id
    elif dialog_id is not None and dialog_id > 0 and out_flag == 0:
        effective_sender_id = dialog_id
    else:
        effective_sender_id = None

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
