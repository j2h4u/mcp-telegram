"""Transport- and storage-neutral topic facts."""

from __future__ import annotations

from dataclasses import dataclass


class TopicSourceUnavailableError(Exception):
    """An expected non-flood failure while reading topics from Telegram."""


@dataclass(frozen=True, slots=True)
class TopicFact:
    topic_id: int
    title: str
    icon_emoji_id: int | None = None
    icon_emoji: str | None = None
    icon_color: int | None = None
    date: int | None = None
    is_general: bool = False
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class TopicCursor:
    offset_date: int | None = None
    offset_id: int = 0
    offset_topic: int = 0


@dataclass(frozen=True, slots=True)
class TopicPage:
    topics: tuple[TopicFact, ...]
    count: int
    order_by_create_date: bool
    next_cursor: TopicCursor | None
    deleted_ids: tuple[int, ...] = ()


def is_topic_capable(entity: object) -> bool:
    """Whether Telegram exposes forum topics for this dialog entity.

    In addition to forum supergroups, Telegram has private bot dialogs which
    opt into a forum-style view via ``bot_forum_view``.
    """
    return bool(getattr(entity, "forum", False)) or (
        bool(getattr(entity, "bot", False)) and bool(getattr(entity, "bot_forum_view", False))
    )
