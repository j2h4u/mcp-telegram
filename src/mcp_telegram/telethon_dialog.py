"""Telethon entity classification adapter."""

from telethon.tl.types import (  # type: ignore[import-untyped]
    Channel,
    ChannelForbidden,
    Chat,
    ChatForbidden,
    User,
)

from .dialog_classification import (
    RESERVED_REPLIES_USERNAME,
    EntityKind,
    is_reserved_replies_username,
    normalize_telegram_username,
)
from .dialog_classification import classify_dialog_type as _classify_dialog_type
from .dialog_identity_contracts import IDENTITY_OMITTED, DialogIdentityObservation, ObservedText
from .identity_observation import USERNAME_UNOBSERVED, observe_username
from .models import DialogType


def classify_dialog_type(entity: object | None) -> DialogType:
    """Classify a Telethon entity through the transport-neutral domain rule."""
    if isinstance(entity, Channel):
        kind = EntityKind.CHANNEL
    elif isinstance(entity, Chat):
        kind = EntityKind.CHAT
    elif entity is not None and hasattr(entity, "first_name"):
        kind = EntityKind.USER
    else:
        kind = EntityKind.UNKNOWN
    return _classify_dialog_type(entity, entity_kind=kind)


def observe_dialog_identity(
    entity: object | None,
    *,
    dialog_id: int,
    source: str,
    observed_at: int,
) -> DialogIdentityObservation | None:
    """Extract only identity facts supported by a concrete Telethon TL class."""
    if isinstance(entity, User):
        return _observe_user_identity(entity, dialog_id=dialog_id, source=source, observed_at=observed_at)
    if isinstance(entity, Channel):
        return _observe_channel_identity(entity, dialog_id=dialog_id, source=source, observed_at=observed_at)
    if isinstance(entity, Chat):
        return _complete_identity(
            entity,
            dialog_id=dialog_id,
            source=source,
            observed_at=observed_at,
            identity_fields=(_positive_name(getattr(entity, "title", None)), None),
        )
    if isinstance(entity, (ChannelForbidden, ChatForbidden)):
        return _observe_partial_title_identity(entity, dialog_id=dialog_id, source=source, observed_at=observed_at)
    return None


def _observe_user_identity(
    entity: User, *, dialog_id: int, source: str, observed_at: int
) -> DialogIdentityObservation | None:
    if bool(getattr(entity, "min", False)):
        name = _positive_name(" ".join(part for part in (entity.first_name, entity.last_name) if part))
        username = _partial_username(entity)
        return _partial_identity(dialog_id, source, observed_at, name, username)
    name = _positive_name(" ".join(part for part in (entity.first_name, entity.last_name) if part))
    username = _canonical_username(entity)
    return _complete_identity(
        entity,
        dialog_id=dialog_id,
        source=source,
        observed_at=observed_at,
        identity_fields=(name, username),
    )


def _observe_channel_identity(
    entity: Channel, *, dialog_id: int, source: str, observed_at: int
) -> DialogIdentityObservation | None:
    name = _positive_name(entity.title)
    if bool(getattr(entity, "min", False)):
        return _partial_identity(dialog_id, source, observed_at, name, _partial_username(entity))
    return _complete_identity(
        entity,
        dialog_id=dialog_id,
        source=source,
        observed_at=observed_at,
        identity_fields=(name, _canonical_username(entity)),
    )


def _observe_partial_title_identity(
    entity: ChannelForbidden | ChatForbidden, *, dialog_id: int, source: str, observed_at: int
) -> DialogIdentityObservation | None:
    name = _positive_name(entity.title)
    username = _partial_username(entity) if isinstance(entity, ChannelForbidden) else IDENTITY_OMITTED
    return _partial_identity(dialog_id, source, observed_at, name, username)


def _complete_identity(
    entity: object,
    *,
    dialog_id: int,
    source: str,
    observed_at: int,
    identity_fields: tuple[str | None, str | None],
) -> DialogIdentityObservation | None:
    name, username = identity_fields
    dialog_type = classify_dialog_type(entity)
    if dialog_type is DialogType.UNKNOWN:
        return None
    return DialogIdentityObservation(
        dialog_id=dialog_id,
        name=name,
        username=username,
        dialog_type=dialog_type,
        complete=True,
        source=source,
        observed_at=observed_at,
    )


def _partial_identity(
    dialog_id: int,
    source: str,
    observed_at: int,
    name: str | None,
    username: ObservedText,
) -> DialogIdentityObservation | None:
    if name is None and username is IDENTITY_OMITTED:
        return None
    return DialogIdentityObservation(
        dialog_id=dialog_id,
        name=name if name is not None else IDENTITY_OMITTED,
        username=username,
        dialog_type=IDENTITY_OMITTED,
        complete=False,
        source=source,
        observed_at=observed_at,
    )


def _partial_username(entity: object) -> ObservedText:
    username = observe_username(_tl_field(entity, "username"), _tl_field(entity, "usernames"))
    if isinstance(username, str) and username:
        return username
    return IDENTITY_OMITTED


def _canonical_username(entity: object) -> str | None:
    username = observe_username(_tl_field(entity, "username"), _tl_field(entity, "usernames"))
    return None if username is USERNAME_UNOBSERVED else username if isinstance(username, str) else None


def _tl_field(entity: object, field: str) -> object:
    return getattr(entity, field, USERNAME_UNOBSERVED)


def _positive_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


__all__ = [
    "RESERVED_REPLIES_USERNAME",
    "classify_dialog_type",
    "is_reserved_replies_username",
    "normalize_telegram_username",
    "observe_dialog_identity",
]
