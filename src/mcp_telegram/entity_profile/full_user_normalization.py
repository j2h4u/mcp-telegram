"""Normalize one ``users.GetFullUser`` response for Entity Profile.

The normalizer is deliberately transport and persistence agnostic.  It accepts
the response-shaped object returned by Telethon, validates the returned user
against the requested target, and emits bounded Python values for the two
projections that share this observation.  In particular, it never carries a
Telethon object, local entity metadata, or a message preview across the domain
boundary.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import TypedDict, cast

FULL_USER_ENDPOINT = "users.GetFullUser"
NORMALIZATION_VERSION = "entity-profile-full-user-v1"

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


class TargetKind(StrEnum):
    """The only target kinds for which the paired operation is applicable."""

    USER = "user"
    BOT = "bot"


class ProjectionStatus(StrEnum):
    """Independent materialization status for one profile projection."""

    USABLE = "usable"
    PARTIAL = "partial"
    ABSENT = "absent"
    UNAVAILABLE = "unavailable"


class FullProfileFacts(TypedDict, total=False):
    about: str | None
    blocked: bool
    ttl_period: int
    private_forward_name: str
    folder_id: int
    birthday: dict[str, int | None]
    bot_info: dict[str, object]
    business_location: dict[str, object]
    business_intro: dict[str, str | None]
    business_work_hours: dict[str, str | None]
    note: str
    name: str
    username: str
    first_name: str
    last_name: str
    extra_usernames: list[str]
    emoji_status_id: int
    status: dict[str, str]
    phone: str
    lang_code: str
    contact: bool
    mutual_contact: bool
    close_friend: bool
    send_paid_messages_stars: int
    verified: bool
    premium: bool
    bot: bool
    scam: bool
    fake: bool
    restricted: bool
    restriction_reason: list[dict[str, str | None]]
    my_membership: dict[str, object]


class PersonalChannelFacts(TypedDict, total=False):
    personal_channel_id: int | None
    personal_channel_message: int
    title: str
    username: str


@dataclass(frozen=True, slots=True)
class ObservationBoundary:
    """Original acquisition boundaries supplied by the worker.

    ``None`` means that the normalizer was used without timing information;
    such an outcome may still be materialized, but cannot authorize freshness
    reuse until the worker supplies valid boundaries.
    """

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
        """Whether this provenance has a valid original observation interval."""
        return self.observation.valid


@dataclass(frozen=True, slots=True)
class ProjectionOutcome:
    """One independent projection result from the shared observation."""

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
class FullUserNormalization:
    """The two independently consumable outcomes of one FullUser response."""

    target_id: int
    target_kind: TargetKind
    full_profile: ProjectionOutcome
    personal_channel: ProjectionOutcome


_MISSING = object()


def _attr(value: object, name: str, default: object = _MISSING) -> object:
    if isinstance(value, Mapping):
        return cast(Mapping[str, object], value).get(name, default)
    try:
        return cast(object, object.__getattribute__(value, name))
    except AttributeError:
        return default


def _sequence(value: object) -> tuple[object, ...] | None:
    if isinstance(value, (str, bytes, bytearray, Mapping)) or not isinstance(value, Sequence):
        return None
    return tuple(cast(Sequence[object], value))


def _string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip()


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _positive_integer(value: object) -> int | None:
    normalized = _integer(value)
    return normalized if normalized is not None and normalized > 0 else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _isoformat(value: object) -> str | None:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return None


def _field(target: dict[str, object], name: str, value: object, *, kind: str) -> None:
    normalized: object | None
    if kind == "string":
        normalized = _string(value)
    elif kind == "int":
        normalized = _integer(value)
    elif kind == "bool":
        normalized = _boolean(value)
    else:  # pragma: no cover - the constants below are intentionally finite.
        raise ValueError(f"unsupported FullUser field kind: {kind}")
    if normalized is not None:
        target[name] = normalized


def _normalize_birthday(value: object) -> dict[str, int | None] | None:
    if value is None or value is _MISSING:
        return None
    return {
        "day": _positive_integer(_attr(value, "day")),
        "month": _positive_integer(_attr(value, "month")),
        "year": _positive_integer(_attr(value, "year")),
    }


def _normalize_bot_info(value: object) -> dict[str, object] | None:
    if value is None or value is _MISSING:
        return None
    commands: list[dict[str, str]] = []
    raw_commands = _sequence(_attr(value, "commands"))
    if raw_commands is not None:
        for command in raw_commands:
            normalized_command = {
                "command": _string(_attr(command, "command")) or "",
                "description": _string(_attr(command, "description")) or "",
            }
            commands.append(normalized_command)
    return {"description": _string(_attr(value, "description")), "commands": commands}


def _normalize_business_location(value: object) -> dict[str, object] | None:
    if value is None or value is _MISSING:
        return None
    geo = _attr(value, "geo_point")
    latitude = _attr(geo, "lat") if geo is not _MISSING else _MISSING
    longitude = _attr(geo, "long") if geo is not _MISSING else _MISSING
    return {
        "address": _string(_attr(value, "address")),
        "lat": latitude if isinstance(latitude, (int, float)) and not isinstance(latitude, bool) else None,
        "long": longitude if isinstance(longitude, (int, float)) and not isinstance(longitude, bool) else None,
    }


def _normalize_business_intro(value: object) -> dict[str, str | None] | None:
    if value is None or value is _MISSING:
        return None
    return {"title": _string(_attr(value, "title")), "description": _string(_attr(value, "description"))}


def _normalize_business_hours(value: object) -> dict[str, str | None] | None:
    if value is None or value is _MISSING:
        return None
    return {"timezone": _string(_attr(value, "timezone_id"))}


def _normalize_note(value: object) -> str | None:
    if isinstance(value, str):
        return _string(value)
    return _string(_attr(value, "text"))


def _normalize_status(value: object) -> dict[str, str] | None:  # noqa: PLR0911
    if value is None or value is _MISSING:
        return None
    status_type = type(value).__name__
    if status_type == "UserStatusRecently":
        return {"type": "recently"}
    if status_type == "UserStatusLastWeek":
        return {"type": "last_week"}
    if status_type == "UserStatusLastMonth":
        return {"type": "last_month"}
    if status_type == "UserStatusOnline":
        timestamp = _isoformat(_attr(value, "expires"))
        return {"type": "online", "expires": timestamp} if timestamp is not None else {"type": "online"}
    if status_type == "UserStatusOffline":
        timestamp = _isoformat(_attr(value, "was_online"))
        return {"type": "offline", "was_online": timestamp} if timestamp is not None else {"type": "offline"}
    return None


def _normalize_restrictions(value: object) -> list[dict[str, str | None]] | None:
    restrictions = _sequence(value)
    if restrictions is None:
        return None
    return [
        {
            "platform": _string(_attr(restriction, "platform")),
            "reason": _string(_attr(restriction, "reason")),
            "text": _string(_attr(restriction, "text")),
        }
        for restriction in restrictions
    ]


def _normalize_full_profile(  # noqa: PLR0912, PLR0915, PLR0914
    full_user: object, user: object
) -> tuple[dict[str, object], bool]:
    facts: dict[str, object] = {}
    complete = True

    def scalar(source: object, name: str, kind: str) -> None:
        nonlocal complete
        value = _attr(source, name)
        if value is _MISSING:
            complete = False
            return
        if value is None:
            facts[name] = None
            return
        normalized: object | None
        if kind == "string":
            normalized = _string(value)
        elif kind == "int":
            normalized = _integer(value)
        elif kind == "bool":
            normalized = _boolean(value)
        else:  # pragma: no cover - this list is intentionally finite.
            raise ValueError(f"unsupported FullUser field kind: {kind}")
        if normalized is None:
            complete = False
            return
        facts[name] = normalized

    for name, kind in (
        ("about", "string"),
        ("blocked", "bool"),
        ("ttl_period", "int"),
        ("private_forward_name", "string"),
        ("folder_id", "int"),
    ):
        scalar(full_user, name, kind)

    nested_normalizers = (
        ("birthday", _normalize_birthday),
        ("bot_info", _normalize_bot_info),
        ("business_location", _normalize_business_location),
        ("business_intro", _normalize_business_intro),
        ("business_work_hours", _normalize_business_hours),
        ("note", _normalize_note),
    )
    for name, normalizer in nested_normalizers:
        raw = _attr(full_user, name)
        if raw is _MISSING:
            complete = False
            continue
        if raw is None:
            facts[name] = None
            continue
        value = normalizer(raw)
        if value is None:
            complete = False
            continue
        facts[name] = value

    first_raw = _attr(user, "first_name")
    last_raw = _attr(user, "last_name")
    if first_raw is _MISSING or last_raw is _MISSING:
        complete = False
    first_name = None if first_raw is _MISSING else _string(first_raw)
    last_name = None if last_raw is _MISSING else _string(last_raw)
    if first_raw is not _MISSING and first_raw is not None and first_name is None:
        complete = False
    if last_raw is not _MISSING and last_raw is not None and last_name is None:
        complete = False
    if first_raw is not _MISSING:
        facts["first_name"] = first_name
    if last_raw is not _MISSING:
        facts["last_name"] = last_name
    if first_raw is not _MISSING or last_raw is not _MISSING:
        facts["name"] = " ".join(part for part in (first_name, last_name) if part)

    for name, kind in (
        ("username", "string"),
        ("phone", "string"),
        ("lang_code", "string"),
        ("contact", "bool"),
        ("mutual_contact", "bool"),
        ("close_friend", "bool"),
        ("send_paid_messages_stars", "int"),
        ("verified", "bool"),
        ("premium", "bool"),
        ("bot", "bool"),
        ("scam", "bool"),
        ("fake", "bool"),
        ("restricted", "bool"),
    ):
        scalar(user, name, kind)

    raw_usernames = _attr(user, "usernames")
    if raw_usernames is _MISSING:
        complete = False
    elif raw_usernames is None:
        facts["extra_usernames"] = None
    else:
        usernames = _sequence(raw_usernames)
        if usernames is None:
            complete = False
        else:
            facts["extra_usernames"] = [
                username
                for entry in usernames
                if (username := _string(_attr(entry, "username"))) is not None and username != facts.get("username")
            ]
    emoji_status = _attr(user, "emoji_status")
    if emoji_status is _MISSING:
        complete = False
    elif emoji_status is None:
        facts["emoji_status_id"] = None
    else:
        emoji_status_id = _positive_integer(_attr(emoji_status, "document_id"))
        if emoji_status_id is None:
            complete = False
        else:
            facts["emoji_status_id"] = emoji_status_id
    raw_status = _attr(user, "status")
    if raw_status is _MISSING:
        complete = False
    elif raw_status is None:
        facts["status"] = None
    else:
        status = _normalize_status(raw_status)
        if status is None:
            complete = False
        else:
            facts["status"] = status
    raw_restrictions = _attr(user, "restriction_reason")
    if raw_restrictions is _MISSING:
        complete = False
    elif raw_restrictions is None:
        facts["restriction_reason"] = None
    else:
        restrictions = _normalize_restrictions(raw_restrictions)
        if restrictions is None:
            complete = False
        else:
            facts["restriction_reason"] = restrictions

    relationship = {name: facts[name] for name in ("contact", "mutual_contact", "close_friend") if name in facts}
    if "blocked" in facts:
        relationship["blocked"] = facts["blocked"]
    if relationship:
        facts["my_membership"] = {
            "is_member": bool(relationship.get("contact") or relationship.get("mutual_contact")),
            "is_admin": False,
            "admin_rights": None,
            "relationship": relationship,
        }
    return facts, complete


def _entity_id(value: object) -> int | None:
    return _positive_integer(_attr(value, "id"))


def _find_matching_user(
    users: tuple[object, ...], target_id: int, target_kind: TargetKind
) -> tuple[object | None, str | None]:
    matches = [user for user in users if _entity_id(user) == target_id]
    if not matches:
        return None, "target_identity_mismatch"
    if len(matches) != 1:
        return None, "target_identity_ambiguous"
    user = matches[0]
    is_bot = _boolean(_attr(user, "bot"))
    if is_bot is None or is_bot is not (target_kind is TargetKind.BOT):
        return None, "target_kind_mismatch"
    return user, None


def _projection_provenance(
    declared_fields: tuple[str, ...],
    payload: Mapping[str, object],
    observation: ObservationBoundary,
    *,
    authoritative: bool,
) -> ProjectionProvenance:
    return ProjectionProvenance(
        endpoint=FULL_USER_ENDPOINT,
        normalization_version=NORMALIZATION_VERSION,
        declared_fields=declared_fields,
        materialized_fields=tuple(name for name in declared_fields if name in payload),
        authoritative=authoritative,
        observation=observation,
    )


def _unavailable(reason: str) -> ProjectionOutcome:
    return ProjectionOutcome(status=ProjectionStatus.UNAVAILABLE, payload=None, reason=reason, provenance=None)


def _normalize_personal_channel(
    full_user: object, chats: object, observation: ObservationBoundary
) -> ProjectionOutcome:
    raw_id = _attr(full_user, "personal_channel_id")
    if raw_id is _MISSING:
        return ProjectionOutcome(
            status=ProjectionStatus.PARTIAL,
            payload=None,
            reason="personal_channel_id_missing",
            provenance=_projection_provenance(PERSONAL_CHANNEL_OWNED_FIELDS, {}, observation, authoritative=False),
        )
    if raw_id is None:
        payload: PersonalChannelFacts = {"personal_channel_id": None}
        return ProjectionOutcome(
            status=ProjectionStatus.ABSENT,
            payload=payload,
            reason=None,
            provenance=_projection_provenance(PERSONAL_CHANNEL_OWNED_FIELDS, payload, observation, authoritative=True),
        )
    channel_id = _positive_integer(raw_id)
    if channel_id is None:
        return ProjectionOutcome(
            status=ProjectionStatus.PARTIAL,
            payload=None,
            reason="personal_channel_id_invalid",
            provenance=_projection_provenance(PERSONAL_CHANNEL_OWNED_FIELDS, {}, observation, authoritative=False),
        )

    payload = {"personal_channel_id": channel_id}
    attached_message_id = _positive_integer(_attr(full_user, "personal_channel_message"))
    if attached_message_id is not None:
        payload["personal_channel_message"] = attached_message_id

    chat_values = _sequence(chats)
    if chat_values is None:
        return ProjectionOutcome(
            status=ProjectionStatus.PARTIAL,
            payload=payload,
            reason="channel_metadata_missing",
            provenance=_projection_provenance(PERSONAL_CHANNEL_OWNED_FIELDS, payload, observation, authoritative=False),
        )
    matching_chat = next((chat for chat in chat_values if _entity_id(chat) == channel_id), None)
    title = _string(_attr(matching_chat, "title")) if matching_chat is not None else None
    username = _string(_attr(matching_chat, "username")) if matching_chat is not None else None
    if title is None and username is None:
        return ProjectionOutcome(
            status=ProjectionStatus.PARTIAL,
            payload=payload,
            reason="channel_metadata_invalid",
            provenance=_projection_provenance(PERSONAL_CHANNEL_OWNED_FIELDS, payload, observation, authoritative=False),
        )
    if title is not None:
        payload["title"] = title
    if username is not None:
        payload["username"] = username
    return ProjectionOutcome(
        status=ProjectionStatus.USABLE,
        payload=payload,
        reason=None,
        provenance=_projection_provenance(PERSONAL_CHANNEL_OWNED_FIELDS, payload, observation, authoritative=True),
    )


def normalize_full_user_response(
    response: object,
    *,
    target_id: int,
    target_kind: TargetKind | str,
    observation: ObservationBoundary | None = None,
) -> FullUserNormalization:
    """Validate and normalize one response for a User or Bot target.

    A valid target identity produces a usable full-profile outcome even when
    channel facts are deficient.  Channel absence is authoritative only when
    Telegram explicitly returns ``personal_channel_id=None``.  All response
    validation failures produce two unavailable outcomes with no provenance,
    so a caller cannot accidentally create a reusable receipt.
    """

    if isinstance(target_id, bool) or not isinstance(target_id, int) or target_id <= 0:
        raise ValueError("target_id must be a positive integer")
    try:
        normalized_kind = TargetKind(target_kind)
    except (TypeError, ValueError) as exc:
        raise ValueError("target_kind must be 'user' or 'bot'") from exc
    boundary = observation or ObservationBoundary()
    full_user = _attr(response, "full_user")
    users = _sequence(_attr(response, "users"))
    reason: str | None = None
    user: object | None = None
    if response is None:
        reason = "invalid_response"
    elif full_user is _MISSING or full_user is None:
        reason = "missing_full_user"
    elif users is None:
        reason = "missing_users"
    else:
        user, reason = _find_matching_user(users, target_id, normalized_kind)
    if reason is not None or user is None:
        failure_reason = reason or "target_identity_missing"
        return FullUserNormalization(
            target_id=target_id,
            target_kind=normalized_kind,
            full_profile=_unavailable(failure_reason),
            personal_channel=_unavailable(failure_reason),
        )

    profile, complete = _normalize_full_profile(full_user, user)
    profile_provenance = _projection_provenance(FULL_PROFILE_OWNED_FIELDS, profile, boundary, authoritative=complete)
    return FullUserNormalization(
        target_id=target_id,
        target_kind=normalized_kind,
        full_profile=ProjectionOutcome(
            status=ProjectionStatus.USABLE,
            payload=profile,
            reason=None if complete else "full_profile_fields_unknown",
            provenance=profile_provenance,
        ),
        personal_channel=_normalize_personal_channel(full_user, _attr(response, "chats"), boundary),
    )


__all__ = [
    "FULL_PROFILE_OWNED_FIELDS",
    "FULL_USER_ENDPOINT",
    "NORMALIZATION_VERSION",
    "PERSONAL_CHANNEL_OWNED_FIELDS",
    "FullProfileFacts",
    "FullUserNormalization",
    "ObservationBoundary",
    "PersonalChannelFacts",
    "ProjectionOutcome",
    "ProjectionProvenance",
    "ProjectionStatus",
    "TargetKind",
    "normalize_full_user_response",
]
