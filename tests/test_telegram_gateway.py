"""Tests for telegram_gateway error translation and flood-sleep helpers.

Covers translate_gateway_failure (exception-to-GatewayFailure boundary) and
_client_flood_sleep_threshold (context manager for Telethon flood override).
All functions are pure and deterministic — no I/O, no async, no network.
"""

from __future__ import annotations

from telethon.errors import (
    ChannelBannedError,
    FloodWaitError,
    UserBannedInChannelError,
)

from mcp_telegram.telegram_gateway import _client_flood_sleep_threshold, translate_gateway_failure
from mcp_telegram.telegram_reading import GatewayFailureKind


class TestTranslateGatewayFailure:
    def test_flood_wait_error_with_seconds(self) -> None:
        exc = FloodWaitError(None, capture=30)
        result = translate_gateway_failure(exc)
        assert result.kind == GatewayFailureKind.FLOOD_WAIT
        assert result.error_type == "FloodWaitError"
        assert result.retryable is True
        assert result.retry_after == 30

    def test_flood_wait_error_without_seconds(self) -> None:
        exc = FloodWaitError(None)
        result = translate_gateway_failure(exc)
        assert result.kind == GatewayFailureKind.FLOOD_WAIT
        assert result.retry_after == 0

    def test_access_lost_error(self) -> None:
        exc = ChannelBannedError(None)
        result = translate_gateway_failure(exc)
        assert result.kind == GatewayFailureKind.ACCESS_LOST
        assert result.error_type == "ChannelBannedError"
        assert result.retryable is False

    def test_access_lost_subclass(self) -> None:
        exc = UserBannedInChannelError(None)
        result = translate_gateway_failure(exc)
        assert result.kind == GatewayFailureKind.ACCESS_LOST
        assert result.error_type == "UserBannedInChannelError"
        assert result.retryable is False

    def test_value_error_becomes_invalid_target(self) -> None:
        exc = ValueError("bad peer id")
        result = translate_gateway_failure(exc)
        assert result.kind == GatewayFailureKind.INVALID_TARGET
        assert result.error_type == "ValueError"
        assert result.retryable is False

    def test_unknown_exception_becomes_transient(self) -> None:
        exc = RuntimeError("boom")
        result = translate_gateway_failure(exc)
        assert result.kind == GatewayFailureKind.TRANSIENT
        assert result.error_type == "RuntimeError"
        assert result.retryable is True

    def test_escapes_newlines_in_error_message(self) -> None:
        exc = RuntimeError("line1\nline2")
        result = translate_gateway_failure(exc)
        assert "\n" not in result.error_message
        assert "\\n" in result.error_message

    def test_empty_message_falls_back_to_type_name(self) -> None:
        exc = RuntimeError()
        result = translate_gateway_failure(exc)
        assert result.error_message == "RuntimeError"


class TestClientFloodSleepThreshold:
    def test_sets_and_restores_threshold(self) -> None:
        from types import SimpleNamespace

        client = SimpleNamespace(flood_sleep_threshold=60)
        with _client_flood_sleep_threshold(client, 0):
            assert client.flood_sleep_threshold == 0
        assert client.flood_sleep_threshold == 60

    def test_client_without_attribute_yields_cleanly(self) -> None:
        client = object()
        with _client_flood_sleep_threshold(client, 0):
            pass

    def test_restores_threshold_on_exception(self) -> None:
        from types import SimpleNamespace

        client = SimpleNamespace(flood_sleep_threshold=60)
        try:
            with _client_flood_sleep_threshold(client, 0):
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert client.flood_sleep_threshold == 60
