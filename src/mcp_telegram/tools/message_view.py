"""Canonical agent-facing projection for a persisted read message.

This is deliberately a small delivery projector.  It owns the wire shape of a
message shared by the list and inbox tools; it does not fetch data or decide
which messages belong to a result.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TypedDict, cast

from ..entity_identity import ENTITY_IDENTITY_SCHEMA, EntityIdentity, project_entity_identity
from ..formatter import _compute_inline_markers
from ..models import DialogType, ReadMessage, ReadState
from ..topic_identity import TOPIC_IDENTITY_SCHEMA, project_topic
from .structured import (
    MEDIA_OUTPUT_SCHEMA,
    TELEGRAM_CONTENT_OUTPUT_SCHEMA,
    serialize_message_content,
    telegram_content,
)


class ReadMarker(TypedDict):
    """One read-boundary fact attached to its anchor message."""

    kind: str
    label: str
    side: str
    anchor_message_id: int


READ_MARKER_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["i_read_up_to_here", "unread_by_me", "peer_read_up_to_here", "unread_by_peer"],
        },
        "label": {
            "type": "string",
            "enum": ["[I read up to here]", "[unread by me]", "[peer read up to here]", "[unread by peer]"],
        },
        "side": {"type": "string", "enum": ["inbox", "outbox"]},
        "anchor_message_id": {"type": "integer"},
    },
    "required": ["kind", "label", "side", "anchor_message_id"],
    "additionalProperties": False,
}


MESSAGE_VIEW_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "msg_id": {"type": "integer"},
        "sent_at": {"type": "integer"},
        "sender": ENTITY_IDENTITY_SCHEMA,
        "out": {"type": "boolean"},
        "is_service": {"type": "boolean"},
        "topic": TOPIC_IDENTITY_SCHEMA,
        "content": TELEGRAM_CONTENT_OUTPUT_SCHEMA,
        "media": MEDIA_OUTPUT_SCHEMA,
        "reply_context_ref": {
            "type": "object",
            "properties": {
                "msg_id": {"type": "integer"},
                "in_page": {"type": "boolean"},
                "context_included": {"type": "boolean"},
            },
            "required": ["msg_id", "in_page", "context_included"],
            "additionalProperties": False,
        },
        "forward": {
            "type": "object",
            "properties": {"from_name": {"type": "string"}},
            "required": ["from_name"],
            "additionalProperties": False,
        },
        "post_author": {"type": "string"},
        "edit_date": {"type": "integer"},
        "reactions": {
            "type": "object",
            "properties": {
                "display": {"type": "string"},
                "content": TELEGRAM_CONTENT_OUTPUT_SCHEMA,
            },
            "required": ["display", "content"],
            "additionalProperties": False,
        },
        "reaction_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reactor_id": {"type": "integer"},
                    "emoji": {"type": "string"},
                    "reacted_at": {"type": "integer"},
                },
                "required": ["emoji"],
                "additionalProperties": False,
            },
        },
        "reaction_events_status": {"type": "string"},
        "read_at": {"type": "integer"},
        "read_markers": {"type": "array", "items": READ_MARKER_SCHEMA},
    },
    "required": ["dialog_id", "msg_id", "sent_at", "out", "reaction_events", "reaction_events_status"],
    "additionalProperties": False,
}

_READ_MARKER_METADATA = {
    "[I read up to here]": {"kind": "i_read_up_to_here", "side": "inbox"},
    "[unread by me]": {"kind": "unread_by_me", "side": "inbox"},
    "[peer read up to here]": {"kind": "peer_read_up_to_here", "side": "outbox"},
    "[unread by peer]": {"kind": "unread_by_peer", "side": "outbox"},
}


def project_read_markers(
    messages: Sequence[ReadMessage],
    *,
    read_state: ReadState | dict[str, object] | None,
    dialog_type: str | None,
) -> dict[int, ReadMarker]:
    """Return canonical marker payloads keyed by message id."""
    if DialogType.parse(dialog_type) != DialogType.USER:
        return {}
    labels = _compute_inline_markers(list(messages), read_state)
    markers: dict[int, ReadMarker] = {}
    for message_id, label in labels.items():
        metadata = _READ_MARKER_METADATA[label]
        markers[message_id] = {
            "kind": metadata["kind"],
            "label": label,
            "side": metadata["side"],
            "anchor_message_id": message_id,
        }
    return markers


def _project_sender(message: ReadMessage) -> EntityIdentity | None:
    actor_id = message.effective_sender_id
    if actor_id is None:
        actor_id = message.sender_id
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id == 0:
        return None
    return project_entity_identity(
        display_name=message.sender_first_name,
        username=message.sender_username,
        telegram_id=actor_id,
    )


def _reaction_event_payload(event: object) -> dict[str, object]:
    if isinstance(event, Mapping):
        reactor_id = event.get("reactor_id")
        emoji = event.get("emoji")
        reacted_at = event.get("reacted_at")
    else:
        typed_event = cast("object", event)
        reactor_id = getattr(typed_event, "reactor_id", None)
        emoji = getattr(typed_event, "emoji", "")
        reacted_at = getattr(typed_event, "reacted_at", None)
    payload: dict[str, object] = {"emoji": str(emoji)}
    if reactor_id is not None:
        payload["reactor_id"] = reactor_id
    if reacted_at is not None:
        payload["reacted_at"] = reacted_at
    return payload


def _reply_context(message: ReadMessage, *, parent_in_page: bool, context_included: bool) -> dict[str, object] | None:
    if message.reply_to_msg_id is None:
        return None
    return {
        "msg_id": message.reply_to_msg_id,
        "in_page": parent_in_page,
        "context_included": context_included,
    }


def _identity_facts(message: ReadMessage) -> dict[str, object]:
    facts: dict[str, object] = {}
    sender = _project_sender(message)
    if sender is not None:
        facts["sender"] = sender
    if message.is_service:
        facts["is_service"] = True
    topic = project_topic(topic_id=message.forum_topic_id, title=message.topic_title)
    if topic is not None:
        facts["topic"] = topic
    return facts


def _content_facts(message: ReadMessage) -> dict[str, object]:
    projected = serialize_message_content(
        message.text, message.media_description, message.content_kind, message.media_kind
    )
    return {key: value for key, value in projected.items() if value is not None}


def _context_facts(message: ReadMessage, *, parent_in_page: bool, context_included: bool) -> dict[str, object]:
    facts: dict[str, object] = {}
    reply = _reply_context(message, parent_in_page=parent_in_page, context_included=context_included)
    if reply is not None:
        facts["reply_context_ref"] = reply
    if message.fwd_from_name:
        facts["forward"] = {"from_name": message.fwd_from_name}
    if message.post_author is not None:
        facts["post_author"] = message.post_author
    return facts


def _event_facts(message: ReadMessage, *, read_marker: ReadMarker | None) -> dict[str, object]:
    facts: dict[str, object] = {}
    if message.edit_date is not None:
        facts["edit_date"] = message.edit_date
    if message.reactions_display:
        facts["reactions"] = {
            "display": message.reactions_display,
            "content": telegram_content(message.reactions_display, "reaction"),
        }
    if message.read_at is not None:
        facts["read_at"] = message.read_at
    if read_marker is not None:
        facts["read_markers"] = [read_marker]
    return facts


def project_message_view(
    message: ReadMessage,
    *,
    parent_in_page: bool = False,
    context_included: bool = False,
    read_marker: ReadMarker | None = None,
) -> dict[str, object]:
    """Project one :class:`ReadMessage` to the shared MCP message envelope."""
    item: dict[str, object] = {
        "dialog_id": message.dialog_id,
        "msg_id": message.id,
        "sent_at": message.sent_at,
        "out": bool(message.out),
        "reaction_events": [_reaction_event_payload(event) for event in message.reaction_events],
        "reaction_events_status": message.reaction_events_status,
    }

    item.update(_identity_facts(message))
    item.update(_content_facts(message))
    item.update(_context_facts(message, parent_in_page=parent_in_page, context_included=context_included))
    item.update(_event_facts(message, read_marker=read_marker))
    return item


__all__ = ["MESSAGE_VIEW_SCHEMA", "ReadMarker", "project_message_view", "project_read_markers"]
