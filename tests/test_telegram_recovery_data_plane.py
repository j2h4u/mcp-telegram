"""Recovery followed by real ingestion and local projections on a test wire."""

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.tl import functions, types

from mcp_telegram import telegram_demand, telegram_rpc, telegram_rpc_scheduler
from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.daemon import _acquire_startup_identity_before_updates
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.dialog_directory import CanonicalDialogDirectory, CanonicalDialogDirectoryDemandAdapter
from mcp_telegram.message_history.telegram_adapter import TelethonFullHistoryPageAdapter
from mcp_telegram.peer_access import PeerAccess
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, load_rpc_safety_record
from mcp_telegram.sync_worker import FullSyncDemandAdapter, FullSyncWorker
from mcp_telegram.telegram_demand import DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_rpc import TelegramRpcBudget, TelegramRpcGate, TelegramRpcSafetyLedger
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.test_daemon import _ApiStub, _CadenceStub, _typed_ctx
from tests.test_daemon_api import _insert_message, _insert_synced_dialog, _response_messages, make_server
from tests.test_dialog_directory import _dialog, _pinned_response, _response
from tests.test_telegram_auto_resume import trial_wire as trial_wire
from tests.test_telegram_demand_coordinator import _adapters
from tests.test_telegram_rpc_safety import Clock
from tests.test_telegram_rpc_wire_integration import _Circuit, _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_telegram_transport import _Writer


async def _reply(wire: _WireHarness, expected: type, reply: object) -> None:
    _, sender, connection, _ = wire
    state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
    assert type(state.request) is expected
    await asyncio.wait_for(sender._prepare_state(state), 3)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result(reply)


def _fresh_wire(old: _WireHarness) -> _WireHarness:
    gate = old[0]
    fresh = TelegramRpcGate(
        gate.session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=gate._safety_store,
    )
    fresh._admission_scheduler._clock = asyncio.get_running_loop().time
    sender = cast(GuardedMTProtoSender, fresh._main_sender)
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 2, loggers=fresh._log)
    connection.bind_owner(sender)
    connection._connected = True
    sink = _Writer()
    connection._writer = cast(asyncio.StreamWriter, sink)
    connection._codec = FullPacketCodec(connection)
    sender._connection = connection
    sender._user_connected = True
    fresh._transport_state = telegram_rpc._TransportBoundaryState.READY
    fresh.configure_auto_resume(enabled=True)
    return fresh, sender, connection, sink


async def test_recovered_read_owners_publish_new_local_facts(  # noqa: PLR0915 - end-to-end recovery lifecycle
    trial_wire: tuple[_WireHarness, Clock], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old, clock = trial_wire
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = clock.monotonic_seconds - real_time()
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    monkeypatch.setattr(telegram_demand, "time", clock)
    monkeypatch.setattr(telegram_rpc_scheduler, "time", clock)
    old[0]._admission_scheduler._clock = loop.time
    await old[0].finish_startup_auto_resume()
    await old[2].disconnect()
    old = _fresh_wire(old)
    await old[0].prepare_startup_safety()
    await old[0].verify_startup_account(42)
    await _record_unknown(old)
    db_path = tmp_path / "sync.db"
    conn = _open_sync_db(db_path)
    writer = await SQLiteWriter.open(db_path)
    old_scope = old[0].retained_auth_scope()
    assert old_scope is not None
    old_access = PeerAccess(old_scope)
    await writer.run(old_access.bind)
    old[0].bind_peer_access(old_access)
    _insert_synced_dialog(conn, 1, status="syncing", enrollment_enabled=True)
    _insert_message(conn, 1, 7, text="retained")
    reader = make_server(conn, old[0])
    reader.self_id = 42
    await _check_retained(reader, old)
    assert not await old[0].maintain_auto_resume()
    clock.advance(299)
    assert not await old[0].maintain_auto_resume()
    clock.advance(1)
    offset = clock.monotonic_seconds - real_time()
    assert await old[0].maintain_auto_resume()
    fresh = _fresh_wire(old)
    directory = CanonicalDialogDirectory(fresh[0], db_path, asyncio.Event(), writer=writer)
    access: PeerAccess | None = None
    try:
        await writer.run(old_access.unbind)
        access = await _startup(fresh, conn, directory, writer)
        await _history(fresh, conn, writer, clock)
        await _directory(fresh, directory, conn, clock)
        await _check_local(reader, old, fresh)
    finally:
        await fresh[0]._admission_scheduler.close()
        await fresh[2].disconnect()
        await old[0]._admission_scheduler.close()
        await old[2].disconnect()
        if access is not None:
            await writer.run(access.unbind)
        await writer.aclose()
        conn.close()


async def _startup(
    wire: _WireHarness, conn: sqlite3.Connection, directory: CanonicalDialogDirectory, writer: SQLiteWriter
) -> PeerAccess:
    gate = wire[0]
    await gate.prepare_startup_safety()
    await gate.verify_startup_account(42)
    api = _ApiStub()
    api._client = gate
    ctx = _typed_ctx(conn=conn, client=gate, api_server=api, self_profile_cadence=_CadenceStub(), writer=writer)
    startup = asyncio.create_task(_acquire_startup_identity_before_updates(ctx, directory))
    try:
        try:
            await _reply(wire, functions.users.GetUsersRequest, [types.User(id=42, is_self=True)])
        except TimeoutError:
            if startup.done():
                await startup
            raise
        assert (await asyncio.wait_for(startup, 3)).result().account_id == 42
        await gate.finish_startup_auto_resume()
        assert ctx.peer_access is not None
        return ctx.peer_access
    except BaseException:
        if ctx.peer_access_bound and ctx.peer_access is not None:
            await writer.run(ctx.peer_access.unbind)
        raise


async def _history(wire: _WireHarness, conn: sqlite3.Connection, writer: SQLiteWriter, clock: Clock) -> None:
    gate = wire[0]
    gate._mb_entity_cache.extend([types.User(id=1, first_name="One", access_hash=42)], [])
    worker = FullSyncWorker(TelethonFullHistoryPageAdapter(gate), conn, asyncio.Event(), writer=writer)
    adapters = cast(dict[DemandKind, DurableDemandAdapter], dict(_adapters()))
    adapters[DemandKind.FULL_SYNC_PAGE] = FullSyncDemandAdapter(worker, account_id=42)
    coordinator = TelegramDemandCoordinator(adapters, clock=clock.time, monotonic_clock=clock.monotonic)
    coordinator.bind_canonical_progress(gate.note_canonical_progress)
    history = asyncio.create_task(coordinator._execute_slice(DemandKind.FULL_SYNC_PAGE))
    message = types.Message(id=8, peer_id=types.PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC), message="new")
    response = types.messages.Messages(
        messages=[message], chats=[], users=[types.User(id=1, first_name="One", access_hash=42)], topics=[]
    )
    await _reply(wire, functions.messages.GetHistoryRequest, response)
    await asyncio.wait_for(history, 3)
    assert gate._auto_resume_acquired and gate._auto_resume_progress


async def _directory(
    wire: _WireHarness, directory: CanonicalDialogDirectory, conn: sqlite3.Connection, clock: Clock
) -> None:
    adapter = CanonicalDialogDirectoryDemandAdapter(directory, conn)
    for expected, response in [
        (functions.messages.GetPinnedDialogsRequest, _pinned_response()),
        (functions.messages.GetPinnedDialogsRequest, _pinned_response()),
        (functions.messages.GetDialogsRequest, _response([_dialog(2, 9)])),
    ]:
        status = adapter.status(clock.time())
        assert status is not None and status.selected is not None
        task = asyncio.create_task(adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected)))
        await _reply(wire, expected, response)
        await asyncio.wait_for(task, 3)


async def _check_local(reader: DaemonAPIServer, old: _WireHarness, fresh: _WireHarness) -> None:
    counts = (len(old[3].frames), len(fresh[3].frames))
    result = await reader._list_messages({"dialog_id": 1, "limit": 10, "message_state": "sent"})
    assert "new" in [m["text"] for m in _response_messages(result)]
    reader.self_id = 42
    dialogs = await reader._get_reading_service().list_dialogs(
        {"verified_account_id": 42, "navigation": "start", "limit": 50}
    )
    assert any(row["id"] == 2 for row in dialogs["data"]["dialogs"])
    assert (len(old[3].frames), len(fresh[3].frames)) == counts


async def _check_retained(reader: DaemonAPIServer, wire: _WireHarness) -> None:
    count = len(wire[3].frames)
    result = await reader._list_messages({"dialog_id": 1, "limit": 10, "message_state": "sent"})
    assert [m["text"] for m in _response_messages(result)] == ["retained"]
    assert len(wire[3].frames) == count


async def test_unknown_mutation_never_starts_automatic_wire(wire: _WireHarness) -> None:
    gate, _, _, sink = wire
    gate.configure_auto_resume(enabled=True)
    ledger = gate._ensure_safety_ledger()
    await ledger.prepare("unknown-mutation", "telethon.tl.functions.messages.SaveDraftRequest:ad0fa15c")
    ledger.mark_claimed("unknown-mutation")
    await ledger.complete("unknown-mutation", known_outcome=False)
    assert not await gate.maintain_auto_resume()
    assert ledger.protection_reason == "unresolved_outbound_outcome"
    assert not sink.frames


async def test_real_sqlite_partial_recovery_write_rolls_back_on_reopen(tmp_path: Path, wire: _WireHarness) -> None:
    gate, _, _, sink = wire
    gate.configure_auto_resume(enabled=True)
    ledger = gate._ensure_safety_ledger()
    await ledger.prepare("read", "telethon.tl.functions.messages.GetHistoryRequest:4423e6c5")
    ledger.mark_claimed("read")
    conn = _open_sync_db(tmp_path / "sync.db")
    before = load_rpc_safety_record(conn)
    assert before is not None
    conn.execute(
        "CREATE TRIGGER fail_cooldown BEFORE UPDATE ON daemon_state "
        "WHEN NEW.key='telegram_account_cooldown_until_utc' "
        "BEGIN SELECT RAISE(ABORT, 'synthetic cooldown failure'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic cooldown failure"):
            await ledger.complete("read", known_outcome=False)
        conn.close()
        conn = _open_sync_db(tmp_path / "sync.db")
        assert load_rpc_safety_record(conn) == before
        session = gate.session
        assert session is not None and session.auth_key is not None
        reopened = TelegramRpcSafetyLedger(gate._safety_store, dc_id=2, key_id=session.auth_key.key_id)
        assert reopened.protection_reason == "unresolved_outbound_outcome"
        assert reopened.status()["blocking_request_count"] == 1
        assert not sink.frames
    finally:
        conn.execute("DROP TRIGGER IF EXISTS fail_cooldown")
        conn.close()


async def _record_unknown(wire: _WireHarness) -> None:
    ledger = wire[0]._ensure_safety_ledger()
    await ledger.prepare("unknown-history", "telethon.tl.functions.messages.GetHistoryRequest:4423e6c5")
    ledger.mark_claimed("unknown-history")
    await ledger.complete("unknown-history", known_outcome=False)
