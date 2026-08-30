"""Canonical discovery projection for one persisted message search hit.

Search hits are compact navigation aids, not message delivery views.  This
module owns their public wire shape and keeps snippets bounded around the
query while preserving the coordinates needed to open sent history.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import cast

from ..formatter import resolve_sender_label
from ..models import ReadReactionEvent
from ..topic_identity import TOPIC_IDENTITY_SCHEMA, project_topic
from .structured import MEDIA_OUTPUT_SCHEMA, serialize_message_content

_SNIPPET_MAX_LEN = 150
_SNIPPET_LEAD = 50


SEARCH_HIT_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "dialog_id": {"type": "integer"},
        "dialog_name": {"type": ["string", "null"]},
        "msg_id": {"type": "integer"},
        # Keep the canonical Unix moment internally; shared temporal projection
        # renders it as ISO-8601 in the requested response timezone.
        "date": {"type": ["integer", "null"]},
        "sender": {"type": ["string", "null"]},
        "topic": TOPIC_IDENTITY_SCHEMA,
        "content": {"type": ["object", "null"]},
        "media": MEDIA_OUTPUT_SCHEMA,
        "anchor_call": {"type": "object"},
        "message_state": {"type": "string", "enum": ["sent", "scheduled"]},
        "visibility": {
            "type": "string",
            "description": "author_only before publication, chat_visible after publication.",
        },
        "unpublished": {"type": "boolean"},
        "published": {"type": "boolean"},
        "unseen": {"type": "boolean"},
        "scheduled_at": {"type": ["integer", "null"]},
        "published_at": {"type": ["integer", "null"]},
        "inclusion_basis": {"type": "array", "items": {"type": "string"}},
        "reaction_events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "reactor_id": {"type": ["integer", "null"]},
                    "emoji": {"type": "string"},
                    "reacted_at": {"type": ["integer", "null"]},
                },
                "required": ["reactor_id", "emoji", "reacted_at"],
                "additionalProperties": False,
            },
        },
        "reaction_events_status": {"type": "string"},
        "read_at": {"type": ["integer", "null"]},
    },
    "required": [
        "dialog_id",
        "dialog_name",
        "msg_id",
        "anchor_call",
        "message_state",
        "visibility",
        "unpublished",
        "published",
        "unseen",
        "scheduled_at",
        "published_at",
        "inclusion_basis",
        "reaction_events",
        "reaction_events_status",
        "read_at",
    ],
    "additionalProperties": False,
}


def extract_search_snippet(text: str | None, query: str) -> str:
    """Return a bounded excerpt centred on the first literal query-word match."""
    if not text:
        return "(no text)"
    if len(text) <= _SNIPPET_MAX_LEN:
        return text

    for word in query.split():
        position = text.lower().find(word.lower())
        if position >= 0:
            start = max(0, position - _SNIPPET_LEAD)
            end = min(len(text), start + _SNIPPET_MAX_LEN)
            snippet = text[start:end]
            prefix = "..." if start > 0 else ""
            suffix = "..." if end < len(text) else ""
            return f"{prefix}{snippet}{suffix}"

    return text[:_SNIPPET_MAX_LEN] + "..."


def _search_date(row: Mapping[str, object]) -> int | None:
    sent_at = row.get("sent_at")
    return int(cast("int", sent_at)) if sent_at is not None else None


def _anchor_call(dialog_id: int, msg_id: int, message_state: str) -> dict[str, object]:
    arguments: dict[str, object] = {"exact_dialog_id": dialog_id}
    if message_state == "scheduled":
        arguments["message_state"] = "scheduled"
    else:
        arguments["anchor_message_id"] = msg_id
    return {"tool": "list_messages", "arguments": arguments}


def _reaction_event_payload(event: object) -> dict[str, object]:
    if isinstance(event, Mapping):
        return {
            "reactor_id": event.get("reactor_id"),
            "emoji": event.get("emoji"),
            "reacted_at": event.get("reacted_at"),
        }
    typed_event = cast("ReadReactionEvent", event)
    return {
        "reactor_id": typed_event.reactor_id,
        "emoji": typed_event.emoji,
        "reacted_at": typed_event.reacted_at,
    }


def _optional_search_facts(row: dict[str, object], snippet: str) -> dict[str, object]:
    facts: dict[str, object] = {}
    topic_id = row.get("forum_topic_id")
    title = row.get("topic_title")
    topic = project_topic(
        topic_id=topic_id if isinstance(topic_id, int) and not isinstance(topic_id, bool) else None,
        title=title if isinstance(title, str) else None,
    )
    media_description = row.get("media_description")
    media_kind = row.get("media_kind")
    projected = serialize_message_content(
        snippet,
        media_description if isinstance(media_description, str) else None,
        "snippet",
        media_kind if isinstance(media_kind, str) else None,
    )
    if projected["content"] is not None:
        facts["content"] = projected["content"]
    if projected["media"] is not None:
        facts["media"] = projected["media"]
    if topic is not None:
        facts["topic"] = topic
    return facts


def project_search_hit(
    row: dict[str, object],
    query: str,
    *,
    lifecycle: Mapping[str, object],
) -> dict[str, object]:
    """Project one daemon search row to the compact SearchHit wire contract."""
    text = row.get("text")
    snippet = extract_search_snippet(text if isinstance(text, str) else None, query)
    dialog_id = int(cast("int", row.get("dialog_id") or 0))
    msg_id = cast("int", row["message_id"])
    message_state = str(lifecycle["message_state"])
    result: dict[str, object] = {
        "dialog_id": dialog_id,
        "dialog_name": row.get("dialog_name"),
        "msg_id": msg_id,
        "date": _search_date(row),
        "sender": resolve_sender_label(row),
        "anchor_call": _anchor_call(dialog_id, msg_id, message_state),
        "message_state": lifecycle["message_state"],
        "visibility": lifecycle["visibility"],
        "unpublished": lifecycle["unpublished"],
        "published": lifecycle["published"],
        "unseen": lifecycle["unseen"],
        "scheduled_at": lifecycle["scheduled_at"],
        "published_at": lifecycle["published_at"],
        "inclusion_basis": lifecycle["inclusion_basis"],
        "reaction_events": [
            _reaction_event_payload(event) for event in cast("Iterable[object]", row.get("reaction_events") or ())
        ],
        "reaction_events_status": row.get("reaction_events_status", "unavailable"),
        "read_at": row.get("read_at"),
    }
    result.update(_optional_search_facts(row, snippet))
    return result
