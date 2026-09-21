"""Stable, storage-neutral facts for the account-owned draft projection.

Draft updates do not carry a Telegram ``pts`` or a server revision.  These
contracts deliberately describe observations rather than pretending that an
arrival has a globally meaningful order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

MAX_DRAFT_ENTITY_COUNT = 1_024
MAX_DRAFT_TEXT_LENGTH = 65_536
MAX_REFERENCE_KIND_LENGTH = 80


class DraftDisposition(StrEnum):
    """Whether an observation contains a draft or an explicit tombstone."""

    PRESENT = "present"
    TOMBSTONE = "tombstone"


class DraftObservationSource(StrEnum):
    """The bounded Telegram path that supplied a draft observation."""

    REALTIME = "realtime"
    SNAPSHOT = "snapshot"


class CompositionCompleteness(StrEnum):
    """Whether every supported draft construct was represented locally."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class DraftCoveragePresence(StrEnum):
    """Whether a read receipt locally observes a draft composition."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class DraftCoverageFreshness(StrEnum):
    """Whether the draft projection receipt can be treated as current."""

    CURRENT = "current"
    STALE = "stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class DraftScope:
    """One account-fenced draft key in the canonical dialog namespace."""

    account_id: int
    dialog_id: int
    top_message_id: int | None = None
    subdialog_peer_id: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (("account_id", self.account_id), ("dialog_id", self.dialog_id)):
            if isinstance(value, bool) or not isinstance(value, int) or value == 0:
                raise ValueError(f"{field_name} must be a non-zero integer")
        optional_values: tuple[tuple[str, int | None], ...] = (
            ("top_message_id", self.top_message_id),
            ("subdialog_peer_id", self.subdialog_peer_id),
        )
        for field_name, optional_value in optional_values:
            if optional_value is not None and (
                isinstance(optional_value, bool) or not isinstance(optional_value, int) or optional_value == 0
            ):
                raise ValueError(f"{field_name} must be a non-zero integer when present")


@dataclass(frozen=True, slots=True)
class DraftEntity:
    """One UTF-16 range with a bounded semantic kind and scalar references."""

    kind: str
    offset_utf16: int
    length_utf16: int
    reference_id: int | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        if not self.kind or len(self.kind) > MAX_REFERENCE_KIND_LENGTH:
            raise ValueError("entity kind must be a non-empty bounded string")
        if self.offset_utf16 < 0 or self.length_utf16 < 0:
            raise ValueError("entity UTF-16 ranges must be non-negative")


@dataclass(frozen=True, slots=True)
class DraftReference:
    """A supported non-text draft reference without raw TL persistence."""

    kind: str
    identifier: int | None = None
    peer_id: int | None = None
    message_id: int | None = None

    def __post_init__(self) -> None:
        if not self.kind or len(self.kind) > MAX_REFERENCE_KIND_LENGTH:
            raise ValueError("reference kind must be a non-empty bounded string")


@dataclass(frozen=True, slots=True)
class DraftComposition:
    """The supported, directly observed shape of a present draft.

    ``text == \"\"`` is a present empty composition.  An absent draft is
    represented only by :class:`DraftDisposition.TOMBSTONE`; callers must not
    collapse those two meanings.
    """

    text: str
    date: datetime | None
    entities: tuple[DraftEntity, ...] = ()
    reply: DraftReference | None = None
    story: DraftReference | None = None
    quote: DraftReference | None = None
    monoforum: DraftReference | None = None
    media: DraftReference | None = None
    rich: DraftReference | None = None
    no_webpage: bool | None = None
    invert_media: bool | None = None
    effect_id: int | None = None
    suggested_post: DraftReference | None = None
    completeness: CompositionCompleteness = CompositionCompleteness.COMPLETE

    def __post_init__(self) -> None:
        if len(self.text) > MAX_DRAFT_TEXT_LENGTH:
            raise ValueError("draft text exceeds the local projection bound")
        if len(self.entities) > MAX_DRAFT_ENTITY_COUNT:
            raise ValueError("draft has too many entities")


@dataclass(frozen=True, slots=True)
class DraftObservation:
    """One normalized observation, before persistence assigns a revision."""

    scope: DraftScope
    disposition: DraftDisposition
    source: DraftObservationSource
    observed_at: datetime
    composition: DraftComposition | None = None
    ambiguity: bool = False

    def __post_init__(self) -> None:
        if self.disposition is DraftDisposition.PRESENT and self.composition is None:
            raise ValueError("a present draft observation requires composition")
        if self.disposition is DraftDisposition.TOMBSTONE and self.composition is not None:
            raise ValueError("a tombstone draft observation cannot carry composition")


@dataclass(frozen=True, slots=True)
class SnapshotCoverage:
    """Authoritative coverage asserted by one unpaged ``messages.GetAllDrafts`` result."""

    account_id: int
    response_complete: bool
    update_count: int
    has_updates_too_long: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.account_id, bool) or not isinstance(self.account_id, int) or self.account_id <= 0:
            raise ValueError("account_id must be positive")
        if self.update_count < 0:
            raise ValueError("update_count must be non-negative")

    @property
    def authoritative(self) -> bool:
        """Return whether absence inference is safe for this response."""
        return self.response_complete and not self.has_updates_too_long


@dataclass(frozen=True, slots=True)
class DraftApplyResult:
    """Persistence result that keeps owner policy independent from SQLite details."""

    accepted: bool
    ambiguous: bool = False
    revision: int | None = None


__all__ = [
    "CompositionCompleteness",
    "DraftApplyResult",
    "DraftComposition",
    "DraftCoverageFreshness",
    "DraftCoveragePresence",
    "DraftDisposition",
    "DraftEntity",
    "DraftObservation",
    "DraftObservationSource",
    "DraftReference",
    "DraftScope",
    "SnapshotCoverage",
]
