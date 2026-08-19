"""Telethon message projection for uncached Telegram gateway reads."""

from __future__ import annotations

import logging
from typing import Protocol

from .formatter import format_reaction_counts
from .messages.telegram_adapter import extract_entity_rows, extract_reply_and_topic
from .telethon_media import describe_media
from .telethon_message import is_service_message
from .text_projection import render_text_links

logger = logging.getLogger(__name__)


class SupportsTimestamp(Protocol):
    def timestamp(self) -> float: ...


class MessageSender(Protocol):
    first_name: str | None


class Reaction(Protocol):
    emoticon: str | None


class ReactionItem(Protocol):
    count: int | float | str
    reaction: Reaction | None


class ReactionResult(Protocol):
    results: list[ReactionItem] | None


class MessageLike(Protocol):
    id: int
    date: SupportsTimestamp | None
    edit_date: SupportsTimestamp | None
    message: object | None
    media: object | None
    out: bool
    sender_id: int | None
    sender: MessageSender | None
    reactions: ReactionResult | None


def _first_non_empty_str(*values: object) -> str | None:
    for value in values:
        if isinstance(value, str) and value != "":
            return value
    return None


def _extract_sender_first_name(msg: MessageLike) -> str | None:
    sender = msg.sender
    if sender is None:
        return None
    return _first_non_empty_str(getattr(sender, "first_name", None), getattr(sender, "title", None))


def _timestamp_to_int(value: SupportsTimestamp | None, *, msg_id: object = None) -> int:
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


def _get_media_description(msg: MessageLike) -> str | None:
    media = msg.media
    if media is None:
        return None
    return describe_media(media)


def _extract_reactions_display(msg: MessageLike) -> str:
    reactions_obj = msg.reactions
    if reactions_obj is None:
        return ""

    results_list: list[ReactionItem] = reactions_obj.results or []
    counts: list[tuple[str, int]] = []
    for item in results_list:
        reaction = item.reaction
        emoticon = reaction.emoticon if reaction is not None else None
        if emoticon is not None:
            counts.append((emoticon, int(item.count)))

    return format_reaction_counts(counts)


def _to_unix_timestamp_or_none(value: SupportsTimestamp | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.timestamp())
    except TypeError, ValueError, AttributeError:
        return None


def _is_service_message(msg: MessageLike) -> int:
    return 1 if is_service_message(msg) else 0


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
    msg: MessageLike,
    dialog_id: int | None = None,
    self_id: int | None = None,
) -> dict[str, object]:
    """Convert a Telethon message object to the standard message dict."""
    sender_first_name = _extract_sender_first_name(msg)
    sent_at = _timestamp_to_int(msg.date, msg_id=msg.id)
    media_description = _get_media_description(msg)
    reactions_display = _extract_reactions_display(msg)
    reply_to_msg_id, forum_topic_id = extract_reply_and_topic(msg)
    edit_date = _to_unix_timestamp_or_none(msg.edit_date)
    is_service_flag = _is_service_message(msg)
    out_flag = 1 if msg.out else 0
    raw_sender_id = msg.sender_id
    effective_sender_id = _resolve_effective_sender_id(
        raw_sender_id=raw_sender_id,
        dialog_id=dialog_id,
        self_id=self_id,
        is_service_flag=is_service_flag,
        out_flag=out_flag,
    )

    text_links = [
        (entity.offset, entity.length, entity.value)
        for entity in extract_entity_rows(dialog_id or 0, msg.id, msg)
        if entity.type == "text_url" and entity.value is not None
    ]
    return {
        "message_id": msg.id,
        "sent_at": sent_at,
        "text": render_text_links(msg.message if isinstance(msg.message, str) else None, text_links),
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
