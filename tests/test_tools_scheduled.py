from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import cast
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent
from pydantic import ValidationError

from mcp_telegram import tools
from mcp_telegram.scheduled_commands import SCHEDULED_REPEAT_PERIODS
from mcp_telegram.tools.scheduled import (
    MANAGE_SCHEDULED_MESSAGE_OUTPUT_SCHEMA,
    ManageScheduledMessage,
    _scheduled_request,
    manage_scheduled_message,
)


def _future_utc() -> str:
    return (datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")


def test_manage_tool_is_registered_with_mutation_annotations_and_schema() -> None:
    entry = tools.TOOL_REGISTRY["manage_scheduled_message"]
    assert entry.cls is ManageScheduledMessage
    assert entry.posture == "primary"
    assert entry.annotations is not None
    assert entry.annotations.read_only_hint is False
    assert entry.annotations.destructive_hint is True
    assert entry.annotations.idempotent_hint is False
    assert entry.annotations.open_world_hint is True
    assert entry.output_schema is not None
    properties = cast(dict[str, object], entry.output_schema["properties"])
    normalized_schema = tools.normalize_output_schema(MANAGE_SCHEDULED_MESSAGE_OUTPUT_SCHEMA)
    assert normalized_schema is not None
    expected = cast(dict[str, object], normalized_schema["properties"])
    for name, schema in expected.items():
        assert properties[name] == schema


def test_request_resolves_selector_and_preserves_only_supplied_edits() -> None:
    args = ManageScheduledMessage(
        dialog="@room",
        account_id=7,
        topic_id=10,
        action="update",
        operation_key="adjust-1",
        scheduled_message_id=500,
        expected_version="v1",
        schedule_at_utc=_future_utc(),
    )
    assert _scheduled_request(args) == {
        "method": "manage_scheduled_message",
        "dialog": "@room",
        "account_id": 7,
        "topic_id": 10,
        "action": "update",
        "operation_key": "adjust-1",
        "scheduled_message_id": 500,
        "expected_version": "v1",
        "schedule_at_utc": args.schedule_at_utc,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "create", "text": "hello"},
        {"action": "create", "text": "hello", "schedule_at_utc": "2099-01-01T00:00:00+03:00"},
        {"action": "create", "text": "hello", "schedule_at_utc": "2099-01-01T00:00:00"},
        {"action": "create", "text": "hello", "schedule_at_utc": "2099-01-01T00:00:00Z", "scheduled_message_id": 2},
        {"action": "update", "scheduled_message_id": 2, "expected_version": "v1"},
        {"action": "update", "scheduled_message_id": 2, "expected_version": "v1", "repeat_period_seconds": True},
        {"action": "update", "scheduled_message_id": 2, "expected_version": "v1", "repeat_period_seconds": None},
        {"action": "update", "scheduled_message_id": 2, "expected_version": "v1", "repeat_period_seconds": 60},
        {"action": "cancel", "scheduled_message_id": 2, "expected_version": "v1", "text": "no"},
    ],
)
def test_invalid_action_combinations_are_rejected(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ManageScheduledMessage.model_validate({"exact_dialog_id": 123, "operation_key": "key", **payload})


def test_repeat_input_matches_command_owner_allowlist() -> None:
    assert set(SCHEDULED_REPEAT_PERIODS) == set(_scheduled_repeat_periods())
    for period in SCHEDULED_REPEAT_PERIODS:
        args = ManageScheduledMessage(
            exact_dialog_id=123,
            action="create",
            operation_key=f"repeat-{period}",
            text="repeat",
            schedule_at_utc=_future_utc(),
            repeat_period_seconds=period,
        )
        assert args.repeat_period_seconds == period


def _scheduled_repeat_periods() -> frozenset[int]:
    return frozenset({86_400, 604_800, 1_209_600, 2_592_000, 7_862_400, 15_724_800, 31_536_000})


def test_create_and_cancel_valid_shapes() -> None:
    create = ManageScheduledMessage(
        exact_dialog_id=123,
        action="create",
        operation_key="create-1",
        text="plain text",
        schedule_at_utc=_future_utc(),
    )
    cancel = ManageScheduledMessage(
        exact_dialog_id=123,
        action="cancel",
        operation_key="cancel-1",
        scheduled_message_id=99,
        expected_version="v4",
    )
    assert _scheduled_request(create)["method"] == "manage_scheduled_message"
    assert _scheduled_request(cancel)["scheduled_message_id"] == 99


async def test_past_schedule_timestamp_reaches_daemon_for_same_key_receipt_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    past_time = "2020-01-01T00:00:00Z"
    payload_seen: dict[str, object] = {}
    receipt_response: dict[str, object] = {
        "ok": True,
        "action": "create",
        "operation_key": "old-create-key",
        "account_id": 7,
        "dialog_id": 123,
        "scheduled_message_id": 500,
        "outcome": "acknowledged",
        "mutation_dispatched": True,
        "acknowledged": True,
        "receipt_persisted": True,
        "current_is_operation_confirmation": True,
        "schedule_at_utc": past_time,
    }

    class Conn:
        async def __aenter__(self) -> Conn:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, payload: dict[str, object]) -> dict[str, object]:
            payload_seen.update(payload)
            return receipt_response

    @asynccontextmanager
    async def connection() -> AsyncIterator[Conn]:
        yield Conn()

    monkeypatch.setattr("mcp_telegram.tools.scheduled.daemon_connection", connection)
    args = ManageScheduledMessage(
        exact_dialog_id=123,
        action="create",
        operation_key="old-create-key",
        text="the original request",
        schedule_at_utc=past_time,
    )
    result = await manage_scheduled_message(args)

    assert payload_seen["schedule_at_utc"] == past_time
    assert payload_seen["operation_key"] == "old-create-key"
    assert result.is_error is False
    assert result.content == ()
    assert result.structured_content == receipt_response


async def test_success_is_structured_only_and_error_keeps_outcome_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    response: dict[str, object] = {
        "ok": True,
        "action": "create",
        "operation_key": "create-1",
        "account_id": 7,
        "dialog_id": 123,
        "scheduled_message_id": 500,
        "outcome": "acknowledged",
        "mutation_dispatched": True,
        "acknowledged": True,
        "receipt_persisted": True,
        "current_is_operation_confirmation": True,
        "schedule_at_utc": "2099-01-01T00:00:00Z",
    }
    request = AsyncMock(return_value=response)

    class Conn:
        async def __aenter__(self) -> Conn:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def request(self, payload: dict[str, object]) -> dict[str, object]:
            assert payload["method"] == "manage_scheduled_message"
            return cast(dict[str, object], await request(payload))

    @asynccontextmanager
    async def connection() -> AsyncIterator[Conn]:
        yield Conn()

    monkeypatch.setattr("mcp_telegram.tools.scheduled.daemon_connection", connection)
    success = await manage_scheduled_message(
        ManageScheduledMessage(
            exact_dialog_id=123,
            action="create",
            operation_key="create-1",
            text="hello",
            schedule_at_utc="2099-01-01T00:00:00Z",
        )
    )
    assert success.content == ()
    assert success.structured_content == response

    request.return_value = {
        "ok": False,
        "error": "scheduled_write_unknown",
        "message": "The remote result is unknown.",
        "required_action": "Read the local queue and replay the same key for receipt lookup.",
        "outcome": "unknown",
        "operation_key": "adjust-1",
        "mutation_dispatched": True,
        "receipt_persisted": True,
    }
    failure = await manage_scheduled_message(
        ManageScheduledMessage(
            exact_dialog_id=123,
            action="update",
            operation_key="adjust-1",
            scheduled_message_id=500,
            expected_version="v1",
            text="updated",
        )
    )
    assert failure.is_error is True
    result = cast(dict[str, object], failure.structured_content)
    assert result["details"] == {
        "outcome": "unknown",
        "operation_key": "adjust-1",
        "mutation_dispatched": True,
        "receipt_persisted": True,
    }
    content = failure.content[0]
    assert isinstance(content, TextContent)
    assert "Action:" in content.text
