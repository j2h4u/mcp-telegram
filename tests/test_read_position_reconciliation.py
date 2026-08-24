# pyright: reportAny=false, reportOperatorIssue=false, reportOptionalSubscript=false, reportIndexIssue=false
"""Focused coverage for durable read-position reconciliation and projection."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from tests.history_enrollment_helpers import seed_full_history_enrollment


def _connection() -> sqlite3.Connection:
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


def _seed_pending(conn: sqlite3.Connection, dialog_id: int, *, status: str = "synced") -> None:
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id, status, read_inbox_max_id) VALUES (?, ?, NULL)",
        (dialog_id, status),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=status == "synced")
    conn.commit()


@pytest.mark.asyncio
async def test_reconciliation_loop_picks_up_late_enrollment() -> None:
    from mcp_telegram.daemon import _run_read_position_reconciliation_loop

    conn = _connection()
    try:
        shutdown = asyncio.Event()
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace()
        client.return_value = SimpleNamespace(
            dialogs=[SimpleNamespace(peer=SimpleNamespace(), read_inbox_max_id=42, read_outbox_max_id=7)]
        )

        async def no_batch_pause(_event: asyncio.Event) -> bool:
            return True

        with (
            patch("mcp_telegram.daemon._sleep_read_pos_batch", new=no_batch_pause),
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", return_value=1001),
        ):
            task = asyncio.create_task(
                _run_read_position_reconciliation_loop(
                    client,
                    conn,
                    shutdown,
                    interval_seconds=0.01,
                    max_dialogs_per_pass=10,
                )
            )
            await asyncio.sleep(0)
            _seed_pending(conn, 1001)
            await asyncio.sleep(0.03)
            shutdown.set()
            await asyncio.wait_for(task, timeout=1)

        row = conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = 1001").fetchone()
        assert row == (42,)
        assert client.await_count >= 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconciliation_pass_caps_selected_dialogs() -> None:
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _connection()
    try:
        for dialog_id in range(1001, 1006):
            _seed_pending(conn, dialog_id)
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace()
        client.return_value = SimpleNamespace(dialogs=[])
        with (
            patch("mcp_telegram.daemon._sleep_read_pos_batch", new=AsyncMock(return_value=True)),
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: 1001),
        ):
            await _initialize_read_positions(client, conn, asyncio.Event(), max_dialogs=2)
        assert client.get_input_entity.await_count == 2
        assert client.await_count == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconciliation_stops_cleanly_on_shutdown() -> None:
    from mcp_telegram.daemon import _run_read_position_reconciliation_loop

    conn = _connection()
    try:
        shutdown = asyncio.Event()
        shutdown.set()
        client = AsyncMock()
        await _run_read_position_reconciliation_loop(
            client, conn, shutdown, interval_seconds=60, max_dialogs_per_pass=2
        )
        client.assert_not_awaited()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconciliation_treats_open_rpc_circuit_as_retryable() -> None:
    from mcp_telegram.daemon import _initialize_read_positions
    from mcp_telegram.telegram_rpc import TelegramRpcCircuitOpenError

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        client = AsyncMock()
        client.get_input_entity.side_effect = TelegramRpcCircuitOpenError("open-for-test")
        assert await _initialize_read_positions(client, conn, asyncio.Event(), max_dialogs=1) == 0
        assert conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = 1001").fetchone() == (None,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_failed_low_id_does_not_starve_later_due_row() -> None:
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        _seed_pending(conn, 1002)
        client = AsyncMock()

        async def resolve(dialog_id: int) -> object:
            if dialog_id == 1001:
                raise ValueError("unresolved")
            return SimpleNamespace(_did=dialog_id)

        client.get_input_entity.side_effect = resolve
        client.side_effect = lambda request: SimpleNamespace(
            dialogs=[SimpleNamespace(peer=SimpleNamespace(_did=1002), read_inbox_max_id=8, read_outbox_max_id=9)]
        )
        with patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did):
            await _initialize_read_positions(
                client, conn, asyncio.Event(), max_dialogs=1, failure_cooldown_seconds=3600
            )
            assert (
                await _initialize_read_positions(
                    client, conn, asyncio.Event(), max_dialogs=1, failure_cooldown_seconds=3600
                )
                == 1
            )

        assert client.get_input_entity.await_args_list[0].args == (1001,)
        assert client.get_input_entity.await_args_list[1].args == (1002,)
        assert conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = 1002").fetchone() == (8,)
        retry_at = conn.execute(
            "SELECT read_position_next_attempt_at FROM synced_dialogs WHERE dialog_id = 1001"
        ).fetchone()[0]
        assert retry_at > int(time.time())

        conn.execute("UPDATE synced_dialogs SET read_position_next_attempt_at = 0 WHERE dialog_id = 1001")
        conn.commit()
        await _initialize_read_positions(client, conn, asyncio.Event(), max_dialogs=1, failure_cooldown_seconds=1)
        assert client.get_input_entity.await_args_list[-1].args == (1001,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_telegram_none_cursor_is_cooled_down() -> None:
    from mcp_telegram.daemon import _initialize_read_positions

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace(_did=1001)
        client.return_value = SimpleNamespace(
            dialogs=[SimpleNamespace(peer=SimpleNamespace(_did=1001), read_inbox_max_id=None, read_outbox_max_id=4)]
        )
        with patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did):
            await _initialize_read_positions(
                client, conn, asyncio.Event(), max_dialogs=1, failure_cooldown_seconds=3600
            )
            calls_after_none = client.get_input_entity.await_count
            await _initialize_read_positions(
                client, conn, asyncio.Event(), max_dialogs=1, failure_cooldown_seconds=3600
            )
        assert client.get_input_entity.await_count == calls_after_none
        assert conn.execute(
            "SELECT read_inbox_max_id, read_outbox_max_id FROM synced_dialogs WHERE dialog_id = 1001"
        ).fetchone() == (None, 4)
        assert conn.execute(
            "SELECT read_position_next_attempt_at FROM synced_dialogs WHERE dialog_id = 1001"
        ).fetchone()[0] > int(time.time())
    finally:
        conn.close()


def test_inbox_projection_uses_read_position_identity_contract() -> None:
    from mcp_telegram.tools.unread import _project_inbox_response

    response = {
        "ok": True,
        "data": {
            "groups": [],
            "read_position_pending_count": 3,
            "read_position_pending_entities": [
                {"dialog_id": 10, "display_name": "Alice", "username": "alice"},
                {"dialog_id": 11, "display_name": "Alias", "username": "@alice"},
                {"dialog_id": 12, "display_name": None, "username": None},
                {"dialog_id": None, "display_name": "invalid", "username": None},
            ],
        },
    }
    from mcp_telegram.tools.unread import GetInbox

    result = _project_inbox_response(GetInbox(), response, applied_since_utc=None, has_inbox_filter=False)
    payload = result.structured_content
    assert "bootstrap_pending" not in payload
    assert payload["read_position_pending_count"] == 3
    assert payload["coverage"]["read_position_pending_count"] == 3
    identities = payload["read_position_pending_entities"]
    assert identities == [
        {"display_name": "Alice", "username": "@alice"},
        {"display_name": "12", "telegram_id": 12},
    ]


def test_inbox_projection_rejects_missing_read_position_contract() -> None:
    from mcp_telegram.tools.unread import GetInbox, _project_inbox_response

    with pytest.raises(KeyError, match="read_position_pending_count"):
        _project_inbox_response(
            GetInbox(),
            {"ok": True, "data": {"groups": []}},
            applied_since_utc=None,
            has_inbox_filter=False,
        )
