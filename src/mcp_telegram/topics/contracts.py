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


def is_topic_capable(entity: object) -> bool:
    """Whether Telegram exposes forum topics for this dialog entity.

    In addition to forum supergroups, Telegram has private bot dialogs which
    opt into a forum-style view via ``bot_forum_view``.
    """
    return bool(getattr(entity, "forum", False)) or (
        bool(getattr(entity, "bot", False)) and bool(getattr(entity, "bot_forum_view", False))
    )
