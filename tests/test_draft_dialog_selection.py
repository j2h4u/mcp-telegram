from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from mcp_telegram.dialog_selector import SELF_DIALOG_ALIASES, required_dialog_selector
from mcp_telegram.resolver import Candidates, Resolved
from mcp_telegram.tools import drafts
from tests.test_daemon_api import _make_db_with_dialogs, _seed_dialog_row, make_server
from tests.test_draft_commands import _Harness
from tests.test_draft_commands import harness as harness


@pytest.mark.parametrize("tool", [drafts.GetDraft, drafts.ManageDraft])
@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"dialog": "Other", "exact_dialog_id": 42},
        {"exact_dialog_id": True},
        {"exact_dialog_id": 0},
        {"exact_dialog_id": 2**63},
        {"dialog": "0"},
    ],
)
def test_draft_tools_reject_invalid_selectors(tool: type, selector: dict[str, object]) -> None:
    values: dict[str, object] = dict(selector)
    if tool is drafts.ManageDraft:
        values.update(action="create", operation_key="selector-test", text="draft")

    with pytest.raises(ValidationError):
        tool.model_validate(values)


@pytest.mark.parametrize(
    ("selector", "expected"),
    [("name", "dialog"), ("exact", "dialog_id")],
)
async def test_public_selector_maps_to_internal_ipc_fields(
    monkeypatch: pytest.MonkeyPatch, selector: str, expected: str
) -> None:
    received: dict[str, object] = {}

    class Connection:
        async def request(self, payload: dict[str, object]) -> dict[str, object]:
            received.update(payload)
            return {
                "ok": True,
                "current": {"presence": "absent", "uncertain": False},
                "acquisition_status": "ready",
                "coverage_status": "complete",
                "history": [],
                "next_action": "Review in Telegram.",
            }

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    args = drafts.GetDraft(dialog="  Alice  ") if selector == "name" else drafts.GetDraft(exact_dialog_id=42)
    result = await drafts.get_draft(args)

    assert not result.is_error
    assert received[expected] == ("Alice" if selector == "name" else 42)
    assert "dialog" not in received or expected == "dialog"
    assert "dialog_id" not in received or expected == "dialog_id"
    assert "exact_dialog_id" not in received


@pytest.mark.parametrize("alias", sorted(SELF_DIALOG_ALIASES))
async def test_self_aliases_override_real_dialog_name(alias: str) -> None:
    conn = _make_db_with_dialogs()
    _seed_dialog_row(conn, 42, name="Me", type_="user")
    server = make_server(conn)
    server.self_id = 9001

    resolved = await server._resolve_dialog_id_local(required_dialog_selector(dialog=f"  {alias.upper()}  "))

    assert int(resolved) == 9001


async def test_self_alias_requires_bound_identity_and_topics_reject_saved_messages() -> None:
    server = make_server(_make_db_with_dialogs())
    missing_identity = await server._draft_request({"dialog": "self"})
    assert missing_identity["error"] == "daemon_not_ready"

    server.self_id = 9001
    result = await server._dispatch({"method": "list_topics", "dialog": "saved messages"})
    assert result["error"] == "saved_messages_not_supported"

    messages = await server._dispatch({"method": "list_messages", "dialog": "self", "limit": 10})
    assert messages["ok"] is True
    assert messages["data"]["is_self"] is True


async def test_draft_request_resolves_local_name_and_returns_plain_int() -> None:
    conn = _make_db_with_dialogs()
    _seed_dialog_row(conn, 808, name="Alice", type_="user")
    server = make_server(conn)
    server.self_id = 42

    by_name = await server._draft_request({"dialog": " Alice "})
    by_id = await server._draft_request({"dialog_id": 808})

    assert by_name["dialog_id"] == 808 and type(by_name["dialog_id"]) is int
    assert by_id["dialog_id"] == 808 and type(by_id["dialog_id"]) is int


@pytest.mark.parametrize("selector", ["@self", "https://t.me/self"])
async def test_reserved_aliases_do_not_capture_username_or_link_selectors(selector: str) -> None:
    server = make_server(_make_db_with_dialogs())
    server.self_id = 42
    server._resolve_dialog_name = lambda _query: Resolved(500, "Other")  # type: ignore[method-assign]

    result = await server._draft_request({"dialog": selector})

    assert result["dialog_id"] == 500


async def test_get_draft_dispatch_reads_locally_resolved_self(harness: _Harness) -> None:
    server = make_server(harness.conn)
    server.self_id = 100
    server.bind_draft_commands(harness.owner)

    result = await server._dispatch({"method": "get_draft", "dialog": "self"})

    assert result["ok"] is True
    assert result["current"]["presence"] == "absent"
    assert result["acquisition_status"] == "ready"
    assert harness.requests == []


async def test_operation_key_replay_uses_resolved_target_and_retargeting_conflicts(harness: _Harness) -> None:
    server = make_server(harness.conn)
    server.self_id = 100
    server.bind_draft_commands(harness.owner)
    server._resolve_dialog_name = lambda query: Resolved(200 if query == "Alice" else 201, query)  # type: ignore[method-assign]
    by_name = {
        "method": "manage_draft",
        "dialog": "Alice",
        "action": "create",
        "operation_key": "canonical-target",
        "text": "draft text",
    }

    first = await server._dispatch(by_name)
    replay = await server._dispatch({**by_name, "dialog": None, "dialog_id": 200})
    retargeted = await server._dispatch({**by_name, "dialog": "Bob"})

    assert first["ok"] is True
    assert replay["ok"] is True
    assert len(harness.requests) == 1
    assert retargeted["ok"] is False
    assert retargeted["error"] == "operation_conflict"
    assert len(harness.requests) == 1


@pytest.mark.parametrize("tool", ["get_draft", "manage_draft"])
async def test_draft_ambiguity_keeps_candidates_and_trust_envelope(monkeypatch: pytest.MonkeyPatch, tool: str) -> None:
    response = {
        "ok": False,
        "error": "ambiguous_dialog",
        "message": "Dialog 'Team' matched multiple dialogs.",
        "candidates": [
            {"entity_id": 1, "display_name": "Team One", "username": "teamone", "score": 100, "entity_type": "user"},
            {"entity_id": 2, "display_name": "Team Two", "username": "teamtwo", "score": 100, "entity_type": "user"},
        ],
        "directory_coverage": {"status": "complete"},
        "required_action": "Retry with an exact dialog id.",
    }

    class Connection:
        async def request(self, _payload: dict[str, object]) -> dict[str, object]:
            return response

    @asynccontextmanager
    async def connection() -> AsyncIterator[Connection]:
        yield Connection()

    monkeypatch.setattr(drafts, "daemon_connection", connection)
    if tool == "manage_draft":
        result = await drafts.manage_draft(
            drafts.ManageDraft(dialog="Team", action="create", operation_key="ambiguous", text="draft")
        )
    else:
        result = await drafts.get_draft(drafts.GetDraft(dialog="Team"))

    assert result.is_error
    content = result.structured_content
    assert content["error"] == "ambiguous_dialog"
    candidates = content["candidates"]
    assert len(candidates) == 2
    assert all(candidate["trust"]["is_untrusted"] for candidate in candidates)


@pytest.mark.parametrize("method", ["get_draft", "manage_draft"])
async def test_daemon_ambiguity_preserves_candidates_and_skips_command(method: str) -> None:
    server = make_server(_make_db_with_dialogs())
    server.self_id = 42
    command = SimpleNamespace(execute=AsyncMock())
    server.bind_draft_commands(command)  # type: ignore[arg-type]
    server._resolve_dialog_name = lambda query: Candidates(
        query,
        [
            {"entity_id": 1, "display_name": "Team One", "score": 100, "username": "teamone", "entity_type": "user"},
            {"entity_id": 2, "display_name": "Team Two", "score": 100, "username": "teamtwo", "entity_type": "user"},
        ],
    )  # type: ignore[method-assign]
    payload: dict[str, object] = {"method": method, "dialog": "Team"}
    if method == "manage_draft":
        payload.update(action="create", operation_key="ambiguous", text="draft")

    result = await server._dispatch(payload)

    assert result["error"] == "ambiguous_dialog"
    assert {candidate["entity_id"] for candidate in result["candidates"]} == {1, 2}
    command.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "payload",
    [
        {"method": "get_draft"},
        {"method": "get_draft", "dialog_id": True},
        {"method": "get_draft", "dialog_id": 0},
        {"method": "get_draft", "dialog_id": 2**63},
        {"method": "get_draft", "dialog_id": 42, "dialog": "Alice"},
        {"method": "get_draft", "dialog": "0"},
        {"method": "get_draft", "dialog": "Alice", "account_id": 43},
        {"method": "get_draft", "dialog": "Alice", "account_id": True},
    ],
)
async def test_draft_ipc_boundary_rejects_invalid_selector_or_account(payload: dict[str, object]) -> None:
    server = make_server(_make_db_with_dialogs())
    server.self_id = 42

    result = await server._dispatch(payload)

    assert result["ok"] is False
    assert result["error"] in {"missing_dialog", "invalid_dialog_selector", "account_mismatch"}


@pytest.mark.parametrize("method", ["get_draft", "manage_draft"])
@pytest.mark.parametrize(
    "selector",
    [
        {},
        {"dialog_id": True},
        {"dialog_id": 42, "dialog": "Alice"},
        {"dialog": "Alice", "account_id": 43},
    ],
)
async def test_both_draft_ipc_methods_validate_at_daemon_boundary(method: str, selector: dict[str, object]) -> None:
    server = make_server(_make_db_with_dialogs())
    server.self_id = 42
    payload = {"method": method, **selector}
    if method == "manage_draft":
        payload.update(action="create", operation_key="invalid-selector", text="draft")

    result = await server._dispatch(payload)

    assert result["ok"] is False
    assert result["error"] in {"missing_dialog", "invalid_dialog_selector", "account_mismatch"}
