"""Stable contracts for progressive entity profiles.

The daemon owns refresh policy. These types describe only what can be safely
returned to a caller while enrichment is incomplete.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

PROFILE_SECTIONS: tuple[str, ...] = (
    "full_profile",
    "common_chats",
    "contact_overlap",
    "avatar_history",
    "personal_channel",
)

PROFILE_ACQUISITION_OUTCOMES: frozenset[str] = frozenset({"usable", "partial", "absent", "unavailable"})
PROFILE_NORMALIZATION_VERSION = "entity-profile-v1"
FULL_USER_ENDPOINT = "users.GetFullUser"
NORMALIZATION_VERSION = "entity-profile-full-user-v1"
GROUP_PROFILE_ENDPOINT = "messages.GetFullChat"
GROUP_PROFILE_NORMALIZATION_VERSION = "entity-profile-group-full-chat-v1"


class TargetKind(StrEnum):
    """The only target kinds for which user profiles can be fetched."""

    USER = "user"
    BOT = "bot"


class ProjectionStatus(StrEnum):
    """Independent materialization status for one profile projection."""

    USABLE = "usable"
    PARTIAL = "partial"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ObservationBoundary:
    """Original acquisition boundaries supplied by the adapter."""

    started_at: float | None = None
    completed_at: float | None = None

    @property
    def valid(self) -> bool:
        return (
            self.started_at is not None
            and self.completed_at is not None
            and math.isfinite(self.started_at)
            and math.isfinite(self.completed_at)
            and self.started_at <= self.completed_at
        )


@dataclass(frozen=True, slots=True)
class ProjectionProvenance:
    """Bounded evidence describing where one projection's fields came from."""

    endpoint: str
    normalization_version: str
    declared_fields: tuple[str, ...]
    materialized_fields: tuple[str, ...]
    authoritative: bool
    observation: ObservationBoundary

    @property
    def reusable(self) -> bool:
        return self.observation.valid


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """One independent projection result from a shared observation."""

    status: ProjectionStatus
    payload: Mapping[str, object] | None
    reason: str | None
    provenance: ProjectionProvenance | None

    @property
    def authoritative_absence(self) -> bool:
        return self.status is ProjectionStatus.ABSENT and bool(
            self.provenance is not None and self.provenance.authoritative
        )


@dataclass(frozen=True, slots=True)
class UserProfileObservation:
    """Neutral result containing both projections of one FullUser request."""

    target_id: int
    target_kind: TargetKind
    full_profile: ProjectionOutcome
    personal_channel: ProjectionOutcome
    personal_channel_reference: PersonalChannelReference | None = None

    def __post_init__(self) -> None:
        _validate_positive_id(self.target_id, field_name="target_id")
        if not isinstance(self.target_kind, TargetKind):
            raise TypeError("target_kind must be a TargetKind")


@dataclass(frozen=True, slots=True)
class PersonalChannelReference:
    """Bounded capability for fetching one user's personal channel post."""

    channel_id: int
    access_hash: int

    def __post_init__(self) -> None:
        _validate_positive_id(self.channel_id, field_name="channel_id")
        if (
            not isinstance(self.access_hash, int)
            or isinstance(self.access_hash, bool)
            or not -(2**63) <= self.access_hash <= (2**63 - 1)
        ):
            raise ValueError("access_hash must be a signed 64-bit integer")


@dataclass(frozen=True, slots=True)
class PersonalChannelPost:
    """Neutral bounded result for one personal-channel message fetch."""

    message_id: int
    sent_at: int | None
    text: str | None

    def __post_init__(self) -> None:
        _validate_positive_id(self.message_id, field_name="message_id")
        if self.sent_at is not None and (not isinstance(self.sent_at, int) or isinstance(self.sent_at, bool)):
            raise TypeError("sent_at must be an integer or None")
        if self.text is not None and not isinstance(self.text, str):
            raise TypeError("text must be a string or None")


@dataclass(frozen=True, slots=True)
class GroupCurrentPhoto:
    """Transport-neutral primitives for the current group avatar."""

    photo_id: int
    date: str | None = None

    def __post_init__(self) -> None:
        _validate_positive_id(self.photo_id, field_name="photo_id")
        if self.date is not None and not isinstance(self.date, str):
            raise TypeError("date must be an ISO string or None")


@dataclass(frozen=True, slots=True)
class GroupProfileObservation:
    """Normalized result of one legacy group profile observation."""

    group_id: int
    about: str | None
    invite_link: str | None
    participant_ids: tuple[int, ...] | None
    participants_unavailable_reason: str | None
    current_photo: GroupCurrentPhoto | None
    observation_started_at: int
    observation_completed_at: int
    endpoint: str = GROUP_PROFILE_ENDPOINT
    normalization_version: str = GROUP_PROFILE_NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, int) or isinstance(self.group_id, bool):
            raise ValueError("group_id must be an integer")
        _validate_participant_observation(self.participant_ids, self.participants_unavailable_reason)
        _validate_observation_interval(self.observation_started_at, self.observation_completed_at)


def _validate_positive_id(value: object, *, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _validate_participant_observation(ids: tuple[int, ...] | None, reason: str | None) -> None:
    if ids is None:
        if reason is None:
            raise ValueError("unavailable participants require a reason")
        return
    if not isinstance(ids, tuple):
        raise TypeError("participant_ids must be a tuple or None")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in ids):
        raise ValueError("participant_ids must contain positive integers")
    if reason is not None:
        raise ValueError("available participants cannot have an unavailable reason")


def _validate_observation_interval(started_at: int, completed_at: int) -> None:
    if not isinstance(started_at, int) or started_at < 0:
        raise ValueError("observation_started_at must be a non-negative integer")
    if not isinstance(completed_at, int) or completed_at < 0:
        raise ValueError("observation_completed_at must be a non-negative integer")
    if completed_at < started_at:
        raise ValueError("observation interval is reversed")


FULL_PROFILE_OWNED_FIELDS: tuple[str, ...] = (
    "about",
    "blocked",
    "ttl_period",
    "private_forward_name",
    "folder_id",
    "birthday",
    "bot_info",
    "business_location",
    "business_intro",
    "business_work_hours",
    "note",
    "name",
    "username",
    "first_name",
    "last_name",
    "extra_usernames",
    "emoji_status_id",
    "status",
    "phone",
    "lang_code",
    "contact",
    "mutual_contact",
    "close_friend",
    "send_paid_messages_stars",
    "verified",
    "premium",
    "bot",
    "scam",
    "fake",
    "restricted",
    "restriction_reason",
    "my_membership",
)

PERSONAL_CHANNEL_OWNED_FIELDS: tuple[str, ...] = (
    "personal_channel_id",
    "personal_channel_message",
    "title",
    "username",
)


@dataclass(frozen=True, slots=True)
class ProfileAcquisitionEvidence:
    """Bounded evidence attached to one section materialization.

    The repository stores this as a small JSON projection.  Raw Telegram
    envelopes and arbitrary response objects deliberately have no place in the
    contract.
    """

    generation: int
    outcome: str
    provenance: Mapping[str, object] | None = None
    normalization_version: str | None = PROFILE_NORMALIZATION_VERSION
    observation_started_at: int | None = None
    observation_completed_at: int | None = None
    identity: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.generation < 0:
            raise ValueError("generation must be non-negative")
        if self.outcome not in PROFILE_ACQUISITION_OUTCOMES:
            raise ValueError(f"unsupported profile acquisition outcome: {self.outcome}")
        if self.observation_started_at is not None and self.observation_started_at < 0:
            raise ValueError("observation_started_at must be non-negative")
        if self.observation_completed_at is not None and self.observation_completed_at < 0:
            raise ValueError("observation_completed_at must be non-negative")
        if (
            self.observation_started_at is not None
            and self.observation_completed_at is not None
            and self.observation_completed_at < self.observation_started_at
        ):
            raise ValueError("observation interval is reversed")

    @property
    def observation_at(self) -> int | None:
        """Use the conservative start of the original observation interval."""
        return self.observation_started_at


def completeness(sections: dict[str, dict[str, object]]) -> str:
    """Return ``complete`` only when every applicable section is fresh."""
    if not sections:
        return "partial"
    statuses = {str(value.get("status")) for value in sections.values()}
    return "complete" if statuses <= {"fresh", "not_applicable"} else "partial"
