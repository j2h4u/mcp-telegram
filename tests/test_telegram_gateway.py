from __future__ import annotations

import pytest
from telethon.errors import ChannelPrivateError

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_gateway import translate_gateway_failure
from mcp_telegram.telegram_reading import GatewayFailureKind
from mcp_telegram.telegram_rpc import TelegramRpcAdmissionDeferred
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, TelegramRpcSource, rpc_scope


def test_translate_gateway_failure_classifies_telegram_and_local_errors() -> None:
    flood_failure = translate_gateway_failure(TelegramRpcThrottled(retry_after_seconds=17))
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


def test_translate_gateway_failure_marks_latched_throttling_non_retryable() -> None:
    failure = translate_gateway_failure(TelegramRpcThrottled(latched=True))

    assert failure.kind is GatewayFailureKind.FLOOD_WAIT
    assert failure.retryable is False
    assert failure.retry_after is None


def test_translate_gateway_failure_hides_internal_admission_subtype() -> None:
    failure = translate_gateway_failure(
        TelegramRpcAdmissionDeferred(
            retry_after_seconds=1,
            detail="interactive queue class admission saturation; retry in 1s",
        )
    )

    assert failure.error_type == "TelegramRpcThrottled"
    assert failure.error_message == "Telegram RPC throttled"


def test_translate_gateway_failure_propagates_closed_admission() -> None:
    with rpc_scope(TelegramRpcSource.MCP_INTERACTIVE) as scope:
        closed = RpcAdmissionClosedError(scope, "background queue closed during admission")

    with pytest.raises(RpcAdmissionClosedError, match="background queue closed"):
        translate_gateway_failure(closed)
