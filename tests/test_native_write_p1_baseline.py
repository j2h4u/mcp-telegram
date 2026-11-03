"""Negative control: scheduled receipts do not contain physical-wire proof."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import SupportsBytes, cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.daemon import _bind_scheduled_commands
from mcp_telegram.native_write_fence import FolderWriteRejected, NoteWriteRejected
from mcp_telegram.scheduled_commands import ScheduledCommandOwner
from mcp_telegram.scheduled_messages import ScheduledMessageReconciler, ScheduledReconciliationPolicy
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import ScheduledHistoryClient
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, current_rpc_scope, rpc_scope
from tests.sqlite_writer_helpers import inline_writer
from tests.test_daemon import _typed_ctx
from tests.test_daemon_api import make_server
from tests.test_scheduled_commands import _OwnerHarness
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.parametrize(
    ("packet_request", "error"),
    [
        (functions.messages.UpdateDialogFilterRequest(8, None), FolderWriteRejected),
        (
            functions.contacts.UpdateContactNoteRequest(types.InputUser(12, 7), types.TextWithEntities("note", [])),
            NoteWriteRejected,
        ),
    ],
)
async def test_native_writes_are_denied_before_packet_without_gate_ownership(
    wire: _WireHarness, packet_request: object, error: type[Exception]
) -> None:
    gate, _sender, _connection, sink = wire
    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, time.monotonic() + 10, asyncio.current_task()
    )
    with pytest.raises(error):
        gate.register_application(packet_request, bytes(cast(SupportsBytes, packet_request)), scope)
    assert sink.frames == []


async def test_scheduled_owner_recovery_does_not_release_unknown_wire_hold(wire: _WireHarness, tmp_path: Path) -> None:
    gate, sender, connection, sink = wire
    scheduled = _OwnerHarness(tmp_path / "scheduled.db", schedule_at=int(time.time()) + 300)
    with write_transaction(scheduled.conn):
        scheduled.conn.execute("UPDATE scheduled_sync_state SET account_id=42 WHERE key='account'")
        scheduled.conn.execute("UPDATE scheduled_reconciliation_state SET dialog_id=42 WHERE dialog_id=200")
    observations: list[tuple[object, object]] = []
    reconciler = ScheduledMessageReconciler(
        cast(ScheduledHistoryClient, scheduled.client),
        scheduled.conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=30),
        writer=inline_writer(scheduled.conn),
        mutation_fence=scheduled.fence,
    )
    reconciler.bind_account_id(42)

    def offer(kind: object) -> None:
        observations.append((kind, gate.account_safety_status()))

    class Client:
        async def __call__(self, request: object) -> object:
            with rpc_scope(TelegramRpcSource.SCHEDULED_MUTATION):
                response = sender.send_owned(request, current_rpc_scope())
                state = await sender._send_queue.next_state()
                await sender._prepare_state(state)
                packet = connection._send_queue.get_nowait()
                assert sender.write_packet(packet, connection._send)
                state.future.set_exception(ConnectionError("lost acknowledgment"))
                return await response

    client = Client()
    server = make_server(scheduled.conn, client, feedback_conn=scheduled.conn)
    server.self_id = 42
    ctx = _typed_ctx(
        conn=scheduled.conn,
        writer=inline_writer(scheduled.conn),
        api_server=server,
        client=client,
        demand_runtime=SimpleNamespace(scheduled_reconciler=reconciler, coordinator=SimpleNamespace(offer=offer)),
        scheduled_mutation_fence=scheduled.fence,
    )
    try:
        await _bind_scheduled_commands(ctx)
        owner = cast(ScheduledCommandOwner, server._scheduled_commands)
        result = await owner.execute(
            {
                "action": "create",
                "dialog_id": 42,
                "operation_key": "operation-one",
                "text": "hello",
                "schedule_at_utc": datetime.fromtimestamp(scheduled.schedule_at, UTC).isoformat(),
            }
        )
        assert result["outcome"] == "unknown"
        assert len(sink.frames) == 1
        assert len(observations) == 1 and observations[0][0] is DemandKind.SCHEDULED_DISCOVERY
        assert observations[0][1] is not None
        row = cast(
            sqlite3.Row,
            scheduled.conn.execute(
                "SELECT coverage_state, coverage_reason FROM scheduled_reconciliation_state WHERE dialog_id=42"
            ).fetchone(),
        )
        assert tuple(row) == ("unknown", "scheduled_mutation_unknown")
        status = gate.account_safety_status()
        assert status is not None and status["unresolved_request_count"] == 1
    finally:
        await server.shutdown()
        scheduled.close()
