"""Telethon message classification at the Telegram integration boundary."""

from telethon.tl.types import MessageService  # type: ignore[import-untyped]


def is_service_message(message: object) -> bool:
    """Return whether a message is a Telethon service-message instance."""
    return isinstance(message, MessageService)
