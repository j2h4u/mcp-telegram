"""Telethon message projection for uncached Telegram gateway reads."""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Sequence
from typing import Protocol, cast

from .formatter import format_reaction_counts
from .media_fact import encode_media_payload
from .message_composition import decode_formatting_entities, decode_service_action, extract_message_composition
from .message_content import MessageSnapshot, project_message_content
from .messages.telegram_adapter import (
    extract_entity_rows,
    extract_fwd_row,
    extract_message_text,
    extract_reply_and_topic,
)
from .reactions.projection import project_reaction_aggregates, project_reaction_event
from .telethon_media import extract_media_fact
from .telethon_message import is_service_message

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


def _extract_reaction_facts(msg: MessageLike) -> dict[str, object]:
    events = tuple(
        dataclasses.asdict(project_reaction_event(item))
        for item in cast(Sequence[object], getattr(msg.reactions, "recent_reactions", ()) or ())
    )
    return {
        "reactions_display": format_reaction_counts(
            [(aggregate.emoji, aggregate.count) for aggregate in project_reaction_aggregates(msg.reactions)]
        ),
        "reaction_events": events,
        "reaction_events_status": "partial" if events else "unavailable",
    }


def _extract_forward_facts(msg: MessageLike, dialog_id: int | None) -> dict[str, object]:
    forward = extract_fwd_row(dialog_id or 0, msg.id, msg)
    return {
        "fwd_from_name": forward.fwd_from_name if forward is not None else None,
        "post_author": getattr(msg, "post_author", None),
    }


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


def message_to_dict(  # noqa: PLR0914
    msg: MessageLike,
    dialog_id: int | None = None,
    self_id: int | None = None,
) -> dict[str, object]:
    """Convert a Telethon message object to the standard message dict."""
    sender_first_name = _extract_sender_first_name(msg)
    sent_at = _timestamp_to_int(msg.date, msg_id=msg.id)
    raw_media = msg.media
    fact = extract_media_fact(raw_media)
    media_kind_value = None if fact is None else fact.kind
    media_payload = encode_media_payload(fact)
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
    content = project_message_content(
        MessageSnapshot(
            text=extract_message_text(msg),
            media_kind=media_kind_value,
            media_payload=media_payload,
            text_links=tuple(text_links),
        )
    )
    entities_json, action_json = extract_message_composition(msg)
    entities = decode_formatting_entities(entities_json)
    return {
        "formatting_entities": None if entities is None else tuple(entities),
        "formatting_text": extract_message_text(msg) if entities is not None else None,
        "service_action": decode_service_action(action_json),
        "message_id": msg.id,
        "sent_at": sent_at,
        "text": content.text,
        "sender_id": raw_sender_id,
        "sender_first_name": sender_first_name,
        "media_description": content.media_description,
        "media_kind": media_kind_value,
        "content_kind": content.kind,
        "reply_to_msg_id": reply_to_msg_id,
        "forum_topic_id": forum_topic_id,
        **_extract_reaction_facts(msg),
        **_extract_forward_facts(msg, dialog_id),
        "is_deleted": 0,
        "edit_date": edit_date,
        "effective_sender_id": effective_sender_id,
        "is_service": is_service_flag,
        "out": out_flag,
        "dialog_id": dialog_id,
    }
