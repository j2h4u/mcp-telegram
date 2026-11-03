"""Local read and strict daemon command boundaries for native management."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint
from mcp_telegram.tools.folders import FOLDER_OUTPUT_SCHEMA, ManageFolder
from tests.test_daemon_api import make_server


@pytest.mark.asyncio
async def test_folder_read_uses_retained_facts_without_telegram(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    with write_savepoint(conn):
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=42,generation=7,observation_started_at=10,observation_completed_at=11 WHERE singleton=1"
        )
    SQLiteFolderSnapshotRepository(conn).project_observation(
        FolderRuleObservation((FolderRule(4, "Retained", included_ids=(43,)),), "token", 100), completed_at=100
    )
    server = make_server(conn)
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    server._client.get_entity = AsyncMock(side_effect=AssertionError("read must not contact Telegram"))
    response = await server._get_folder({"folder_id": 4})
    definition = response["definition"]
    assert isinstance(definition, dict)
    assert response["presence"] == "present" and definition["title"] == "Retained"
    Draft202012Validator(FOLDER_OUTPUT_SCHEMA).validate(response)
    assert (await server._get_folder({"folder_id": 999}))["presence"] == "absent"
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 99, 2, 99))
    server.self_id = 99
    assert (await server._get_folder({"folder_id": 4}))["presence"] == "unknown"
    conn.close()


def test_action_specific_json_schema_is_strict() -> None:
    validator = Draft202012Validator(ManageFolder.model_json_schema())
    validator.validate(
        {
            "action": "update",
            "operation_key": "op",
            "folder_id": 4,
            "expected_version": "v",
            "clear_excluded_dialog_ids": [43],
        }
    )
    assert not validator.is_valid({"action": "create", "operation_key": "op", "title": "x", "dialog_ids": [43]})
    assert not validator.is_valid(
        {
            "action": "delete",
            "operation_key": "op",
            "folder_id": 4,
            "expected_version": "v",
            "clear_excluded_dialog_ids": [43],
        }
    )


@pytest.mark.asyncio
async def test_public_adapters_return_structured_data_and_preserve_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from mcp_telegram.tools import _base
    from mcp_telegram.tools.contact_notes import ManageContactNote, manage_contact_note
    from mcp_telegram.tools.folders import GetFolder, get_folder, manage_folder

    requests = []

    async def request(payload: dict[str, object]) -> dict[str, object]:
        requests.append(payload)
        if payload["method"] == "get_folder":
            return {"ok": True, "account_id": 42, "presence": "unknown", "catalog_version": None}
        if payload["method"] == "manage_contact_note":
            return {
                "ok": True,
                "account_id": 42,
                "operation_key": "note",
                "outcome": "unknown",
                "verification": "pending",
            }
        return {
            "ok": True,
            "account_id": 42,
            "operation_key": "folder",
            "folder_id": 4,
            "phase": "completed",
            "outcome": "unknown",
            "dispatch": "unknown",
            "acknowledgement": "unknown",
            "verification": "pending",
            "next_action": "Use the same key.",
        }

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(request=request)

    monkeypatch.setattr(_base, "daemon_connection", connection)
    read = await get_folder(GetFolder(folder_id=4))
    folder = await manage_folder(
        ManageFolder(action="delete", operation_key="folder", folder_id=4, expected_version="v")
    )
    note = await manage_contact_note(
        ManageContactNote(dialog_id=43, action="clear", operation_key="note", expected_version="a" * 64)
    )
    assert (
        read.structured_content is not None
        and folder.structured_content is not None
        and note.structured_content is not None
    )
    assert not read.content and read.structured_content["presence"] == "unknown"
    assert not folder.content and folder.structured_content["outcome"] == "unknown"
    assert not note.content and note.structured_content["outcome"] == "unknown"
    assert all("timezone" not in payload for payload in requests)


@pytest.mark.asyncio
async def test_daemon_mutations_require_bound_owner_and_validate_complete_request(tmp_path: Path) -> None:
    from typing import cast
    from unittest.mock import MagicMock

    from mcp_telegram.daemon_api import DraftCommands

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    server = make_server(conn)
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    assert (await server._manage_folder({}))["error"] == "daemon_not_ready"
    folder = MagicMock()
    folder_execute = AsyncMock(return_value={"ok": True, "outcome": "unchanged"})
    folder.execute = folder_execute
    server.bind_folder_commands(cast(DraftCommands, folder))
    invalid = {
        "method": "manage_folder",
        "action": "delete",
        "operation_key": "op",
        "folder_id": 4,
        "expected_version": "v",
        "add_dialog_ids": [43],
    }
    assert (await server._manage_folder(invalid))["error"] == "invalid_arguments"
    folder_execute.assert_not_called()
    valid = {
        "method": "manage_folder",
        "action": "update",
        "operation_key": "op",
        "folder_id": 4,
        "expected_version": "v",
        "clear_excluded_dialog_ids": [43],
    }
    assert (await server._manage_folder(valid))["outcome"] == "unchanged"
    assert (await server._manage_folder({**valid, "unexpected": 1}))["error"] == "invalid_arguments"
    folder_execute.assert_awaited_once_with({key: value for key, value in valid.items() if key != "method"})
    notes = MagicMock()
    notes_execute = AsyncMock(return_value={"outcome": "unknown", "verification": "pending"})
    notes.execute = notes_execute
    server.bind_contact_note_commands(cast(DraftCommands, notes))
    bad_note = {
        "method": "manage_contact_note",
        "dialog_id": 43,
        "action": "clear",
        "operation_key": "n",
        "expected_version": "v",
        "account_id": 7,
    }
    assert (await server._manage_contact_note(bad_note))["error"] == "invalid_arguments"
    notes_execute.assert_not_called()
    note = {key: value for key, value in bad_note.items() if key != "account_id"}
    assert (await server._manage_contact_note({**note, "unexpected": 1}))["error"] == "invalid_arguments"
    assert (await server._manage_contact_note(note))["outcome"] == "unknown"
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (
            "manage_folder",
            {
                "action": "create",
                "operation_key": "create",
                "title": "MCP check",
                "dialog_ids": [43],
                "expected_catalog_version": "folder-v1:42:1:token",
            },
        ),
        (
            "manage_folder",
            {
                "action": "update",
                "operation_key": "update",
                "folder_id": 4,
                "expected_version": "folder-v1:42:1:token",
                "add_dialog_ids": [43],
            },
        ),
        (
            "manage_folder",
            {"action": "delete", "operation_key": "delete", "folder_id": 4, "expected_version": "folder-v1:42:1:token"},
        ),
        (
            "manage_contact_note",
            {
                "action": "create",
                "operation_key": "note-create",
                "dialog_id": 43,
                "expected_version": "a" * 64,
                "text": "hello",
            },
        ),
        (
            "manage_contact_note",
            {
                "action": "update",
                "operation_key": "note-update",
                "dialog_id": 43,
                "expected_version": "a" * 64,
                "text": "hello",
            },
        ),
        (
            "manage_contact_note",
            {"action": "clear", "operation_key": "note-clear", "dialog_id": 43, "expected_version": "a" * 64},
        ),
    ],
)
async def test_native_commands_preserve_arguments_through_mcp_and_real_ipc(  # noqa: PLR0915 - full IPC composition
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, arguments: dict[str, object]
) -> None:
    import asyncio
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager
    from typing import cast
    from unittest.mock import MagicMock

    from mcp_telegram import server as mcp_server
    from mcp_telegram.daemon_api import DraftCommands
    from mcp_telegram.daemon_client import DaemonConnection
    from mcp_telegram.tools import _base

    api = make_server()
    api.self_id = 42
    await api._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    handle_line = AsyncMock(wraps=api._handle_client_line)
    monkeypatch.setattr(api, "_handle_client_line", handle_line)
    folder = MagicMock()
    folder_execute = AsyncMock(
        return_value={
            "ok": True,
            "account_id": 42,
            "operation_key": arguments["operation_key"],
            "folder_id": 4,
            "phase": "completed",
            "outcome": "unknown",
            "dispatch": "unknown",
            "acknowledgement": "unknown",
            "verification": "pending",
            "next_action": "Inspect the same operation key.",
        }
    )
    folder.execute = folder_execute
    api.bind_folder_commands(cast(DraftCommands, folder))
    note = MagicMock()
    note_execute = AsyncMock(return_value={"outcome": "unknown", "verification": "pending"})
    note.execute = note_execute
    api.bind_contact_note_commands(cast(DraftCommands, note))
    socket_path = tmp_path / "native.sock"

    @asynccontextmanager
    async def connection() -> AsyncIterator[DaemonConnection]:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        try:
            yield DaemonConnection(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setattr(_base, "daemon_connection", connection)
    monkeypatch.setattr(mcp_server, "daemon_connection", connection)
    async with await asyncio.start_unix_server(api.handle_client, path=socket_path):
        result = await mcp_server.call_tool(tool, arguments)
    assert result.is_error is not True
    payload = cast(dict[str, object] | None, result.structured_content)
    assert payload is not None and payload["outcome"] == "unknown"
    requests = [cast(dict[str, object], json.loads(cast(bytes, call.args[0]))) for call in handle_line.await_args_list]
    wire = next(request for request in requests if request["method"] == tool)
    assert isinstance(wire["operation_id"], str) and isinstance(wire["request_id"], str)
    if tool == "manage_folder":
        folder_execute.assert_awaited_once_with(arguments)
    else:
        note_execute.assert_awaited_once()
        request = cast(dict[str, object], note_execute.await_args_list[0].args[0])
        assert {key: request[key] for key in arguments} == arguments
        assert isinstance(request["operation_id"], str)
        assert isinstance(request["request_id"], str)
