"""Telegram access-loss exception classification shared by gateway adapters."""

from __future__ import annotations

from telethon.errors import (  # type: ignore[import-untyped]
    ChannelBannedError,
    ChannelPrivateError,
    ChatForbiddenError,
    UserKickedError,
)

ACCESS_LOST_ERRORS = (
    ChannelBannedError,
    ChannelPrivateError,
    ChatForbiddenError,
    UserKickedError,
)
