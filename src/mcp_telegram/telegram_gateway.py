"""Telethon-only gateway helpers for reading adapters."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

from telethon.errors import FloodWaitError  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetScheduledHistoryRequest  # type: ignore[import-untyped]
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_reading import GatewayFailure, GatewayFailureKind

CATCHABLE_GATEWAY_FAILURES = (Exception,)


class ScheduledHistoryClient(Protocol):
    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


def translate_gateway_failure(exc: BaseException) -> GatewayFailure:
    """Translate Telegram exceptions at the integration boundary."""
    message = str(exc).replace("\n", "\\n") or type(exc).__name__
    if isinstance(exc, FloodWaitError):
        return GatewayFailure(
            GatewayFailureKind.FLOOD_WAIT, type(exc).__name__, message, True, int(getattr(exc, "seconds", 0) or 0)
        )
    if isinstance(exc, ACCESS_LOST_ERRORS):
        return GatewayFailure(GatewayFailureKind.ACCESS_LOST, type(exc).__name__, message, False)
    if isinstance(exc, ValueError):
        return GatewayFailure(GatewayFailureKind.INVALID_TARGET, type(exc).__name__, message, False)
    return GatewayFailure(GatewayFailureKind.TRANSIENT, type(exc).__name__, message, True)


async def fetch_scheduled_history_snapshot(
    client: ScheduledHistoryClient,
    dialog_id: int,
    *,
    flood_sleep_threshold_seconds: int,
) -> list[object]:
    """Fetch one scheduled queue snapshot through Telethon.

    The threshold is injected by daemon configuration.  For scheduled
    reconciliation production uses ``0`` so Telethon raises even short
    FloodWaits and the caller can persist durable account-level backoff instead
    of silently sleeping and continuing the fan-out pass.
    """
    input_entity = cast(TypeInputPeer, await client.get_input_entity(dialog_id))
    result = await client(
        GetScheduledHistoryRequest(peer=input_entity, hash=0),
        flood_sleep_threshold=flood_sleep_threshold_seconds,
    )
    messages = list(cast(Sequence[object], getattr(result, "messages", ()) or ()))
    entities = {
        get_peer_id(entity): entity
        for entity in [
            *cast(Sequence[object], getattr(result, "users", ()) or ()),
            *cast(Sequence[object], getattr(result, "chats", ()) or ()),
        ]
    }
    for message in messages:
        finish_init = getattr(message, "_finish_init", None)
        if callable(finish_init):
            finish_init(client, entities, input_entity)
    return messages
