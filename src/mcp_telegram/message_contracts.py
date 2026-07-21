"""Neutral message persistence contracts."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredMessage:
    """Row shape for INSERT OR REPLACE INTO messages.

    Field names are the single source of truth for column names and INSERT SQL.
    is_deleted is always 0 at insert time (hardcoded in INSERT_MESSAGE_SQL).
    """

    dialog_id: int
    message_id: int
    sent_at: int
    text: str | None
    sender_id: int | None
    sender_first_name: str | None
    media_description: str | None
    reply_to_msg_id: int | None
    forum_topic_id: int | None
    edit_date: int | None
    grouped_id: int | None
    reply_to_peer_id: int | None
    out: int
    is_service: int
    post_author: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ReactionRecord:
    """One row in message_reactions."""

    dialog_id: int
    message_id: int
    emoji: str
    count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class EntityRecord:
    """One row in message_entities."""

    dialog_id: int
    message_id: int
    offset: int
    length: int
    type: str
    value: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ForwardRecord:
    """One row in message_forwards."""

    dialog_id: int
    message_id: int
    fwd_from_peer_id: int | None
    fwd_from_name: str | None
    fwd_date: int | None
    fwd_channel_post: int | None


@dataclass
class ExtractedMessage:
    """Bundle of extracted rows for atomic multi-table insert."""

    message: StoredMessage
    reply_count: int
    reactions: list[ReactionRecord] = field(default_factory=list)
    entities: list[EntityRecord] = field(default_factory=list)
    forward: ForwardRecord | None = None
