# pyright: reportAny=false, reportOperatorIssue=false, reportOptionalSubscript=false, reportIndexIssue=false
"""Focused coverage for durable read-position reconciliation and projection."""

from __future__ import annotations

import asyncio
import sqlite3
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
