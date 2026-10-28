from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from pydantic import ValidationError

from mcp_telegram.daemon_client import DaemonNotRunningError
from mcp_telegram.tools import drafts


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
