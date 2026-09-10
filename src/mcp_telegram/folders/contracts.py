"""Transport- and storage-neutral folder facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

FOLDER_DIALOG_PAGE_SIZE = 100


class DialogCategory(StrEnum):
    CONTACT = "contact"
    NON_CONTACT = "non_contact"
    BOT = "bot"
    GROUP = "group"
    BROADCAST = "broadcast"
    UNKNOWN = "unknown"


class FolderSourceUnavailableError(Exception):
    """An expected transient failure while reading folder state from Telegram."""


class FolderStagingCorruptError(ValueError):
    """The durable, unpublished folder acquisition state cannot be decoded."""


class FolderStagingStaleError(RuntimeError):
    """The unpublished acquisition was based on a no-longer-current generation."""


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


@dataclass(frozen=True, slots=True)
class FolderDialogCursor:
    """Serializable Telegram dialog-enumeration cursor."""

    offset_date: str | None
    offset_id: int
    offset_peer_type: str | None
    offset_peer_id: int
    offset_peer_access_hash: int


@dataclass(frozen=True, slots=True)
class FolderDialogItem:
    """One dialog fact together with the cursor after that observation."""

    facts: DialogFacts
    cursor: FolderDialogCursor


@dataclass(frozen=True, slots=True)
class FolderStagingSnapshot:
    """Durable source facts that are never exposed before publication."""

    folders: tuple[FolderRule, ...]
    dialogs: tuple[DialogFacts, ...]
    cursor: FolderDialogCursor | None
    started_at: int
    base_generation: int | None = None
