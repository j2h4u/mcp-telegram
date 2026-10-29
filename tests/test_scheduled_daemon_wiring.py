# pyright: reportAny=false

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon.tl import types
from telethon.tl.functions.messages import SendMessageRequest

from mcp_telegram import sync_db
from mcp_telegram.daemon import _bind_scheduled_commands
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.event_handlers import _EventHandlerClient
from mcp_telegram.scheduled_commands import ScheduledCommandOwner, _parse_command, prepare_scheduled_operation
from mcp_telegram.scheduled_messages import (
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    mark_missing_from_snapshot,
    mark_scheduled_messages_removed,
    upsert_scheduled_message,
    verify_scheduled_publication,
)
from mcp_telegram.scheduled_write_fence import ScheduledMutationFence
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_gateway import ScheduledHistoryClient
from tests.sqlite_writer_helpers import inline_writer
from tests.test_daemon import _typed_ctx
from tests.test_daemon_api import make_server
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.fixture
def conn(tmp_path: Path):
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    try:
        yield connection
    finally:
        connection.close()


def _message(message_id: int = 7) -> types.Message:
    return types.Message(
        id=message_id,
        peer_id=types.PeerUser(200),
        date=datetime.now(UTC),
        message="retained",
        out=True,
    )


def _seed(conn: sqlite3.Connection) -> None:
    with write_transaction(conn):
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES(200,'user')")
        conn.execute("INSERT INTO daemon_state(key,value) VALUES('canonical_observation_sequence','1')")
        conn.execute("UPDATE scheduled_sync_state SET account_id=100 WHERE key='account'")
        conn.execute(
            "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,snapshot_observation_order,"
            "baseline_observed_at,coverage_state) VALUES(200,1,1,1,'complete')"
        )


async def test_native_command_binding_routes_and_persists_canonical_response(conn: sqlite3.Connection) -> None:
    _seed(conn)
    calls: list[object] = []

    class Client:
        def retained_draft_peer(self, dialog_id: int) -> object:
            assert dialog_id == 200
            return types.InputPeerUser(200, 99)

        async def __call__(self, request: object) -> object:
            calls.append(request)
            assert isinstance(request, SendMessageRequest)
            message = _message(77)
            message.date = request.schedule_date
            return types.Updates(
                [types.UpdateMessageID(77, request.random_id), types.UpdateNewScheduledMessage(message)],
                [],
                [],
                datetime.now(UTC),
                1,
            )

    client = Client()
    server = make_server(conn, client, feedback_conn=conn)
    server.self_id = 100
    server.self_profile = {"premium": False}
    fence = ScheduledMutationFence()
    runtime = SimpleNamespace(
        scheduled_reconciler=SimpleNamespace(invalidate_continuity=AsyncMock()),
        coordinator=SimpleNamespace(offer=lambda *_: None),
    )
    ctx = _typed_ctx(
        conn=conn,
        writer=inline_writer(conn),
        client=client,
        api_server=server,
        demand_runtime=runtime,
        scheduled_mutation_fence=fence,
    )
    await _bind_scheduled_commands(ctx)
    assert server._scheduled_commands is not None
    assert cast(ScheduledCommandOwner, server._scheduled_commands)._fence is fence
    result = await server._dispatch(
        {
            "method": "manage_scheduled_message",
            "request_id": "abcdef12",
            "operation_id": "op",
            "dialog_id": 200,
            "action": "create",
            "operation_key": "create-one",
            "text": "future",
            "schedule_at_utc": datetime.fromtimestamp(int(time.time()) + 3600, UTC).isoformat(),
        }
    )
    assert result["ok"] is True
    assert result["outcome"] == "acknowledged"
    assert len(calls) == 1
    row = conn.execute(
        "SELECT text,has_rich_message,invert_media,video_processing_pending FROM scheduled_messages "
        "WHERE dialog_id=200 AND message_id=77"
    ).fetchone()
    assert tuple(row) == ("retained", 0, 0, 0)
    assert conn.execute("SELECT outcome FROM scheduled_operation_receipts").fetchone()[0] == "acknowledged"
    await server.shutdown()


@pytest.mark.parametrize(
    "kind,topic,expected",
    [
        ("forum", None, "topic_required"),
        ("channel", 1, "topic_mismatch"),
        ("forum", 1, "topic_unavailable"),
        ("unknown", None, "dialog_unavailable"),
        ("bot", None, "topic_capability_unknown"),
    ],
)
async def test_binding_rejects_unsafe_scope_without_telegram(
    conn: sqlite3.Connection,
    kind: str,
    topic: int | None,
    expected: str,
) -> None:
    _seed(conn)
    with write_transaction(conn):
        conn.execute("UPDATE dialogs SET type=? WHERE dialog_id=200", (kind,))
    client = SimpleNamespace(retained_draft_peer=lambda _: types.InputPeerChannel(200, 1))
    server = make_server(conn, client, feedback_conn=conn)
    server.self_id = 100
    ctx = _typed_ctx(
        conn=conn,
        writer=inline_writer(conn),
        client=client,
        api_server=server,
        demand_runtime=SimpleNamespace(),
        scheduled_mutation_fence=ScheduledMutationFence(),
    )
    await _bind_scheduled_commands(ctx)
    result = await server._dispatch(
        {
            "method": "manage_scheduled_message",
            "dialog_id": 200,
            "action": "create",
            "operation_key": "scope",
            "text": "future",
            "topic_id": topic,
            "schedule_at_utc": datetime.fromtimestamp(int(time.time()) + 3600, UTC).isoformat(),
        }
    )
    assert result["error"] == expected
    await server.shutdown()


async def test_mutating_ipc_outlives_disconnect() -> None:
    release = asyncio.Event()

    async def request() -> tuple[dict, str, str | None]:
        await release.wait()
        return {}, "manage_scheduled_message", None

    async def eof() -> bytes:
        return b""

    pending = asyncio.create_task(eof())
    task = asyncio.create_task(request())
    try:
        abandoned = await DaemonAPIServer._cancel_abandoned_foreground_request(
            json.dumps({"method": "manage_scheduled_message"}).encode(),
            pending,
            task,
        )
        assert abandoned is False
        assert not task.cancelled() and not task.done()
    finally:
        release.set()
        await task


def test_accepted_canonical_writers_invalidate_only_their_target(conn: sqlite3.Connection) -> None:
    _seed(conn)
    fence = ScheduledMutationFence()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 200, _message(), observation_order=5)
    selected = fence.watch(100, 200, 7)
    other = fence.watch(100, 200, 8)
    create = fence.watch(100, 200, None)
    with write_transaction(conn):
        upsert_scheduled_message(conn, 200, _message(), observation_order=4, mutation_fence=fence, account_id=100)
    assert not selected.invalidated.is_set()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 200, _message(), observation_order=6, mutation_fence=fence, account_id=100)
    assert selected.invalidated.is_set()
    assert not other.invalidated.is_set() and not create.invalidated.is_set()
    selected = fence.watch(100, 200, 7)
    with write_transaction(conn):
        mark_missing_from_snapshot(conn, 200, [7], observation_order=7, mutation_fence=fence, account_id=100)
    assert selected.invalidated.is_set()
    selected = fence.watch(100, 200, 7)
    with write_transaction(conn):
        mark_scheduled_messages_removed(conn, 200, [7], [91], observation_order=8)
        verify_scheduled_publication(conn, 200, 91, mutation_fence=fence, account_id=100)
    assert selected.invalidated.is_set()


async def test_authoritative_snapshot_and_continuity_invalidate_create(conn: sqlite3.Connection) -> None:
    _seed(conn)
    fence = ScheduledMutationFence()
    reconciler = ScheduledMessageReconciler(
        cast(ScheduledHistoryClient, SimpleNamespace()),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(10),
        writer=inline_writer(conn),
        mutation_fence=fence,
    )
    reconciler.bind_account_id(100)
    create = fence.watch(100, 200, None)
    assert await reconciler._apply_snapshot(200, 0, [], discovery=True, now=2, observation_order=1) is None
    assert not create.invalidated.is_set()
    assert await reconciler._apply_snapshot(200, 0, [], discovery=True, now=2, observation_order=2) is not None
    assert create.invalidated.is_set()
    create = fence.watch(100, 999, None)
    other_account = fence.watch(101, 999, None)
    await reconciler.invalidate_continuity(None)
    assert create.invalidated.is_set() and not other_account.invalidated.is_set()


def test_schema92_preserves_legacy_rows_and_isolates_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "migration.db"
    with monkeypatch.context() as patch:
        patch.setattr(sync_db, "_CURRENT_SCHEMA_VERSION", 91)
        ensure_sync_schema(path)
    conn = _open_sync_db(path)
    with write_transaction(conn):
        conn.execute(
            "INSERT INTO scheduled_messages(dialog_id,message_id,text,first_seen_at,updated_at) "
            "VALUES(200,7,'legacy',1,1)"
        )
    conn.close()
    ensure_sync_schema(path)
    conn = _open_sync_db(path)
    try:
        assert conn.execute(
            "SELECT text,has_rich_message,invert_media,video_processing_pending FROM scheduled_messages"
        ).fetchone() == ("legacy", None, None, None)
        with write_transaction(conn):
            for account in (100, 101):
                command = _parse_command(
                    {
                        "action": "create",
                        "dialog_id": 200,
                        "operation_key": "same",
                        "text": "future",
                        "schedule_at_utc": "2030-01-01T00:00:00Z",
                    },
                    account,
                )
                prepare_scheduled_operation(conn, command, account, now=1)
        assert conn.execute("SELECT COUNT(*) FROM scheduled_operation_receipts").fetchone() == (2,)
        assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone() == (92,)
    finally:
        conn.close()


@pytest.mark.parametrize("interrupt_recovery", [False, True])
async def test_startup_interruption_recovers_before_settlement_and_keeps_account_scope(
    conn: sqlite3.Connection,
    interrupt_recovery: bool,
) -> None:
    _seed(conn)
    with write_transaction(conn):
        for account in (100, 101):
            command = _parse_command(
                {
                    "action": "create",
                    "dialog_id": 200,
                    "operation_key": "interrupted",
                    "text": "future",
                    "schedule_at_utc": "2030-01-01T00:00:00Z",
                },
                account,
            )
            prepare_scheduled_operation(conn, command, account)
    fence = ScheduledMutationFence()
    reconciler = ScheduledMessageReconciler(
        cast(ScheduledHistoryClient, SimpleNamespace()),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(10),
        writer=inline_writer(conn),
        mutation_fence=fence,
    )
    reconciler.bind_account_id(100)
    calls: list[object] = []

    async def recover(dialog_id: int, reason: str) -> None:
        assert (
            conn.execute("SELECT outcome FROM scheduled_operation_receipts WHERE account_id=100").fetchone()[0]
            == "pending"
        )
        await reconciler.invalidate_continuity(dialog_id, reason)
        calls.append(dialog_id)
        if interrupt_recovery:
            raise RuntimeError("startup interrupted after durable recovery")

    runtime = SimpleNamespace(
        scheduled_reconciler=SimpleNamespace(invalidate_continuity=recover),
        coordinator=SimpleNamespace(offer=lambda kind: calls.append(kind)),
    )
    server = make_server(conn, SimpleNamespace(), feedback_conn=conn)
    server.self_id = 100
    ctx = _typed_ctx(
        conn=conn, writer=inline_writer(conn), api_server=server, demand_runtime=runtime, scheduled_mutation_fence=fence
    )
    if interrupt_recovery:
        with pytest.raises(RuntimeError, match="startup interrupted"):
            await _bind_scheduled_commands(ctx)
        assert (
            conn.execute("SELECT outcome FROM scheduled_operation_receipts WHERE account_id=100").fetchone()[0]
            == "pending"
        )
        assert (
            conn.execute("SELECT coverage_state FROM scheduled_reconciliation_state WHERE dialog_id=200").fetchone()[0]
            == "unknown"
        )
        assert server._scheduled_commands is None
        await server.shutdown()
        return
    await _bind_scheduled_commands(ctx)
    assert calls[0] == 200 and len(calls) == 2
    assert [
        tuple(row)
        for row in conn.execute("SELECT account_id,outcome FROM scheduled_operation_receipts ORDER BY account_id")
    ] == [(100, "unknown"), (101, "pending")]
    coverage = conn.execute(
        "SELECT coverage_state,discovery_due_at FROM scheduled_reconciliation_state WHERE dialog_id=200"
    ).fetchone()
    assert coverage[0] == "unknown" and coverage[1] is not None
    assert server._scheduled_commands is not None
    premium = cast(ScheduledCommandOwner, server._scheduled_commands)._account_is_premium
    assert premium() is None
    server.self_profile = {"premium": True}
    assert premium() is True
    server.self_profile = {"premium": False}
    assert premium() is False
    await server.shutdown()


async def test_raw_canonical_event_uses_shared_fence_and_filters_stale_order(conn: sqlite3.Connection) -> None:
    from mcp_telegram.delivered_update import DeliveredUpdate, delivered_update_context
    from mcp_telegram.event_handlers import EventHandlerManager

    _seed(conn)
    fence = ScheduledMutationFence()
    manager = EventHandlerManager(
        cast(_EventHandlerClient, SimpleNamespace()),
        conn,
        asyncio.Event(),
        writer=inline_writer(conn),
        profile_detail_ttl_seconds=300,
        scheduled_mutation_fence=fence,
    )
    manager.set_self_id(100)
    with write_transaction(conn):
        upsert_scheduled_message(conn, 200, _message(), observation_order=5)
    selected = fence.watch(100, 200, 7)
    create = fence.watch(100, 200, None)
    for order in (4, 6):
        with delivered_update_context(DeliveredUpdate(order, time.time(), 0)):
            await manager.on_raw_new_scheduled_message(types.UpdateNewScheduledMessage(_message()))
        assert selected.invalidated.is_set() is (order == 6)
    assert not create.invalidated.is_set()


async def test_saved_messages_target_uses_verified_self_without_entity_cache(conn: sqlite3.Connection) -> None:
    _seed(conn)
    server = make_server(conn, SimpleNamespace(), feedback_conn=conn)
    server.self_id = 100
    ctx = _typed_ctx(
        conn=conn,
        writer=inline_writer(conn),
        api_server=server,
        demand_runtime=SimpleNamespace(),
        scheduled_mutation_fence=ScheduledMutationFence(),
    )
    await _bind_scheduled_commands(ctx)
    owner = cast(ScheduledCommandOwner, server._scheduled_commands)
    assert isinstance(await owner._resolve_peer(100, None), types.InputPeerSelf)
    await server.shutdown()


async def test_actual_canonical_event_between_owner_prepare_and_wire_claim(  # noqa: PLR0915 - one command-to-frame race scenario
    wire: _WireHarness,
    tmp_path: Path,
) -> None:
    from mcp_telegram.delivered_update import DeliveredUpdate, delivered_update_context
    from mcp_telegram.event_handlers import EventHandlerManager
    from mcp_telegram.observation_order import allocate_observation_order
    from mcp_telegram.scheduled_contracts import scheduled_message_version
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope, rpc_scope

    path = tmp_path / "command.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = sqlite3.connect(path)
    reader.row_factory = sqlite3.Row
    fence = ScheduledMutationFence()
    _, sender, connection, sink = wire
    schedule_at = int(time.time()) + 3600

    def message(text: str) -> types.Message:
        return types.Message(
            id=7, peer_id=types.PeerUser(42), date=datetime.fromtimestamp(schedule_at, UTC), message=text, out=True
        )

    def seed(db: sqlite3.Connection) -> None:
        with write_transaction(db):
            db.execute("UPDATE scheduled_sync_state SET account_id=42 WHERE key='account'")
            baseline = allocate_observation_order(db)
            db.execute(
                "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,snapshot_observation_order,"
                "baseline_observed_at,coverage_state) VALUES(42,1,?,1,'complete')",
                (baseline,),
            )
            upsert_scheduled_message(db, 42, message("initial"))

    await writer.run(seed)
    manager = EventHandlerManager(
        cast(_EventHandlerClient, SimpleNamespace()),
        reader,
        asyncio.Event(),
        writer=writer,
        profile_detail_ttl_seconds=300,
        scheduled_mutation_fence=fence,
    )
    manager.set_self_id(42)
    accepted_event = True

    class Client:
        async def __call__(self, request: object) -> object:
            with rpc_scope(TelegramRpcSource.SCHEDULED_MUTATION):
                result = sender.send_owned(request, current_rpc_scope())
                state = await sender._send_queue.next_state()
                await sender._prepare_state(state)
            packet = connection._send_queue.get_nowait()
            if accepted_event:
                order = await writer.run(allocate_observation_order)
            else:
                row = reader.execute(
                    "SELECT observation_order FROM scheduled_messages WHERE dialog_id=42 AND message_id=7"
                ).fetchone()
                order = int(row[0]) - 1
            with delivered_update_context(DeliveredUpdate(order, time.time(), 0)):
                await manager.on_raw_new_scheduled_message(types.UpdateNewScheduledMessage(message("new local event")))
            dispatched = sender.write_packet(packet, connection._send)
            assert dispatched is not accepted_event
            if dispatched:
                state.future.set_result(
                    types.Updates(
                        [types.UpdateNewScheduledMessage(message("accepted edit"))], [], [], datetime.now(UTC), 1
                    )
                )
            return await result

    async def resolve_peer(_dialog_id: int, _topic_id: int | None) -> object:
        return types.InputPeerSelf()

    owner = ScheduledCommandOwner(
        Client(),
        reader,
        writer=writer,
        account_id=42,
        fence=fence,
        resolve_peer=resolve_peer,
        account_is_premium=lambda: False,
        request_recovery=AsyncMock(),
    )

    def request(key: str) -> dict[str, object]:
        row = reader.execute(
            "SELECT observation_order FROM scheduled_messages WHERE dialog_id=42 AND message_id=7"
        ).fetchone()
        return {
            "action": "update",
            "dialog_id": 42,
            "operation_key": key,
            "scheduled_message_id": 7,
            "expected_version": scheduled_message_version(42, 42, 7, int(row[0])),
            "text": "edited",
        }

    try:
        rejected = await owner.execute(request("accepted-event-race"))
        assert rejected["error"] == "scheduled_version_changed"
        assert rejected["mutation_dispatched"] is False
        assert sink.frames == []
        assert wire[0].account_safety_status() is None
        accepted_event = False
        acknowledged = await owner.execute(request("stale-event-followup"))
        assert acknowledged["outcome"] == "acknowledged"
        assert acknowledged["mutation_dispatched"] is True
        assert len(sink.frames) == 1
        assert wire[0]._transport_state.value == "ready"
        assert wire[0].account_safety_status() is None
        assert not wire[0]._ensure_safety_ledger()._record["intents"]
    finally:
        await writer.aclose()
        reader.close()


def test_full_supplied_message_captures_composition_flags_without_legacy_inference(conn: sqlite3.Connection) -> None:
    message = _message()
    message.invert_media = True
    message.video_processing_pending = True
    message.rich_message = types.RichMessage([], [], [])
    with write_transaction(conn):
        upsert_scheduled_message(conn, 200, message)
    assert conn.execute(
        "SELECT has_rich_message,invert_media,video_processing_pending FROM scheduled_messages"
    ).fetchone() == (1, 1, 1)
