"""Transport- and storage-neutral folder facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class DialogCategory(StrEnum):
    CONTACT = "contact"
    NON_CONTACT = "non_contact"
    BOT = "bot"
    GROUP = "group"
    BROADCAST = "broadcast"
    UNKNOWN = "unknown"


class FolderSourceUnavailableError(Exception):
    """An expected transient failure while reading folder state from Telegram."""


@dataclass(frozen=True, slots=True)
class FolderRule:
    folder_id: int
    title: str
    included_ids: frozenset[int] = frozenset()
    pinned_ids: frozenset[int] = frozenset()
    excluded_ids: frozenset[int] = frozenset()
    categories: frozenset[DialogCategory] = frozenset()
    exclude_archived: bool = False
    exclude_read: bool = False
    exclude_muted: bool = False
    explicit_only: bool = False


@dataclass(frozen=True, slots=True)
class DialogFacts:
    dialog_id: int
    category: DialogCategory
    archived: bool = False
    unread: bool = False
    muted: bool = False


@dataclass(frozen=True, slots=True)
class FolderSourceSnapshot:
    folders: tuple[FolderRule, ...]
    dialogs: tuple[DialogFacts, ...]
