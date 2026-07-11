"""Telethon entity classification at the Telegram integration boundary."""

from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]

from .models import DialogType


def classify_dialog_type(entity: object | None) -> DialogType:
    """Derive the canonical dialog type from a live Telegram entity."""
    kind = DialogType.UNKNOWN
    if entity is not None:
        if isinstance(entity, Channel):
            if getattr(entity, "forum", False):
                kind = DialogType.FORUM
            elif getattr(entity, "megagroup", False):
                kind = DialogType.SUPERGROUP
            else:
                kind = DialogType.CHANNEL
        elif isinstance(entity, Chat):
            kind = DialogType.GROUP
        # Bots are Users with bot=True; avoid importing User for duck-typed clients/tests.
        elif hasattr(entity, "first_name"):
            kind = DialogType.BOT if getattr(entity, "bot", False) else DialogType.USER
    return kind
