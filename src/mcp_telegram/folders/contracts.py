"""Folder-rule contracts independent from Telegram directory acquisition."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

RULE_TTL_SECONDS = 900
DEFAULT_FOLDER_NAMESPACE = "default"
FILTER_FOLDER_NAMESPACE = "filter"


class DialogCategory(StrEnum):
    CONTACT = "contact"
    NON_CONTACT = "non_contact"
    BOT = "bot"
    GROUP = "group"
    BROADCAST = "broadcast"


class FolderRuleKind(StrEnum):
    FILTER = "filter"
    CHATLIST = "chatlist"
    DEFAULT = "default"


class MembershipState(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class FolderSourceUnavailableError(Exception):
    """A transient failure while observing Telegram folder rules."""


@dataclass(frozen=True, slots=True)
class DialogFacts:
    """One current canonical eligibility record; ``None`` means unknown."""

    dialog_id: int
    category: DialogCategory | None = None
    archived: bool | None = None
    unread: bool | None = None
    mute_until: int | None = None
    observed_at: int | None = None


@dataclass(frozen=True, slots=True)
class FolderRule:
    """A ``GetDialogFilters`` constructor with the exact supplied order."""

    folder_id: int
    title: str
    namespace: str = FILTER_FOLDER_NAMESPACE
    kind: FolderRuleKind = FolderRuleKind.FILTER
    source_position: int = 0
    included_ids: tuple[int, ...] = ()
    pinned_ids: tuple[int, ...] = ()
    excluded_ids: tuple[int, ...] = ()
    categories: frozenset[DialogCategory] = frozenset()
    exclude_archived: bool = False
    exclude_read: bool = False
    exclude_muted: bool = False

    @property
    def key(self) -> tuple[str, int]:
        return self.namespace, self.folder_id

    @property
    def explicit_ids(self) -> tuple[int, ...]:
        return _dedupe_first((*self.included_ids, *self.pinned_ids))


@dataclass(frozen=True, slots=True)
class FolderRuleObservation:
    rules: tuple[FolderRule, ...]
    token: str
    started_at: int


@dataclass(frozen=True, slots=True)
class FolderMembership:
    namespace: str
    folder_id: int
    dialog_id: int
    state: MembershipState
    pin_position: int | None = None


def _dedupe_first(values: tuple[int, ...]) -> tuple[int, ...]:
    seen: set[int] = set()
    result: list[int] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)
