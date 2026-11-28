from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import sys
import time
from collections.abc import Callable
from contextlib import AsyncExitStack, closing
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast
from urllib.error import URLError
from urllib.request import urlopen

import pytest

from mcp_telegram.config import load_config
from mcp_telegram.daemon_ipc import get_daemon_socket_path
from mcp_telegram.demand_wiring import DemandOfferSink, offer_durable_demand
from mcp_telegram.dialog_identity import capture_identity_baseline, publish_dialog_identity
from mcp_telegram.dialog_identity_contracts import DialogIdentityObservation
from mcp_telegram.fts import INSERT_FTS_SQL, stem_text
from mcp_telegram.server import run_mcp_http_server
from mcp_telegram.telegram_rpc_consumers import DemandKind
from test_daemon_api import make_server

_GROUP_ID = -10090001
_PENDING_GROUP_ID = -10090002
_EMPTY_OWN_ONLY_GROUP_ID = -10090004
_ACCOUNT_ID = 90003
_GROUP_NAME = "Canonical acceptance group"
_GROUP_USERNAME = "canonical_acceptance_group"
_PENDING_NAME = "Pending cursor group"
_EMPTY_OWN_ONLY_GROUP_NAME = "Canonical empty own-only group"


class _FailOnRpcClient:
    def __init__(self) -> None:
        self.attempts: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., NoReturn]:
        def reject(*_args: object, **_kwargs: object) -> NoReturn:
            self.attempts.append(name)
            raise AssertionError(f"unexpected Telegram RPC attempt: {name}")

        return reject


class _RecordingDemandOfferSink(DemandOfferSink):
    def __init__(self) -> None:
        self.offers: list[DemandKind] = []

    def offer(self, kind: DemandKind) -> bool:
        self.offers.append(kind)
        return True


def _seed_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    identity: tuple[str, str | None],
    *,
    read_cursor: int | None,
    own_only: bool = False,
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
    if own_only:
        conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (?,'own_only')", (dialog_id,))
        conn.execute(
            "INSERT INTO own_only_dialogs(dialog_id,inclusion_basis,updated_at) VALUES (?,?,?)",
            (dialog_id, '["owned_channel"]', now),
        )
    else:
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
    _seed_dialog(
        conn,
        _EMPTY_OWN_ONLY_GROUP_ID,
        (_EMPTY_OWN_ONLY_GROUP_NAME, None),
        read_cursor=None,
        own_only=True,
    )
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
        "observation_started_at=?,observation_completed_at=?,observed_count=3 WHERE singleton=1",
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
            "list_messages",
            {
                "dialog": _EMPTY_OWN_ONLY_GROUP_NAME,
                "limit": 2,
                "navigation": "latest",
                "message_state": "all",
            },
            checks={
                "structuredContent.dialog_id": _EMPTY_OWN_ONLY_GROUP_ID,
                "structuredContent.dialog.id": _EMPTY_OWN_ONLY_GROUP_ID,
                "structuredContent.dialog.name": _EMPTY_OWN_ONLY_GROUP_NAME,
                "structuredContent.dialog.type": "supergroup",
                "structuredContent.dialog.display_name_source": "name",
                "structuredContent.count": 0,
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
    drain = asyncio.create_task(process.communicate())
    try:
        stdout, stderr = await asyncio.wait_for(asyncio.shield(drain), timeout=60)
    except BaseException as error:
        try:
            if process.returncode is None:
                process.kill()
        except ProcessLookupError:
            pass
        except BaseException as cleanup_error:  # noqa: BLE001 - preserve the original failure.
            error.add_note(f"CLI termination failed: {cleanup_error!r}")
        for task in (
            cast(asyncio.Task[object], drain),
            cast(asyncio.Task[object], asyncio.create_task(process.wait())),
        ):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except BaseException:  # noqa: BLE001 - retrieve the failure below.
                    break
            try:
                task.result()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve the original failure.
                error.add_note(f"CLI cleanup failed: {cleanup_error!r}")
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
    async with AsyncExitStack() as stack:
        feedback_conn = stack.enter_context(closing(sqlite3.connect(":memory:")))
        await _run_isolated_mcp_with_feedback(conn, telegram, script_path, feedback_conn, stack)


async def _run_isolated_mcp_with_feedback(  # noqa: PLR0915
    conn: sqlite3.Connection,
    telegram: _FailOnRpcClient,
    script_path: Path,
    feedback_conn: sqlite3.Connection,
    stack: AsyncExitStack,
) -> None:
    feedback_conn.execute(
        "CREATE TABLE feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, submitted_at INTEGER NOT NULL, "
        "message TEXT NOT NULL, severity TEXT, context TEXT, model TEXT, harness TEXT)"
    )
    api = make_server(conn, telegram, feedback_conn)
    api.self_id = 7
    api.self_profile = {"id": 7, "first_name": "Acceptance", "last_name": "Account", "username": None}
    demand_sink = _RecordingDemandOfferSink()
    api.bind_demand_sink(demand_sink)
    offer_durable_demand(demand_sink, DemandKind.ENTITY_PROFILE_REFRESH)
    assert demand_sink.offers == [DemandKind.ENTITY_PROFILE_REFRESH]
    demand_sink.offers.clear()
    state_dir = load_config().state.dir
    state_dir.mkdir(parents=True, exist_ok=True)
    socket_path = get_daemon_socket_path(state_dir)
    unix_server = await asyncio.start_unix_server(api.handle_client, path=str(socket_path))
    stack.callback(socket_path.unlink, missing_ok=True)

    async def close_unix_server() -> None:
        unix_server.close()
        await unix_server.wait_closed()

    stack.push_async_callback(close_unix_server)
    port = _unused_loopback_port()
    stop_event = asyncio.Event()
    http_task = asyncio.create_task(run_mcp_http_server(host="127.0.0.1", port=port, stop_event=stop_event))
    body_error: BaseException | None = None

    async def stop_http() -> None:
        stop_event.set()
        try:
            await asyncio.wait_for(asyncio.shield(http_task), timeout=10)
        except TimeoutError:
            http_task.cancel()
        except BaseException as error:
            if body_error is None:
                raise
            if body_error is not error:
                body_error.add_note(f"HTTP shutdown failed: {error!r}")
        finally:
            await asyncio.gather(http_task, return_exceptions=True)

    stack.push_async_callback(stop_http)
    try:
        await _wait_for_http(f"http://127.0.0.1:{port}/health", http_task)
        await _run_supported_cli(port, script_path)
        assert telegram.attempts == []
        assert demand_sink.offers == []
        assert (
            conn.execute(
                "SELECT 1 FROM entities WHERE id IN (?,?,?)",
                (_GROUP_ID, _PENDING_GROUP_ID, _EMPTY_OWN_ONLY_GROUP_ID),
            ).fetchone()
            is None
        )
    except BaseException as error:
        body_error = error
        raise


@pytest.mark.asyncio
async def test_canonical_only_dialog_identity_over_mcp_http_cli(
    make_synced_db: Callable[[], sqlite3.Connection],
    tmp_path: Path,
) -> None:
    conn = make_synced_db()
    _seed_acceptance_data(conn)
    assert (
        conn.execute(
            "SELECT 1 FROM entities WHERE id IN (?,?,?)",
            (_GROUP_ID, _PENDING_GROUP_ID, _EMPTY_OWN_ONLY_GROUP_ID),
        ).fetchone()
        is None
    )

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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unix_start", "http_task"])
async def test_isolated_scenario_closes_resources_on_start_failure(
    make_synced_db: Callable[[], sqlite3.Connection], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    conn = make_synced_db()
    feedback: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        result = original_connect(*args, **kwargs)  # type: ignore[arg-type]
        if args and args[0] == ":memory:":
            feedback.append(result)
        return result

    async def fail_http(**_kwargs: object) -> None:
        raise OSError("HTTP failed")

    async def fail_unix(*_args: object, **_kwargs: object) -> asyncio.AbstractServer:
        raise OSError("Unix start failed")

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(
        sys.modules[__name__], "load_config", lambda: SimpleNamespace(state=SimpleNamespace(dir=tmp_path))
    )
    monkeypatch.setattr(sys.modules[__name__], "run_mcp_http_server", fail_http)
    if failure == "unix_start":
        monkeypatch.setattr(asyncio, "start_unix_server", fail_unix)
    try:
        with pytest.raises(OSError, match="failed"):
            await _run_isolated_mcp_scenario(conn, _FailOnRpcClient(), tmp_path / "unused")
        assert not get_daemon_socket_path(tmp_path).exists()
        assert len(feedback) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            feedback[0].execute("SELECT 1")
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_cli_cancellation_kills_and_reaps_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Child:
        returncode: int | None = None
        started = asyncio.Event()
        stopped = asyncio.Event()
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            self.started.set()
            await self.stopped.wait()
            self.waited = True
            return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.stopped.set()

        async def wait(self) -> int:
            self.waited = True
            return -9

    child = Child()

    async def spawn(*_args: object, **_kwargs: object) -> Child:
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(_run_supported_cli(1, tmp_path / "unused"))
    await child.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.killed and child.waited


@pytest.mark.asyncio
async def test_cli_repeated_cancellation_drains_real_child(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    original_spawn = asyncio.create_subprocess_exec
    children: list[asyncio.subprocess.Process] = []
    marker = tmp_path / "started"

    async def spawn(*_args: object, **_kwargs: object) -> asyncio.subprocess.Process:
        code = (
            "import os,time; from pathlib import Path; "
            f"os.write(1, b'x' * 1000000); Path({str(marker)!r}).touch(); time.sleep(60)"
        )
        child = await original_spawn(
            sys.executable, "-c", code, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        children.append(child)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    task = asyncio.create_task(_run_supported_cli(1, tmp_path / "unused"))
    async with asyncio.timeout(10):
        while not marker.exists():
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert children[0].returncode is not None


@pytest.mark.asyncio
async def test_cli_reaps_child_when_communicate_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class Child:
        returncode: int | None = None
        killed = False
        waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            raise OSError("read failed")

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> int:
            self.waited = True
            return -9

    child = Child()

    async def spawn(*_args: object, **_kwargs: object) -> Child:
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(OSError, match="read failed"):
        await _run_supported_cli(1, tmp_path / "unused")
    assert child.killed and child.waited


@pytest.mark.asyncio
@pytest.mark.parametrize("body_fails", [False, True])
async def test_http_shutdown_error_preserves_body_error(
    make_synced_db: Callable[[], sqlite3.Connection], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body_fails: bool
) -> None:
    conn = make_synced_db()

    async def http(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
        await stop_event.wait()
        raise OSError("shutdown failed")

    async def ready(*_args: object) -> None:
        return None

    async def cli(*_args: object) -> None:
        if body_fails:
            raise ValueError("scenario failed")

    monkeypatch.setattr(
        sys.modules[__name__], "load_config", lambda: SimpleNamespace(state=SimpleNamespace(dir=tmp_path))
    )
    monkeypatch.setattr(sys.modules[__name__], "run_mcp_http_server", http)
    monkeypatch.setattr(sys.modules[__name__], "_wait_for_http", ready)
    monkeypatch.setattr(sys.modules[__name__], "_run_supported_cli", cli)
    try:
        if body_fails:
            with pytest.raises(ValueError, match="scenario failed") as result:
                await _run_isolated_mcp_scenario(conn, _FailOnRpcClient(), tmp_path / "unused")
            assert any("shutdown failed" in note for note in result.value.__notes__)
        else:
            with pytest.raises(OSError, match="shutdown failed"):
                await _run_isolated_mcp_scenario(conn, _FailOnRpcClient(), tmp_path / "unused")
        assert not get_daemon_socket_path(tmp_path).exists()
    finally:
        conn.close()
