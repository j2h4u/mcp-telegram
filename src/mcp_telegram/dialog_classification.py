"""Canonical domain classification for Telegram dialog-shaped entities."""

from enum import IntEnum

from .models import DialogType

RESERVED_REPLIES_USERNAME = "replies"


def normalize_telegram_username(username: object) -> str | None:
    """Normalize a Telegram username for exact reserved-peer matching."""
    if not isinstance(username, str):
        return None
    normalized = username.strip().removeprefix("@").casefold()
    return normalized or None


def is_reserved_replies_username(username: object) -> bool:
    """Return whether *username* is Telegram's reserved ``@replies`` peer."""
    return normalize_telegram_username(username) == RESERVED_REPLIES_USERNAME


def is_bot_dialog_type(raw: object) -> bool:
    """Return whether a persisted legacy dialog type denotes a bot."""
    return DialogType.parse(raw if isinstance(raw, str) else None) is DialogType.BOT


SERVICE_DIALOG_TYPE = DialogType.SERVICE.value


class EntityKind(IntEnum):
    """Transport-neutral shape hint supplied by a Telegram adapter."""

    UNKNOWN = 0
    USER = 1
    CHAT = 2
    CHANNEL = 3


def classify_dialog_type(entity: object | None, *, entity_kind: EntityKind | None = None) -> DialogType:
    """Derive the canonical dialog type from a Telegram entity-shaped object."""
    if entity is None:
        return DialogType.UNKNOWN
    kind = entity_kind if entity_kind is not None else _infer_entity_kind(entity)
    return _classify_entity_kind(entity, kind)


def _infer_entity_kind(entity: object) -> EntityKind:
    if hasattr(entity, "first_name"):
        return EntityKind.USER
    if hasattr(entity, "megagroup") and hasattr(entity, "broadcast"):
        return EntityKind.CHANNEL
    if hasattr(entity, "participants_count"):
        return EntityKind.CHAT
    return EntityKind.UNKNOWN


def _classify_entity_kind(entity: object, kind: EntityKind) -> DialogType:
    result = DialogType.UNKNOWN
    if kind is EntityKind.CHANNEL:
        if getattr(entity, "forum", False):
            result = DialogType.FORUM
        elif getattr(entity, "megagroup", False):
            result = DialogType.SUPERGROUP
        else:
            result = DialogType.CHANNEL
    elif kind is EntityKind.CHAT:
        result = DialogType.GROUP
    elif kind is EntityKind.USER:
        result = (
            DialogType.SERVICE
            if is_reserved_replies_username(getattr(entity, "username", None))
            else (DialogType.BOT if getattr(entity, "bot", False) else DialogType.USER)
        )
    return result
