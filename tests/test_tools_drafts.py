from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from jsonschema import validate
from pydantic import ValidationError

from mcp_telegram.daemon_client import DaemonNotRunningError
from mcp_telegram.tools import drafts
from mcp_telegram.tools._base import normalize_output_schema


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
        drafts.ManageDraft.model_validate({"dialog_id": 42, "operation_key": "test", **extra})


async def test_uncertain_ipc_failure_instructs_reusing_same_operation_key(monkeypatch: pytest.MonkeyPatch) -> None:
    class Connection:
        async def request(self, _payload: dict[str, object]) -> dict[str, object]:
            raise DaemonNotRunningError("response timed out", kind="response_timeout")

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    result = await drafts.manage_draft(
        drafts.ManageDraft(dialog_id=42, action="create", operation_key="same-key", text="draft")
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
            drafts.ManageDraft(dialog_id=42, action="create", operation_key="same-key", text="draft")
        )
        schema = drafts.MANAGE_DRAFT_OUTPUT_SCHEMA
    else:
        result = await drafts.get_draft(drafts.GetDraft(dialog_id=42))
        schema = drafts.GET_DRAFT_OUTPUT_SCHEMA
    assert not result.is_error
    assert not result.content
    assert "request_id" not in str(result.structured_content)
    normalized_schema = normalize_output_schema(schema)
    assert normalized_schema is not None
    validate(result.structured_content, normalized_schema)
