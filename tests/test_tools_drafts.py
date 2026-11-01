from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from unittest.mock import AsyncMock

import pytest
from jsonschema import validate
from pydantic import ValidationError
from telethon.errors import FloodWaitError

from mcp_telegram import server
from mcp_telegram.daemon_client import DaemonNotRunningError
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.tools import drafts
from mcp_telegram.tools._base import normalize_output_schema
from tests.test_draft_commands import _Harness
from tests.test_draft_commands import harness as harness
from tests.test_telegram_rpc_flood_outcome import _server_response
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.parametrize(
    "extra",
    [
        {"action": "update", "text": "new"},
        {"action": "clear", "expected_version": "version", "text": "unexpected"},
        {"action": "restore", "expected_version": "version"},
        {"action": "create", "text": ""},
        {"action": "send", "text": "must never send"},
    ],
)
def test_invalid_mutations_fail_before_daemon_contact(extra: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        drafts.ManageDraft.model_validate({"exact_dialog_id": 42, "operation_key": "test", **extra})


async def test_uncertain_ipc_failure_instructs_reusing_same_operation_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        async def request(self, _payload: dict[str, object]) -> dict[str, object]:
            raise DaemonNotRunningError("response timed out", kind="response_timeout")

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    result = await drafts.manage_draft(
        drafts.ManageDraft(exact_dialog_id=42, action="create", operation_key="same-key", text="draft")
    )
    assert result.is_error
    rendered = str(result.content)
    assert "same-key" in rendered
    assert "unconfirmed" in rendered
    assert "do not blindly" in rendered


@pytest.mark.parametrize("mutation", [False, True])
async def test_successful_draft_tools_validate_schema_without_ipc_metadata(
    monkeypatch: pytest.MonkeyPatch, mutation: bool
) -> None:
    response: dict[str, object] = {
        "ok": True,
        "request_id": "internal-correlation-id",
        "current": {"presence": "absent", "uncertain": False, "text": None},
        "next_action": "Review in Telegram.",
    }
    if mutation:
        response.update(
            outcome="acknowledged",
            operation_key="same-key",
            mutation_dispatched=True,
            save_acknowledged=True,
            result_persisted=True,
            current_is_operation_confirmation=False,
            backup_history_id=None,
        )
    else:
        response.update(history=[], acquisition_status="ready", coverage_status="complete")

    class Connection:
        async def request(self, payload: dict[str, object]) -> dict[str, object]:
            assert payload["method"] == ("manage_draft" if mutation else "get_draft")
            return dict(response)

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    if mutation:
        result = await drafts.manage_draft(
            drafts.ManageDraft(exact_dialog_id=42, action="create", operation_key="same-key", text="draft")
        )
        schema = drafts.MANAGE_DRAFT_OUTPUT_SCHEMA
    else:
        result = await drafts.get_draft(drafts.GetDraft(exact_dialog_id=42))
        schema = drafts.GET_DRAFT_OUTPUT_SCHEMA
    assert not result.is_error
    assert not result.content
    assert "request_id" not in str(result.structured_content)
    normalized_schema = normalize_output_schema(schema)
    assert normalized_schema is not None
    validate(result.structured_content, normalized_schema)
    if mutation:
        assert result.structured_content is not None
        assert result.structured_content["verification"] == {
            "status": "unverified",
            "basis": "none",
            "reason": "legacy_receipt",
        }


@pytest.mark.parametrize(
    "status,basis", [("verified", "post_write_snapshot"), ("conflict", "post_write_snapshot"), ("unverified", "none")]
)
async def test_mutation_verification_is_preserved_at_tool_boundary(
    monkeypatch: pytest.MonkeyPatch, status: str, basis: str
) -> None:
    verification = {"status": status, "basis": basis}

    class Connection:
        async def request(self, _payload: dict[str, object]) -> dict[str, object]:
            return {
                "ok": True,
                "outcome": "acknowledged",
                "operation_key": "key",
                "result_persisted": True,
                "current_is_operation_confirmation": status == "verified",
                "verification": verification,
                "current": {"presence": "present", "uncertain": False, "expected_version": "v1"},
                "next_action": "Review the verification result.",
            }

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    result = await drafts.manage_draft(
        drafts.ManageDraft(exact_dialog_id=42, action="create", operation_key="key", text="draft")
    )
    assert result.structured_content is not None
    assert result.structured_content["verification"] == verification
    schema = normalize_output_schema(drafts.MANAGE_DRAFT_OUTPUT_SCHEMA)
    assert schema is not None
    validate(result.structured_content, schema)


def test_mutation_error_preserves_verification_details() -> None:
    verification = {"status": "unverified", "basis": "none", "reason": "interrupted"}
    result = drafts._draft_error_response({"ok": False, "error": "draft_failed", "verification": verification})
    assert result is not None and result.structured_content is not None
    assert result.structured_content["details"] == {"verification": verification}


async def test_legacy_unchanged_receipt_is_projected_without_changing_stored_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {"presence": "present", "uncertain": False, "expected_version": "saved-version", "text": "saved"}
    receipt: dict[str, object] = {
        "ok": True,
        "outcome": "unchanged",
        "operation_key": "old-key",
        "result_persisted": True,
        "current_is_operation_confirmation": True,
        "backup_history_id": 19,
        "current": current,
        "next_action": "Review in Telegram.",
    }
    original = {**receipt, "current": dict(current)}
    calls = 0

    class Connection:
        async def request(self, _payload: dict[str, object]) -> dict[str, object]:
            nonlocal calls
            calls += 1
            return receipt

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    result = await drafts.manage_draft(
        drafts.ManageDraft(
            exact_dialog_id=42, action="clear", operation_key="old-key", expected_version="saved-version"
        )
    )
    assert calls == 1
    assert receipt == original
    assert result.structured_content is not None
    assert result.structured_content["verification"] == {
        "status": "unverified",
        "basis": "none",
        "reason": "legacy_receipt",
    }
    assert result.structured_content["current_is_operation_confirmation"] is False
    for field in ("outcome", "current", "backup_history_id"):
        assert result.structured_content[field] == original[field]


@pytest.mark.parametrize("prepared", [False, True])
async def test_finite_throttle_retains_structured_action_and_receipt_through_mcp(
    monkeypatch: pytest.MonkeyPatch, harness: _Harness, wire: _WireHarness, prepared: bool
) -> None:
    gate = wire[0]
    if prepared:
        _server_response(wire, monkeypatch, FloodWaitError(None, capture=20))
    else:

        def pause() -> None:
            raise TelegramRpcThrottled(retry_after_seconds=20)

        harness.after_preflight = pause
    owner = harness.new_owner(gate)

    class Connection:
        async def request(self, payload: dict[str, object]) -> dict[str, object]:
            return {**await owner.execute(payload), "request_id": "internal"}

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    monkeypatch.setattr(server, "_schedule_telemetry", lambda _event: None)
    monkeypatch.setattr(
        server, "_account_protection_status", AsyncMock(return_value=server._account_protection_unavailable())
    )
    result = await server.call_tool(
        "manage_draft", {"exact_dialog_id": 200, "action": "create", "operation_key": "flood-mcp", "text": "unsent"}
    )
    assert result.is_error
    payload = cast(dict[str, object], result.structured_content)
    error = cast(dict[str, object], payload["error"])
    details = cast(dict[str, object], error["details"])
    assert "20 seconds" in str(error["action"])
    assert ("new operation_key" if prepared else "same operation_key") in str(error["action"])
    assert details["outcome"] == "rejected"
    assert details["operation_key"] == "flood-mcp"
    assert details["result_persisted"] is prepared
    assert details["mutation_dispatched"] is prepared
    assert details["request_rejected"] is prepared
    assert details["retry_after_seconds"] == 20
