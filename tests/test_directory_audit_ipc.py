"""The directory cursor stays opaque across the real daemon socket boundary."""

import asyncio
import threading
from pathlib import Path
from typing import cast

import pytest
from jsonschema import validate
from mcp.types import TextContent

from mcp_telegram import server as mcp_server
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from tests.test_mcp_account_protection import _install_daemon_socket, _local_daemon


async def _assert_fractional_directory_bounds() -> None:
    for bound, expected in (
        ({"last_message_since_utc": "1970-01-01T00:01:40.5Z"}, []),
        ({"last_message_until_utc": "1970-01-01T00:01:40.5Z"}, [1, 2]),
        ({"last_message_since_utc": "1970-01-01T00:01:40.1Z", "last_message_until_utc": "1970-01-01T00:01:40.9Z"}, []),
    ):
        result = await mcp_server.call_tool("list_dialogs", {"dialog_type": "user", "exclude_self": True, **bound})
        assert result.is_error is False
        rows = cast(list[dict[str, object]], cast(dict[str, object], result.structured_content)["dialogs"])
        assert [row["id"] for row in rows] == expected


@pytest.mark.asyncio
async def test_directory_audit_registered_mcp_real_ipc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, conn, feedback, _ = _local_daemon(tmp_path, object())
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,name,type,hidden,last_message_at) VALUES (?,?,?,0,?)",
        [(1, "one", "user", 100), (2, "two", "user", 100), (42, "self", "user", 100), (50, "bot", "bot", 100)],
    )
    conn.commit()
    daemon.self_id = 42
    await daemon._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    socket = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket):
            args = {"dialog_type": "user", "exclude_self": True, "limit": 1}
            first = await mcp_server.call_tool("list_dialogs", args)
            assert first.is_error is False and first.content == []
            payload = cast(dict[str, object], first.structured_content)
            schema = mcp_server.tool_by_name["list_dialogs"].output_schema
            assert schema is not None
            validate(payload, schema)
            assert [row["id"] for row in cast(list[dict[str, object]], payload["dialogs"])] == [1]
            cursor = cast(dict[str, object], payload["navigation"])["next_navigation"]
            assert isinstance(cursor, str)
            second = await mcp_server.call_tool("list_dialogs", {**args, "navigation": cursor})
            assert second.is_error is False and second.content == []
            payload2 = cast(dict[str, object], second.structured_content)
            validate(payload2, schema)
            assert [row["id"] for row in cast(list[dict[str, object]], payload2["dialogs"])] == [2]
            changed = await mcp_server.call_tool("list_dialogs", {**args, "dialog_type": "bot", "navigation": cursor})
            assert changed.is_error is True
            assert isinstance(changed.content[0], TextContent)
            assert "restart" in changed.content[0].text.lower()
            malformed = await daemon._list_dialogs({"exclude_self": "false"})
            assert malformed["ok"] is False
            await _assert_fractional_directory_bounds()
    finally:
        conn.close()
        feedback.close()


@pytest.mark.asyncio
async def test_directory_discards_threaded_read_when_account_scope_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon, conn, feedback, _ = _local_daemon(tmp_path, object())
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.commit()
    daemon.self_id = 42
    await daemon._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    reader = daemon._get_reading_service()
    original = reader._list_dialogs_from_reader
    entered = threading.Event()
    release = threading.Event()

    def blocked_read(path: Path, req: dict[str, object]) -> dict:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test reader not released")
        return original(path, req)

    monkeypatch.setattr(reader, "_list_dialogs_from_reader", blocked_read)
    try:
        task = asyncio.create_task(daemon._list_dialogs({"navigation": "start"}))
        assert await asyncio.to_thread(entered.wait, 5)
        await daemon._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 100))
        release.set()
        result = await task
        assert result["error"] == "account_unverified"
        assert "data" not in result
    finally:
        release.set()
        conn.close()
        feedback.close()
