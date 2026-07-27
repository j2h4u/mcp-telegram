"""Telethon-only gateway helpers for reading adapters."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
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


class _FloodSleepThresholdClient(Protocol):
    flood_sleep_threshold: int


@contextmanager
def _client_flood_sleep_threshold(client: object, threshold: int) -> Iterator[None]:
    """Temporarily set Telethon's client-level FloodWait sleep threshold.

    Telethon 1.44 accepts a per-call ``flood_sleep_threshold`` kwarg, but its
    FloodWait exception path still compares against ``self.flood_sleep_threshold``.
    Scheduled reconciliation needs short FloodWaits to raise immediately so the
    daemon can persist account-level backoff instead of sleeping inside a
    fan-out pass.
    """
    if not hasattr(client, "flood_sleep_threshold"):
        yield
        return

    threshold_client = cast(_FloodSleepThresholdClient, client)
    original = threshold_client.flood_sleep_threshold
    threshold_client.flood_sleep_threshold = threshold
    try:
        yield
    finally:
        threshold_client.flood_sleep_threshold = original


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
    with _client_flood_sleep_threshold(client, flood_sleep_threshold_seconds):
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
