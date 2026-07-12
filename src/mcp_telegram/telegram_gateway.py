"""Telethon-only exception translation for reading adapters."""

from __future__ import annotations

from telethon.errors import FloodWaitError  # type: ignore[import-untyped]

from .dialog_sync import _ACCESS_LOST_ERRORS
from .telegram_reading import GatewayFailure, GatewayFailureKind

CATCHABLE_GATEWAY_FAILURES = (Exception,)


def translate_gateway_failure(exc: BaseException) -> GatewayFailure:
    """Translate Telegram exceptions at the integration boundary."""
    message = str(exc).replace("\n", "\\n") or type(exc).__name__
    if isinstance(exc, FloodWaitError):
        return GatewayFailure(
            GatewayFailureKind.FLOOD_WAIT, type(exc).__name__, message, True, int(getattr(exc, "seconds", 0) or 0)
        )
    if isinstance(exc, _ACCESS_LOST_ERRORS):
        return GatewayFailure(GatewayFailureKind.ACCESS_LOST, type(exc).__name__, message, False)
    if isinstance(exc, ValueError):
        return GatewayFailure(GatewayFailureKind.INVALID_TARGET, type(exc).__name__, message, False)
    return GatewayFailure(GatewayFailureKind.TRANSIENT, type(exc).__name__, message, True)
