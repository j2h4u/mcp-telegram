"""Exact profile-job diagnostics preserve account and read-only boundaries."""

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from typer.testing import CliRunner

from mcp_telegram import app, daemon_client
from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.daemon_ipc import get_daemon_socket_path
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand_progress import WorkRevision
from tests.test_daemon_api import make_server


def _database(path: Path) -> sqlite3.Connection:
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.commit()
    return conn


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("servererror", "servererror"),
        ("story_validation:ids_missing:missing=2", "story_validation"),
        ("rpcerror:400:USER_NOT_FOUND", "rpc_error"),
        ("private message contents", "unknown"),
        (None, None),
    ],
)
def test_profile_reasons_are_bounded_codes(value: object, expected: str | None) -> None:
    assert EntityProfileRepository._safe_refresh_reason(value) == expected


@pytest.mark.parametrize("status", ["pending", "failed", "complete", "rejected"])
def test_repository_exact_job_and_account_fence(tmp_path: Path, status: str) -> None:
    conn = _database(tmp_path / "sync.db")
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,reason,retry_at) VALUES(7,?,?,?,?)",
        (status, 10, "flood_wait", 100),
    )
    conn.commit()
    repo = EntityProfileRepository(conn, section_ttl_seconds=1)
    before = conn.total_changes
    durable, revision = repo.inspect_refresh_job(7, 42)
    assert durable["status"] == status and durable["reason"] == "flood_wait" and durable["retry_at"] == 100
    assert repo.refresh_work_state(7, 42) == (revision, status)
    assert repo.inspect_refresh_job(8, 42) == ({"availability": "not_enqueued"}, None)
    assert conn.total_changes == before
    conn.execute("UPDATE entity_profile_refresh_state SET reason='private message contents' WHERE entity_id=7")
    assert repo.inspect_refresh_job(7, 42)[0]["reason"] == "unknown"
    with pytest.raises(PermissionError):
        repo.inspect_refresh_job(7, 99)
    assert conn.total_changes == before + 1
    conn.execute("DROP TABLE entity_profile_refresh_state")
    with pytest.raises(sqlite3.Error):
        repo.inspect_refresh_job(7, 42)
    conn.close()


async def test_cli_framed_ipc_reads_current_exact_job_without_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = _database(tmp_path / "sync.db")
    conn.execute(
        "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,reason,retry_at) VALUES(7,'failed',10,'timeouterror',100)"
    )
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(1, 42, 2, 10))
    seen: list[WorkRevision] = []

    def inspect(revision: WorkRevision) -> dict[str, object]:
        seen.append(revision)
        return {
            "coordinator_state": "running",
            "active_for_target": True,
            "active_revision_matches": False,
            "selected_for_kind": False,
            "excluded": True,
            "hold_kind": "excluded",
            "reason": "deadline_exceeded",
            "retry_after_seconds": None,
            "attempts": 2,
            "invocation_remaining_seconds": None,
        }

    server.bind_profile_job_inspector(inspect)
    monkeypatch.setattr(daemon_client, "load_config", lambda: SimpleNamespace(state=SimpleNamespace(dir=tmp_path)))
    socket = get_daemon_socket_path(tmp_path)
    before = conn.total_changes
    async with await asyncio.start_unix_server(server.handle_client, path=socket):
        result = await asyncio.to_thread(CliRunner().invoke, app, ["summary", "--profile-entity-id", "7"])
    assert result.exit_code == 0, result.output
    assert "Saved job: failed" in result.output and "timeouterror" in result.output
    assert "active revision matches saved job: False" in result.output and "deadline_exceeded" in result.output
    assert "process-local" in result.output and "retry after seconds: unknown" in result.output
    assert "acquisition_cursor" not in result.output and "page_cursor_json" not in result.output
    assert len(seen) == 1 and seen[0].key.account_id == 42 and conn.total_changes == before
    conn.close()


async def test_private_view_rejects_invalid_scope_and_reports_missing_coordinator(tmp_path: Path) -> None:
    conn = _database(tmp_path / "sync.db")
    conn.execute("INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at) VALUES(7,'pending',10)")
    conn.commit()
    server = make_server(conn)
    server.self_id = 42
    await server._publish_auth_scope(TelegramAuthScope(1, 42, 2, 10))
    good = await server._get_sync_status({"view": "profile_job", "entity_id": 7})
    data = cast(dict[str, object], good["data"])
    assert cast(dict[str, object], data["live"])["unavailable_reason"] == "coordinator_unavailable"
    assert (await server._get_sync_status({"view": "profile_job", "entity_id": 0}))["error"] == "invalid_entity_id"
    assert (await server._get_sync_status({"view": "profile_job", "entity_id": 7, "dialog_id": 7}))[
        "error"
    ] == "invalid_arguments"
    assert (await server._get_sync_status({"view": "unsupported", "entity_id": 7}))["error"] == "invalid_arguments"
    missing = await server._get_sync_status({"view": "profile_job", "entity_id": 8})
    assert (
        cast(dict[str, object], cast(dict[str, object], missing["data"])["durable"])["availability"] == "not_enqueued"
    )
    conn.execute("UPDATE dialog_directory_state SET account_id=99 WHERE singleton=1")
    assert (await server._get_sync_status({"view": "profile_job", "entity_id": 7}))["error"] == "account_unverified"
    conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
    conn.execute("DROP TABLE entity_profile_refresh_state")
    assert (await server._get_sync_status({"view": "profile_job", "entity_id": 7}))[
        "error"
    ] == "profile_job_unavailable"
    server.self_id = 9
    assert (await server._get_sync_status({"view": "profile_job", "entity_id": 7}))["error"] == "account_unverified"
    conn.close()


def test_cli_rejects_mixed_window_and_missing_daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_client, "load_config", lambda: SimpleNamespace(state=SimpleNamespace(dir=tmp_path)))
    runner = CliRunner()
    mixed = runner.invoke(app, ["summary", "--profile-entity-id", "7", "--since", "2d"])
    assert mixed.exit_code != 0 and "does not apply" in mixed.output
    unavailable = runner.invoke(app, ["summary", "--profile-entity-id", "7"])
    assert unavailable.exit_code != 0 and "Profile job unavailable" in unavailable.output
