"""Stable contracts for progressive entity profiles.

The daemon owns refresh policy. These types describe only what can be safely
returned to a caller while enrichment is incomplete.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

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
CHANNEL_PROFILE_ENDPOINT = "channels.GetFullChannel"
CHANNEL_PROFILE_NORMALIZATION_VERSION = "entity-profile-channel-full-v1"
CHANNEL_CONTACT_OVERLAP_ENDPOINT = "channels.GetParticipants"
CHANNEL_CONTACT_OVERLAP_NORMALIZATION_VERSION = "entity-profile-channel-contacts-v1"
CHANNEL_ID_MARKER = 1_000_000_000_000


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
class ChannelReference:
    """A canonical channel identity and its signed access hash."""

    channel_id: int
    access_hash: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.channel_id, int)
            or isinstance(self.channel_id, bool)
            or self.channel_id >= -CHANNEL_ID_MARKER
        ):
            raise ValueError("channel_id must be a canonical marked channel id")
        if (
            not isinstance(self.access_hash, int)
            or isinstance(self.access_hash, bool)
            or not -(2**63) <= self.access_hash <= 2**63 - 1
        ):
            raise ValueError("access_hash must be a signed 64-bit integer")


@dataclass(frozen=True, slots=True)
class UserReference:
    """A reconstructible user capability obtained from the local session."""

    user_id: int
    access_hash: int
    is_self: bool = False

    def __post_init__(self) -> None:
        _validate_positive_id(self.user_id, field_name="user_id")
        if not isinstance(self.access_hash, int) or isinstance(self.access_hash, bool):
            raise ValueError("access_hash must be an integer")
        if not -(2**63) <= self.access_hash <= 2**63 - 1:
            raise ValueError("access_hash must be a signed 64-bit integer")
        if not isinstance(self.is_self, bool):
            raise TypeError("is_self must be a boolean")


@dataclass(frozen=True, slots=True)
class GroupReference:
    """A canonical legacy group identity."""

    group_id: int

    def __post_init__(self) -> None:
        if not isinstance(self.group_id, int) or isinstance(self.group_id, bool) or self.group_id == 0:
            raise ValueError("group_id must be a non-zero integer")


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
    current_photo: ChatCurrentPhoto | None = None

    def __post_init__(self) -> None:
        _validate_positive_id(self.target_id, field_name="target_id")
        if not isinstance(self.target_kind, TargetKind):
            raise TypeError("target_kind must be a TargetKind")
        if self.current_photo is not None and not isinstance(self.current_photo, ChatCurrentPhoto):
            raise TypeError("current_photo must be ChatCurrentPhoto or None")


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
class ChatCurrentPhoto:
    """Transport-neutral primitives for a chat or channel's current avatar."""

    photo_id: int
    date: str | None = None

    def __post_init__(self) -> None:
        _validate_positive_id(self.photo_id, field_name="photo_id")
        if self.date is not None and not isinstance(self.date, str):
            raise TypeError("date must be an ISO string or None")


@dataclass(frozen=True, slots=True)
class CommonChatSummary:
    """One common-chat row with only transport-neutral fields."""

    chat_id: int
    name: str | None
    kind: str

    def __post_init__(self) -> None:
        _validate_nonzero_id(self.chat_id, field_name="chat_id")
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("name must be a string or None")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("kind must be a non-empty string")


@dataclass(frozen=True, slots=True)
class UserAvatarHistoryObservation:
    """One bounded user profile-photo page."""

    user_id: int
    photos: tuple[ChatCurrentPhoto, ...]
    reported_count: int
    status: ProjectionStatus
    reason: str | None
    observation_started_at: float
    observation_completed_at: float

    def __post_init__(self) -> None:
        _validate_positive_id(self.user_id, field_name="user_id")
        _validate_photos(self.photos)
        _validate_count(self.reported_count)
        _validate_bounded_status(self.status, self.reason)
        _validate_observation_boundary(self.observation_started_at, self.observation_completed_at)


@dataclass(frozen=True, slots=True)
class CommonChatsObservation:
    """One bounded common-chat page for a user."""

    user_id: int
    chats: tuple[CommonChatSummary, ...]
    reported_count: int
    status: ProjectionStatus
    reason: str | None
    observation_started_at: float
    observation_completed_at: float

    def __post_init__(self) -> None:
        _validate_positive_id(self.user_id, field_name="user_id")
        if not isinstance(self.chats, tuple) or any(not isinstance(item, CommonChatSummary) for item in self.chats):
            raise TypeError("chats must be a tuple of CommonChatSummary")
        _validate_count(self.reported_count)
        _validate_bounded_status(self.status, self.reason)
        _validate_observation_boundary(self.observation_started_at, self.observation_completed_at)


ChatAvatarReference = ChannelReference | GroupReference


def reconcile_chat_avatar_history(
    history: tuple[ChatCurrentPhoto, ...], current_photo: ChatCurrentPhoto | None, reported_count: int
) -> tuple[tuple[ChatCurrentPhoto, ...], int]:
    """Merge current avatar into a bounded history deterministically."""
    output: list[ChatCurrentPhoto] = []
    positions: dict[int, int] = {}
    if current_photo is not None:
        output.append(current_photo)
        positions[current_photo.photo_id] = 0
    for photo in history:
        position = positions.get(photo.photo_id)
        if position is None:
            positions[photo.photo_id] = len(output)
            output.append(photo)
            continue
        if output[position].date is None and photo.date is not None:
            output[position] = photo
    return tuple(output), max(reported_count, len(output))


@dataclass(frozen=True, slots=True)
class ChatAvatarHistoryObservation:
    """One bounded chat avatar-history page."""

    reference: ChatAvatarReference
    photos: tuple[ChatCurrentPhoto, ...]
    reported_count: int
    status: ProjectionStatus
    reason: str | None
    observation_started_at: float
    observation_completed_at: float

    def __post_init__(self) -> None:
        if not isinstance(self.reference, (ChannelReference, GroupReference)):
            raise TypeError("chat avatar reference is invalid")
        _validate_photos(self.photos)
        _validate_count(self.reported_count)
        _validate_bounded_status(self.status, self.reason)
        _validate_observation_boundary(self.observation_started_at, self.observation_completed_at)


@dataclass(frozen=True, slots=True)
class ChannelProfileObservation:
    """Normalized result of one ``channels.GetFullChannel`` observation."""

    channel_id: int
    about: str | None
    participants_count: int | None
    linked_chat_id: int | None
    pinned_msg_id: int | None
    slow_mode_seconds: int | None
    available_reactions: Mapping[str, object]
    current_photo: ChatCurrentPhoto | None
    observation_started_at: float
    observation_completed_at: float
    status: ProjectionStatus = ProjectionStatus.USABLE
    reason: str | None = None
    endpoint: str = CHANNEL_PROFILE_ENDPOINT
    normalization_version: str = CHANNEL_PROFILE_NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        _validate_nonzero_id(self.channel_id, field_name="channel_id")
        _validate_optional_nonnegative(self.participants_count, field_name="participants_count")
        _validate_optional_nonzero_id(self.linked_chat_id, field_name="linked_chat_id")
        _validate_optional_nonnegative(self.pinned_msg_id, field_name="pinned_msg_id")
        _validate_optional_nonnegative(self.slow_mode_seconds, field_name="slow_mode_seconds")
        if not isinstance(self.available_reactions, Mapping):
            raise TypeError("available_reactions must be a mapping")
        status = _coerce_projection_status(self.status, error="channel profile status is invalid")
        object.__setattr__(self, "status", status)
        _validate_projection_reason(status, self.reason, usable_message="usable channel profile cannot have a reason")
        _validate_observation_boundary(self.observation_started_at, self.observation_completed_at)


@dataclass(frozen=True, slots=True)
class ChannelContactOverlapObservation:
    """Bounded contact overlap from one contacts-filter page."""

    channel_id: int
    contact_ids: tuple[int, ...] | None
    status: ProjectionStatus
    reason: str | None
    observation_started_at: float | None = None
    observation_completed_at: float | None = None
    endpoint: str = CHANNEL_CONTACT_OVERLAP_ENDPOINT
    normalization_version: str = CHANNEL_CONTACT_OVERLAP_NORMALIZATION_VERSION

    def __post_init__(self) -> None:
        _validate_nonzero_id(self.channel_id, field_name="channel_id")
        status = _coerce_projection_status(self.status, error="channel contact overlap status is invalid")
        object.__setattr__(self, "status", status)
        _validate_contact_overlap(status, self.contact_ids, self.reason)
        _validate_optional_observation_boundary(self.observation_started_at, self.observation_completed_at)


@dataclass(frozen=True, slots=True)
class GroupProfileObservation:
    """Normalized result of one legacy group profile observation."""

    group_id: int
    about: str | None
    invite_link: str | None
    participant_ids: tuple[int, ...] | None
    participants_unavailable_reason: str | None
    current_photo: ChatCurrentPhoto | None
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


def _validate_nonzero_id(value: object, *, field_name: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value == 0:
        raise ValueError(f"{field_name} must be a non-zero integer")


def _validate_count(value: object) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("reported_count must be a non-negative integer")


def _validate_photos(value: object) -> None:
    if not isinstance(value, tuple) or any(not isinstance(item, ChatCurrentPhoto) for item in value):
        raise TypeError("photos must be a tuple of ChatCurrentPhoto")
    if len({item.photo_id for item in value}) != len(value):
        raise ValueError("photos must be deduplicated")


def _validate_bounded_status(status: object, reason: str | None) -> None:
    if status not in {ProjectionStatus.USABLE, ProjectionStatus.PARTIAL, ProjectionStatus.UNAVAILABLE}:
        raise ValueError("bounded observation status is invalid")
    if status is ProjectionStatus.USABLE and reason is not None:
        raise ValueError("usable bounded observation cannot have a reason")
    if status is ProjectionStatus.PARTIAL and reason != "bounded_page":
        raise ValueError("partial bounded observation requires bounded_page")
    if status is ProjectionStatus.UNAVAILABLE and not reason:
        raise ValueError("unavailable bounded observation requires a reason")


def _validate_optional_nonzero_id(value: object, *, field_name: str) -> None:
    if value is not None:
        _validate_nonzero_id(value, field_name=field_name)


def _validate_optional_nonnegative(value: object, *, field_name: str) -> None:
    if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise ValueError(f"{field_name} must be a non-negative integer or None")


def _coerce_projection_status(value: object, *, error: str) -> ProjectionStatus:
    try:
        return ProjectionStatus(cast(str, value))
    except (TypeError, ValueError) as exc:
        raise ValueError(error) from exc


def _validate_projection_reason(status: ProjectionStatus, reason: str | None, *, usable_message: str) -> None:
    if status is ProjectionStatus.USABLE and reason is not None:
        raise ValueError(usable_message)
    if status is ProjectionStatus.UNAVAILABLE and not reason:
        raise ValueError("unavailable channel profile requires a reason")


def _validate_contact_overlap(
    status: ProjectionStatus,
    contact_ids: tuple[int, ...] | None,
    reason: str | None,
) -> None:
    if status not in {ProjectionStatus.PARTIAL, ProjectionStatus.UNAVAILABLE}:
        raise ValueError("channel contact overlap status must be partial or unavailable")
    if contact_ids is None:
        _validate_missing_contact_overlap(status, reason)
        return
    _validate_present_contact_overlap(status, contact_ids, reason)


def _validate_missing_contact_overlap(status: ProjectionStatus, reason: str | None) -> None:
    if status is not ProjectionStatus.UNAVAILABLE:
        raise ValueError("partial contact overlap requires contact ids")
    if not reason:
        raise ValueError("unavailable contact overlap requires a reason")


def _validate_present_contact_overlap(
    status: ProjectionStatus,
    contact_ids: tuple[int, ...],
    reason: str | None,
) -> None:
    if not isinstance(contact_ids, tuple):
        raise TypeError("contact_ids must be a tuple or None")
    if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in contact_ids):
        raise ValueError("contact_ids must contain positive integers")
    if len(set(contact_ids)) != len(contact_ids):
        raise ValueError("contact_ids must be deduplicated")
    if status is not ProjectionStatus.PARTIAL or reason != "bounded_contacts_page":
        raise ValueError("successful contact overlap must be partial for bounded_contacts_page")


def _validate_optional_observation_boundary(started_at: float | None, completed_at: float | None) -> None:
    if started_at is None and completed_at is None:
        return
    if started_at is None or completed_at is None:
        raise ValueError("contact overlap timing must include both boundaries")
    _validate_observation_boundary(started_at, completed_at)


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


def _validate_observation_boundary(started_at: float, completed_at: float) -> None:
    if not _finite_number(started_at) or not _finite_number(completed_at) or completed_at < started_at:
        raise ValueError("observation timing is invalid")


def _finite_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


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
