from __future__ import annotations

from types import SimpleNamespace

from telethon.errors import ChannelPrivateError, FloodWaitError

from mcp_telegram.telegram_gateway import (
    _client_flood_sleep_threshold,
    translate_gateway_failure,
)
from mcp_telegram.telegram_reading import GatewayFailureKind


def test_client_flood_sleep_threshold_is_noop_for_clients_without_attribute() -> None:
    """The threshold helper must not fail when the client lacks flood_sleep_threshold."""
    client = SimpleNamespace()

    with _client_flood_sleep_threshold(client, 0):
        pass

    assert not hasattr(client, "flood_sleep_threshold")


def test_translate_gateway_failure_classifies_known_exceptions() -> None:
    """Telegram exceptions must map to the correct GatewayFailure kind and retryability."""
    flood = FloodWaitError(request=None, capture=17)
    flood_failure = translate_gateway_failure(flood)
    assert flood_failure.kind is GatewayFailureKind.FLOOD_WAIT
    assert flood_failure.retryable is True
    assert flood_failure.retry_after == 17

    access_lost = ChannelPrivateError(request=None)
    access_failure = translate_gateway_failure(access_lost)
    assert access_failure.kind is GatewayFailureKind.ACCESS_LOST
    assert access_failure.retryable is False

    invalid = ValueError("dialog not available")
    invalid_failure = translate_gateway_failure(invalid)
    assert invalid_failure.kind is GatewayFailureKind.INVALID_TARGET
    assert invalid_failure.retryable is False

    transient = RuntimeError("network hiccup")
    transient_failure = translate_gateway_failure(transient)
    assert transient_failure.kind is GatewayFailureKind.TRANSIENT
    assert transient_failure.retryable is True
