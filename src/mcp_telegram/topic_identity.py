"""Pure projection of persisted topic facts into one agent-facing contract."""

from __future__ import annotations

from typing import TypedDict


class TopicTitleIdentity(TypedDict):
    title: str


class TopicIdIdentity(TypedDict):
    topic_id: int


TopicIdentity = TopicTitleIdentity | TopicIdIdentity


TOPIC_IDENTITY_SCHEMA: dict[str, object] = {
    "type": "object",
    "oneOf": [
        {
            "type": "object",
            "properties": {"title": {"type": "string", "minLength": 1}},
            "required": ["title"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"topic_id": {"type": "integer"}},
            "required": ["topic_id"],
            "additionalProperties": False,
        },
    ],
}


def project_topic(*, topic_id: int | None, title: str | None) -> TopicIdentity | None:
    """Project persisted topic facts without exposing Telegram topic mechanics."""
    if isinstance(title, str):
        normalized_title = title.strip()
        if normalized_title:
            return {"title": normalized_title}
    if isinstance(topic_id, int) and not isinstance(topic_id, bool) and topic_id > 0:
        return {"topic_id": topic_id}
    return None


__all__ = ["TOPIC_IDENTITY_SCHEMA", "TopicIdentity", "project_topic"]
