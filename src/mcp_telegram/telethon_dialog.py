"""Telethon entity classification adapter."""

from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]

from .dialog_classification import (
    RESERVED_REPLIES_USERNAME,
    EntityKind,
    is_reserved_replies_username,
    normalize_telegram_username,
)
from .dialog_classification import classify_dialog_type as _classify_dialog_type
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


__all__ = [
    "RESERVED_REPLIES_USERNAME",
    "classify_dialog_type",
    "is_reserved_replies_username",
    "normalize_telegram_username",
]
