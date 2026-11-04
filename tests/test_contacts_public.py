"""Public contacts validation and composed MCP/daemon boundary."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import validate
from mcp.types import TextContent
from pydantic import ValidationError

from mcp_telegram import server as mcp_server
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_api import DraftCommands
from mcp_telegram.daemon_client import DaemonConnection
from mcp_telegram.tools import _base
from mcp_telegram.tools.contacts import ManageContact
from tests.test_daemon_api import make_server


@pytest.mark.parametrize(
    "action, names", [("create", {"first_name": "Ada"}), ("update", {"last_name": ""}), ("delete", {})]
)
async def test_contact_commands_cross_actual_mcp_and_unix_ipc(  # noqa: PLR0915 - actual IPC composition
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str, names: dict[str, str]
) -> None:
    api = make_server()
    api.self_id = 42
    await api._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    incoming = AsyncMock(wraps=api._handle_client_line)
    monkeypatch.setattr(api, "_handle_client_line", incoming)
    owner = MagicMock()
    execute = AsyncMock(
        return_value={
            "ok": True,
            "account_id": 42,
            "entity_id": 43,
            "operation_key": "op",
            "outcome": "rejected" if action == "delete" else "unknown",
            "verification": "pending",
            **(
                {
                    "telegram_error": {"type": "UserPrivacyRestrictedError", "code": 403},
                    "required_action": "Check the contact and use a new operation key for a new intent.",
                }
                if action == "delete"
                else {}
            ),
        }
    )
    owner.execute = execute
    api.bind_contact_commands(cast(DraftCommands, owner))
    note_owner = MagicMock()
    note_owner.execute = AsyncMock(return_value={"error": "contact_required"})
    api.bind_contact_note_commands(cast(DraftCommands, note_owner))
    socket_path = tmp_path / "contacts.sock"

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
    args = {"entity_id": 43, "action": action, "expected_version": "a" * 64, "operation_key": "op", **names}
    async with await asyncio.start_unix_server(api.handle_client, path=socket_path):
        result = await mcp_server.call_tool("manage_contact", args)
        async with connection() as direct:
            invalid = await direct.request({"method": "manage_contact", **args, "unexpected": "invalid"})
        note = await mcp_server.call_tool(
            "manage_contact_note",
            {
                "dialog_id": 43,
                "action": "create",
                "text": "note",
                "expected_version": "a" * 64,
                "operation_key": "note-op",
            },
        )
    assert result.is_error is not True
    assert invalid["error"] == "invalid_arguments"
    payload = cast(dict[str, object], result.structured_content)
    assert payload["outcome"] == ("rejected" if action == "delete" else "unknown")
    if action == "delete":
        assert payload["telegram_error"] == {"type": "UserPrivacyRestrictedError", "code": 403}
        assert isinstance(payload["required_action"], str)
        schema = cast(dict[str, object] | None, mcp_server.tool_by_name["manage_contact"].output_schema)
        assert schema is not None
        validate(payload, schema)
    execute.assert_awaited_once_with(args)
    requests = [cast(dict[str, object], json.loads(cast(bytes, call.args[0]))) for call in incoming.await_args_list]
    request = next(item for item in requests if item["method"] == "manage_contact")
    assert isinstance(request["operation_id"], str) and isinstance(request["request_id"], str)
    assert note.is_error is True
    text = cast(TextContent, note.content[0]).text
    assert "manage_contact" in text and "new operation key" in text


@pytest.mark.parametrize(
    "action, names", [("create", {}), ("create", {"first_name": " "}), ("update", {}), ("delete", {"last_name": None})]
)
def test_contact_command_rejects_invalid_action_fields(action: str, names: dict[str, str | None]) -> None:
    with pytest.raises(ValidationError):
        ManageContact.model_validate(
            {"entity_id": 43, "action": action, "expected_version": "a" * 64, "operation_key": "op", **names}
        )


def test_contact_version_requires_lowercase_hex() -> None:
    with pytest.raises(ValidationError):
        ManageContact(entity_id=43, action="delete", expected_version="z" * 64, operation_key="op")


async def test_note_contact_required_gives_new_command_guidance() -> None:
    api = make_server()
    api.self_id = 42
    await api._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    owner = MagicMock()
    owner.execute = AsyncMock(return_value={"error": "contact_required"})
    api.bind_contact_note_commands(cast(DraftCommands, owner))
    response = await api._manage_contact_note(
        {
            "method": "manage_contact_note",
            "operation_id": "test",
            "dialog_id": 43,
            "action": "clear",
            "operation_key": "op",
            "expected_version": "a" * 64,
        }
    )
    action = cast(str, response["required_action"])
    assert response["error"] == "contact_required"
    assert "No note was cleared" in action and "manage_contact" not in action
    created = await api._manage_contact_note(
        {"dialog_id": 43, "action": "create", "text": "note", "operation_key": "op", "expected_version": "a" * 64}
    )
    action = cast(str, created["required_action"])
    assert "manage_contact" in action and "current note version" in action and "new operation key" in action


async def test_list_contacts_reads_canonical_non_dialog_contact_without_rpc(  # noqa: PLR0915 - real producer and IPC
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telethon.tl import types

    from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
    from mcp_telegram.sync_transactions import write_transaction
    from sqlite_writer_helpers import event_manager

    path = tmp_path / "contacts.db"
    ensure_sync_schema(path)
    db = _open_sync_db(path)
    client = MagicMock(side_effect=AssertionError("local contact read must not call Telegram"))
    manager = event_manager(client, db, asyncio.Event())
    manager.set_self_id(42)
    with write_transaction(db):
        db.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (55,'user','Public',1)")
    manager._write_profile_pushes(db, object(), [types.User(55, contact=True, first_name="Saved")], [])
    api = make_server(db, client=client)
    api.self_id = 42
    await api._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    socket_path = tmp_path / "list.sock"

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
    from mcp_telegram.tools import entity_info

    monkeypatch.setattr(entity_info, "daemon_connection", connection)
    async with await asyncio.start_unix_server(api.handle_client, path=socket_path):
        result = await mcp_server.call_tool("list_contacts", {"limit": 1})
        empty = await mcp_server.call_tool("list_contacts", {"limit": 1, "after_entity_id": 55})
        detail = await mcp_server.call_tool("get_entity_info", {"exact_entity_id": 55})
    assert result.is_error is not True and empty.is_error is not True
    payload = cast(dict[str, object], result.structured_content)
    items = cast(list[dict[str, object]], payload["items"])
    assert items[0]["entity_id"] == 55 and items[0]["first_name"] == "Saved"
    assert cast(dict[str, object], payload["coverage"])["state"] == "unknown"
    assert cast(dict[str, object], empty.structured_content)["items"] == []
    assert detail.is_error is not True
    profile = cast(dict[str, object], detail.structured_content)
    schema = cast(dict[str, object] | None, mcp_server.tool_by_name["get_entity_info"].output_schema)
    assert schema is not None
    validate(profile, schema)
    assert cast(dict[str, object], profile["contact"])["membership"] is True
    relationships = cast(dict[str, object], profile["relationships"])
    assert cast(dict[str, object], relationships["contact"])["contact"] is True
    type_specific = cast(dict[str, object], profile["type_specific"])
    assert cast(dict[str, object], type_specific["relationship"])["contact"] is True
    assert db.execute("SELECT 1 FROM dialogs WHERE dialog_id=55").fetchone() is None
    client.assert_not_called()
    db.close()
