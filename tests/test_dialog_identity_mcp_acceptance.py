from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import sys
import time
from collections.abc import Callable
from http.client import HTTPResponse
from pathlib import Path
from typing import NoReturn, cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from mcp_telegram.config import load_config
from mcp_telegram.daemon_ipc import get_daemon_socket_path
from mcp_telegram.dialog_identity import capture_identity_baseline, publish_dialog_identity
from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation
from mcp_telegram.fts import INSERT_FTS_SQL, stem_text
from mcp_telegram.server import run_mcp_http_server
from test_daemon_api import make_server

_GROUP_ID = -10090001
_PENDING_GROUP_ID = -10090002
_ACCOUNT_ID = 90003
_GROUP_NAME = "Canonical acceptance group"
_GROUP_USERNAME = "canonical_acceptance_group"
_PENDING_NAME = "Pending cursor group"


class _FailOnRpcClient:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., NoReturn]:
        def reject(*_args: object, **_kwargs: object) -> NoReturn:
            self.attempts.append(name)
            raise AssertionError(f"unexpected Telegram RPC attempt: {name}")

        return reject


def _seed_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    identity: tuple[str, str | None],
    *,
    read_cursor: int | None,
) -> None:
    name, username = identity
    now = int(time.time())
    conn.execute(
        "INSERT INTO dialogs(dialog_id,name,type,username,identity_source,identity_complete,identity_observed_at) "
        "VALUES (?,NULL,'unknown',NULL,'legacy',0,?)",
        (dialog_id, now),
    )
    baseline = capture_identity_baseline(conn, dialog_id)
    assert publish_dialog_identity(
        conn,
        dialog_id,
        DialogIdentityObservation(
            dialog_id,
            name=name,
            username=username,
            dialog_type="supergroup",
            complete=True,
            source="directory",
            observed_at=now,
        ),
        baseline,
    )
    conn.execute(
        "UPDATE dialogs SET unread_count=1,unread_count_observed_at=? WHERE dialog_id=?",
        (now, dialog_id),
    )
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id,status,read_inbox_max_id,read_outbox_max_id,last_event_at) "
        "VALUES (?,'synced',?,0,?)",
        (dialog_id, read_cursor, now),
    )


def _seed_acceptance_data(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    _seed_dialog(conn, _GROUP_ID, (_GROUP_NAME, _GROUP_USERNAME), read_cursor=1)
    _seed_dialog(conn, _PENDING_GROUP_ID, (_PENDING_NAME, None), read_cursor=None)
    conn.execute(
        "INSERT INTO dialogs(dialog_id,name,type,identity_complete,identity_observed_at,identity_source) "
        "VALUES (?,'Acceptance account','user',1,?,'directory')",
        (_ACCOUNT_ID, now),
    )
    conn.execute(
        "INSERT INTO entities(id,type,name,username,updated_at) VALUES (?,'User','Acceptance account','acceptance_account',?)",
        (_ACCOUNT_ID, now),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,sender_id,out,is_service,is_deleted) "
        "VALUES (?,?,?,'canonical needle from account',?,0,0,0)",
        (_GROUP_ID, 10, now, _ACCOUNT_ID),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,sender_id,out,is_service,is_deleted) "
        "VALUES (?,?,?,'canonical needle authored locally',?,1,0,0)",
        (_GROUP_ID, 11, now, 7),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,sender_id,out,is_service,is_deleted) "
        "VALUES (?,?,?,'pending unread needle',?,0,0,0)",
        (_PENDING_GROUP_ID, 10, now, _ACCOUNT_ID),
    )
    conn.executemany(
        INSERT_FTS_SQL,
        [
            (_GROUP_ID, 10, stem_text("canonical needle from account")),
            (_GROUP_ID, 11, stem_text("canonical needle authored locally")),
            (_PENDING_GROUP_ID, 10, stem_text("pending unread needle")),
        ],
    )
    conn.execute(
        "UPDATE dialog_directory_state SET account_id=7,generation=1,status='complete',"
        "ordinary_status='complete',pinned_main_status='complete',pinned_archive_status='complete',"
        "observation_started_at=?,observation_completed_at=?,observed_count=2 WHERE singleton=1",
        (now, now),
    )
    conn.execute(
        "UPDATE dialog_directory_publication SET account_id=7,generation=1,"
        "observation_started_at=?,observation_completed_at=? WHERE singleton=1",
        (now, now),
    )
    conn.commit()


def _tool_step(name: str, arguments: dict[str, object], *, checks: dict[str, object]) -> dict[str, object]:
    return {
        "action": "call_tool",
        "name": name,
        "arguments": arguments,
        "expect": {
            "is_error": False,
            "path_equals": {"content": [], **checks},
        },
    }


def _acceptance_steps() -> list[dict[str, object]]:
    return [
        _tool_step(
            "list_dialogs",
            {"filter": _GROUP_NAME, "limit": 10},
            checks={
                "structuredContent.dialogs.0.id": _GROUP_ID,
                "structuredContent.dialogs.0.name": _GROUP_NAME,
                "structuredContent.dialogs.0.type": "supergroup",
                "structuredContent.dialogs.0.display_name_source": "name",
            },
        ),
        _tool_step(
            "list_messages",
            {"dialog": _GROUP_NAME, "limit": 10, "navigation": "latest", "message_state": "all"},
            checks={
                "structuredContent.dialog.id": _GROUP_ID,
                "structuredContent.dialog.name": _GROUP_NAME,
                "structuredContent.dialog.type": "supergroup",
                "structuredContent.dialog.display_name_source": "name",
                "structuredContent.source": "sync_db+scheduled_messages+draft_current",
            },
        ),
        _tool_step(
            "search_messages",
            {"dialog": _GROUP_NAME, "query": "needle", "limit": 5},
            checks={
                "structuredContent.dialog_name": _GROUP_NAME,
                "structuredContent.dialog_name_source": "name",
                "structuredContent.scope.dialog": _GROUP_NAME,
                "structuredContent.count": 2,
            },
        ),
        _tool_step(
            "search_messages",
            {"query": "needle", "limit": 5},
            checks={
                "structuredContent.scope.global": True,
                "structuredContent.results.1.dialog_id": _GROUP_ID,
                "structuredContent.results.1.dialog_name": _GROUP_NAME,
                "structuredContent.results.1.dialog_name_source": "name",
            },
        ),
        _tool_step(
            "get_inbox",
            {"last_hours": 24, "include_dialog_types": ["supergroup"]},
            checks={
                "structuredContent.applied_dialog_types": ["supergroup"],
                "structuredContent.dialogs.0.entity.display_name": _GROUP_NAME,
                "structuredContent.dialogs.0.entity.username": f"@{_GROUP_USERNAME}",
                "structuredContent.dialogs.0.dialog_type": "supergroup",
                "structuredContent.dialogs.0.category": "supergroup",
            },
        ),
        _tool_step(
            "get_inbox",
            {"last_hours": 24},
            checks={
                "structuredContent.read_position_pending_count": 1,
                "structuredContent.read_position_pending_entities.0.entity.telegram_id": _PENDING_GROUP_ID,
                "structuredContent.read_position_pending_entities.0.entity.display_name": _PENDING_NAME,
            },
        ),
        _tool_step(
            "get_unread_summary",
            {"limit": 10},
            checks={
                "structuredContent.dialogs.1.entity.display_name": _GROUP_NAME,
                "structuredContent.dialogs.1.dialog_type": "supergroup",
            },
        ),
        _tool_step(
            "get_my_recent_activity",
            {"since_hours": 24, "limit": 10, "dialog_kinds": ["all"]},
            checks={
                "structuredContent.comments.0.dialog_id": _GROUP_ID,
                "structuredContent.comments.0.dialog_name": _GROUP_NAME,
                "structuredContent.comments.0.dialog_type": "supergroup",
            },
        ),
        _tool_step(
            "trace_account_messages",
            {
                "view": "dialogs",
                "exact_account_id": _ACCOUNT_ID,
                "exact_dialog_id": _GROUP_ID,
                "coverage_goal": "observed",
                "limit": 10,
            },
            checks={
                "structuredContent.dialogs.0.dialog_id": _GROUP_ID,
                "structuredContent.dialogs.0.dialog_title": _GROUP_NAME,
                "structuredContent.dialogs.0.dialog_type": "supergroup",
            },
        ),
        {
            "action": "call_tool",
            "name": "list_messages",
            "arguments": {
                "dialog": "@unknown_local_acceptance_username",
                "limit": 5,
                "navigation": "latest",
                "message_state": "all",
            },
            "expect": {
                "is_error": True,
                "content_text_contains": ["dialog_not_found"],
            },
        },
    ]


async def _wait_for_http(url: str, task: asyncio.Task[None]) -> None:
    deadline = asyncio.get_running_loop().time() + 10
    while asyncio.get_running_loop().time() < deadline:
        if task.done():
            await task
        try:
            response = cast(HTTPResponse, await asyncio.to_thread(urlopen, url, timeout=0.5))
            with response:
                if response.status == 200:
                    return
        except OSError, URLError:
            await asyncio.sleep(0.05)
    raise AssertionError("isolated MCP HTTP server did not become ready")


def _unused_loopback_port() -> int:
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        return cast(tuple[str, int], port_socket.getsockname())[1]


async def _run_supported_cli(port: int, script_path: Path) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "devtools.mcp_client.cli",
        "script",
        "--url",
        f"http://127.0.0.1:{port}/mcp",
        "--file",
        str(script_path),
        "--redact",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    assert process.returncode == 0, (
        f"supported devtools CLI failed ({process.returncode}); "
        f"stdout={stdout.decode(errors='replace')} stderr={stderr.decode(errors='replace')}"
    )


async def _run_isolated_mcp_scenario(
    conn: sqlite3.Connection,
    telegram: _FailOnRpcClient,
    script_path: Path,
) -> None:
    feedback_conn = sqlite3.connect(":memory:")
    feedback_conn.execute(
        "CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, submitted_at INTEGER NOT NULL, "
        "message TEXT NOT NULL, severity TEXT, context TEXT, model TEXT, harness TEXT)"
    )
    api = make_server(conn, telegram, feedback_conn)
    api.self_id = 7
    api.self_profile = {"id": 7, "first_name": "Acceptance", "last_name": "Account", "username": None}
    state_dir = load_config().state.dir
    state_dir.mkdir(parents=True, exist_ok=True)
    socket_path = get_daemon_socket_path(state_dir)
    unix_server = await asyncio.start_unix_server(api.handle_client, path=str(socket_path))
    port = _unused_loopback_port()
    stop_event = asyncio.Event()
    http_task = asyncio.create_task(run_mcp_http_server(host="127.0.0.1", port=port, stop_event=stop_event))
    try:
        await _wait_for_http(f"http://127.0.0.1:{port}/health", http_task)
        await _run_supported_cli(port, script_path)
        assert telegram.attempts == []
        assert (
            conn.execute("SELECT 1 FROM entities WHERE id IN (?,?)", (_GROUP_ID, _PENDING_GROUP_ID)).fetchone() is None
        )
    finally:
        stop_event.set()
        try:
            await asyncio.wait_for(http_task, timeout=10)
        except TimeoutError:
            http_task.cancel()
            await asyncio.gather(http_task, return_exceptions=True)
        unix_server.close()
        await unix_server.wait_closed()
        feedback_conn.close()
        socket_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_canonical_only_dialog_identity_over_mcp_http_cli(
    make_synced_db: Callable[[], sqlite3.Connection],
    tmp_path: Path,
) -> None:
    conn = make_synced_db()
    _seed_acceptance_data(conn)
    assert conn.execute("SELECT 1 FROM entities WHERE id IN (?,?)", (_GROUP_ID, _PENDING_GROUP_ID)).fetchone() is None

    telegram = _FailOnRpcClient()
    with pytest.raises(AssertionError):
        telegram.negative_control("negative control")
    assert telegram.attempts == ["negative_control"]
    telegram.attempts.clear()

    script_path = tmp_path / "dialog-identity-acceptance.json"
    script_path.write_text(json.dumps({"steps": _acceptance_steps()}), encoding="utf-8")
    try:
        await _run_isolated_mcp_scenario(conn, telegram, script_path)
    finally:
        conn.close()
