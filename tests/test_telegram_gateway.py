from __future__ import annotations

from telethon.errors import ChannelPrivateError, FloodWaitError

from mcp_telegram.telegram_gateway import translate_gateway_failure
from mcp_telegram.telegram_reading import GatewayFailureKind


def test_translate_gateway_failure_classifies_telegram_and_local_errors() -> None:
    flood_failure = translate_gateway_failure(FloodWaitError(request=None, capture=17))
    assert flood_failure.kind is GatewayFailureKind.FLOOD_WAIT
    assert flood_failure.retryable is True
    assert flood_failure.retry_after == 17

    access_failure = translate_gateway_failure(ChannelPrivateError(request=None))
    assert access_failure.kind is GatewayFailureKind.ACCESS_LOST
    assert access_failure.retryable is False

    invalid_failure = translate_gateway_failure(ValueError("dialog not available"))
    assert invalid_failure.kind is GatewayFailureKind.INVALID_TARGET
    assert invalid_failure.retryable is False

    transient_failure = translate_gateway_failure(RuntimeError("network hiccup"))
    assert transient_failure.kind is GatewayFailureKind.TRANSIENT
    assert transient_failure.retryable is True
