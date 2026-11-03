"""Folder owner through the real gateway, ledger and guarded packet sender."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest
from telethon.crypto import AuthKey
from telethon.network.connection.tcpfull import FullPacketCodec
from telethon.sessions import MemorySession
from telethon.tl import functions, types

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.folders.commands import FolderCommandOwner, FolderReceiptOwner, folder_catalog_version
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.intake import invalidate_folder_intake
from mcp_telegram.folders.refresh import FolderRefreshResult
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository
from mcp_telegram.folders.telegram_adapter import folder_update_from_native
from mcp_telegram.native_write_fence import (
    FolderWriteRejected,
    NativeWireOutcome,
    NativeWriteDomainOwner,
    NativeWriteReceiptBinding,
)
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.peer_access import PeerAccess
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import (
    ensure_sync_schema,
    load_account_cooldown_until_utc,
    load_rpc_safety_owners,
    load_rpc_safety_record,
    open_sync_db_reader,
    save_rpc_safety_record,
)
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import AcquisitionKind
from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcGate,
    TelegramRpcSafetyStore,
    _TransportBoundaryState,
)
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcSource, current_rpc_scope, rpc_scope
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.helpers import reset_rpc_process_policy
from tests.test_telegram_rpc_wire_integration import _Circuit
from tests.test_telegram_transport import _Writer


@pytest.fixture
async def physical_folder(tmp_path: Path) -> AsyncIterator[tuple[object, ...]]:  # noqa: PLR0915 - compose the actual physical boundary.
    reset_rpc_process_policy()
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    reader = open_sync_db_reader(path)
    writer = await SQLiteWriter.open(path)

    async def save(expected: str | None, encoded: str) -> None:
        await writer.run(lambda conn: save_rpc_safety_record(conn, expected, encoded))

    store = TelegramRpcSafetyStore(
        lambda: load_rpc_safety_record(reader),
        save,
        lambda: load_account_cooldown_until_utc(reader),
        lambda: load_rpc_safety_owners(reader),
    )
    session = MemorySession()
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    gate = TelegramRpcGate(
        session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=store,
    )
    with telegram_session_lease(tmp_path) as lease:
        gate.bind_session_lease(lease)
        receipts = FolderReceiptOwner(writer, gate)
        gate.register_native_write_domain(
            "folder",
            NativeWriteDomainOwner(
                receipts.verify_request,
                receipts.bind_attempt,
                receipts.record_wire_outcome,
                receipts.validate_binding,
                receipts.recover_binding,
                receipts.preclaim,
                receipts.notify_terminal,
            ),
        )
        await gate.verify_startup_account(42)
        scope = gate.retained_auth_scope()
        assert scope is not None
        access = PeerAccess(scope)
        await writer.run(access.bind)
        gate.bind_peer_access(access)
        sender = cast(GuardedMTProtoSender, gate._main_sender)
        connection = GuardedConnectionTcpFull("127.0.0.1", 443, 2, loggers=gate._log)
        connection.bind_owner(sender)
        connection._connected = True
        sink = _Writer()
        connection._writer = cast(asyncio.StreamWriter, sink)
        connection._codec = FullPacketCodec(connection)
        sender._connection = connection
        sender._user_connected = True
        gate._transport_state = _TransportBoundaryState.READY

        def populate(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute(
                    "UPDATE dialog_directory_publication SET account_id=42,generation=7,observation_started_at=10,observation_completed_at=11 WHERE singleton=1"
                )
                SQLiteFolderSnapshotRepository(conn).project_observation(
                    FolderRuleObservation((FolderRule(4, "Old", included_ids=(43,)),), "initial", 100), completed_at=100
                )

        await writer.run(populate)
        try:
            yield gate, sender, connection, sink, writer, reader, receipts, access
        finally:
            for state in tuple(sender._owned_states):
                if not state.future.done():
                    state.future.cancel()
            await asyncio.sleep(0)
            await asyncio.gather(*sender._observing, return_exceptions=True)
            await gate.close_rpc_scheduler()
            await connection.disconnect()
            await writer.run(access.unbind)
            await writer.aclose()
            reader.close()
    reset_rpc_process_policy()


@pytest.mark.parametrize(
    "case",
    [
        "acknowledged",
        "rejected",
        "unknown",
        "create_unknown",
        "body_swap",
        "target_swap",
        "stale_intake",
        "forged_token",
        "receipt_failure",
        "auth_transition",
        "cancel_ack",
        "cancel_unknown",
        "cancel_admission",
    ],
)
async def test_folder_owner_physical_attempt_is_bound_and_not_replayed(  # noqa: PLR0914, PLR0915 - compare physical outcomes.
    physical_folder: tuple[object, ...], case: str
) -> None:
    gate, sender, connection, sink, writer, reader, receipts, _ = physical_folder
    gate = cast(TelegramRpcGate, gate)
    sender = cast(GuardedMTProtoSender, sender)
    connection = cast(GuardedConnectionTcpFull, connection)
    sink = cast(_Writer, sink)
    writer = cast(SQLiteWriter, writer)
    reader = cast(sqlite3.Connection, reader)
    receipts = cast(FolderReceiptOwner, receipts)
    mutations = []
    observed_rule = None
    recording, release = asyncio.Event(), asyncio.Event()
    entered_admission = asyncio.Event()
    offers: list[str] = []
    receipts.bind_terminal_offer(lambda: offers.append("terminal"))
    if case in {"cancel_ack", "cancel_unknown"}:
        from dataclasses import replace

        domain = gate._native_write_domains["folder"]

        async def delay_receipt(binding: NativeWriteReceiptBinding, attempt: str, outcome: NativeWireOutcome) -> None:
            recording.set()
            await release.wait()
            await domain.record_wire_outcome(binding, attempt, outcome)

        gate._native_write_domains["folder"] = replace(domain, record_wire_outcome=delay_receipt)
    if case == "receipt_failure":
        from dataclasses import replace

        async def fail_receipt(*_args: object) -> None:
            raise sqlite3.OperationalError("injected receipt failure")

        domain = gate._native_write_domains["folder"]
        gate._native_write_domains["folder"] = replace(domain, record_wire_outcome=fail_receipt)

    async def call(request: object) -> object:  # noqa: PLR0912, PLR0915 - inject faults at actual packet phases.
        nonlocal observed_rule
        response = sender.send_owned(request, current_rpc_scope())
        state = await sender._send_queue.next_state()
        if case == "forged_token" and isinstance(request, functions.messages.UpdateDialogFilterRequest):
            from dataclasses import replace

            from mcp_telegram.telegram_rpc import _WireAttempt

            owner = cast(_WireAttempt, sender.application_owner(response))
            assert owner.native_check is not None

            async def forged():
                return lambda: True

            owner.native_check = replace(owner.native_check, revalidate=forged)
        if isinstance(request, functions.messages.UpdateDialogFilterRequest):
            if case == "body_swap":
                assert isinstance(request.filter, types.DialogFilter)
                request.filter.title.text = "altered"
            if case == "target_swap":
                request.id += 1
        try:
            await sender._prepare_state(state)
        except FolderWriteRejected:
            return await response
        packet = connection._send_queue.get_nowait()
        if case == "auth_transition" and isinstance(request, functions.messages.UpdateDialogFilterRequest):
            from dataclasses import replace

            ledger = gate._ensure_safety_ledger()
            original_store = ledger._store
            entered, finish = asyncio.Event(), asyncio.Event()

            async def block_save(expected: str | None, encoded: str) -> None:
                entered.set()
                await finish.wait()
                await original_store.save_record(expected, encoded)

            ledger._store = replace(original_store, save_record=block_save)
            scope = gate.retained_auth_scope()
            assert scope is not None
            transition = asyncio.create_task(
                ledger.begin_authentication(scope.dc_id, scope.auth_key_id, acknowledged=True)
            )
            await entered.wait()
            assert gate.retained_auth_scope() == scope
            assert not sender.write_packet(packet, connection._send)
            finish.set()
            await transition
            ledger._store = original_store
            return await response
        if case in {"stale_intake", "forged_token"} and isinstance(
            request, functions.messages.UpdateDialogFilterRequest
        ):
            invalidate_folder_intake()
            assert not sender.write_packet(packet, connection._send)
            return await response
        assert sender.write_packet(packet, connection._send)
        if isinstance(request, functions.messages.UpdateDialogFilterRequest):
            mutations.append(request)
            if case != "rejected":
                observed_rule = folder_update_from_native(types.UpdateDialogFilter(request.id, request.filter)).rule
            if case in {"unknown", "create_unknown", "cancel_unknown"}:
                state.future.set_exception(RuntimeError("lost response"))
            else:
                state.future.set_result(case != "rejected")
        else:
            state.future.set_result(True)
        return await response

    class Client:
        retained_auth_scope = gate.retained_auth_scope
        capture_image_scope = gate.capture_image_scope
        claim_image_scope = gate.claim_image_scope

        def retained_draft_peer(self, dialog_id: int) -> object:
            return types.InputPeerUser(dialog_id, 55)

        async def __call__(self, _request: object, **_kwargs: object) -> object:
            request = _request
            if case == "cancel_admission" and isinstance(request, functions.messages.UpdateDialogFilterRequest):
                entered_admission.set()
                await asyncio.Event().wait()
            return await call(request)

    client = Client()
    receipts._client = client

    class Worker:
        calls = 0

        async def force_refresh(self, budget: object = None) -> FolderRefreshResult:
            self.calls += 1
            with rpc_scope(TelegramRpcSource.FOLDER_RECONCILIATION, acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT):
                order = await writer.run(allocate_observation_order)
                await call(functions.messages.GetDialogFiltersRequest())
            if observed_rule is not None:
                rule = observed_rule
                await writer.run(
                    lambda conn: SQLiteFolderSnapshotRepository(conn).project_observation(
                        FolderRuleObservation(
                            (FolderRule(4, "Old", included_ids=(43,)), rule) if case == "create_unknown" else (rule,),
                            "changed",
                            101,
                        ),
                        completed_at=101,
                    )
                )
            return FolderRefreshResult(0, 7, "initial", 100, observation_order=order)

    worker = Worker()
    owner = FolderCommandOwner(
        client, writer, worker, reader, receipts, offer_verification=lambda: offers.append("early")
    )  # type: ignore[arg-type]
    version = folder_catalog_version(reader, 42)
    request = {
        "action": "update",
        "operation_key": "once",
        "folder_id": 4,
        "expected_version": version,
        "add_dialog_ids": [44],
    }
    if case == "create_unknown":
        request = {
            "action": "create",
            "operation_key": "once",
            "expected_catalog_version": version,
            "title": "Created",
            "dialog_ids": [44],
        }
    if case in {"cancel_ack", "cancel_unknown"}:
        task = asyncio.create_task(owner.execute(request))
        await recording.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert offers == ["early"]
        row = cast(
            tuple[object, ...] | None,
            reader.execute(
                "SELECT outcome FROM folder_command_receipts WHERE account_id=42 AND operation_key='once'"
            ).fetchone(),
        )
        assert row is not None and row[0] == "pending"
        release.set()
        await asyncio.gather(*sender._observing, return_exceptions=True)
        assert offers == ["early", "terminal"]
        row = cast(
            tuple[object, ...] | None,
            reader.execute(
                "SELECT outcome,attempt_id FROM folder_command_receipts WHERE account_id=42 AND operation_key='once'"
            ).fetchone(),
        )
        assert row is not None and row[0] == ("unknown" if case == "cancel_unknown" else "acknowledged")
        assert len(mutations) == 1 and row[1]
        return
    if case == "cancel_admission":
        task = asyncio.create_task(owner.execute(request))
        await entered_admission.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        row = cast(
            tuple[object, ...] | None,
            reader.execute(
                "SELECT outcome,attempt_id FROM folder_command_receipts WHERE account_id=42 AND operation_key='once'"
            ).fetchone(),
        )
        assert row is not None and tuple(row) == ("not_sent", None)
        assert not mutations and len(sink.frames) == 1 and "terminal" not in offers
        return
    result = await owner.execute(request)
    row = cast(
        tuple[object, ...] | None,
        reader.execute(
            "SELECT outcome,attempt_id FROM folder_command_receipts WHERE account_id=42 AND operation_key='once'"
        ).fetchone(),
    )
    assert row is not None
    if case in {"body_swap", "target_swap", "stale_intake", "forged_token", "auth_transition"}:
        assert len(mutations) == 0
        assert row[0] == ("rejected" if case in {"stale_intake", "forged_token", "auth_transition"} else "not_sent")
        assert result["outcome"] == "not_sent" and result["dispatch"] == "not_sent"
    elif case == "receipt_failure":
        assert len(mutations) == 1 and row[0] == "pending"
        assert result["outcome"] == "unknown" and gate.account_safety_status() is not None
    else:
        assert len(mutations) == 1
        expected = "unknown" if case == "create_unknown" else case
        assert row[0] == expected
        assert result["outcome"] == expected
        if case == "acknowledged":
            assert "terminal" not in offers
            assert worker.calls == 2
    repeated = await owner.execute(request)
    if case == "create_unknown":
        assert repeated["folder_id"] == result["folder_id"]
    assert len(mutations) <= 1
    assert len(sink.frames) >= 1


@pytest.mark.parametrize("scenario", ["admission_cancel", "late_ack"])
async def test_folder_production_sender_cancellation_and_late_terminal_wake(  # noqa: PLR0914, PLR0915 - exercise the production sender boundary.
    physical_folder: tuple[object, ...], scenario: str
) -> None:
    import time

    from mcp_telegram.telegram_rpc import _AdmissionAwareSender, _WireAttempt

    gate, sender, connection, sink, writer, reader, receipts, _ = physical_folder
    gate = cast(TelegramRpcGate, gate)
    sender = cast(GuardedMTProtoSender, sender)
    connection = cast(GuardedConnectionTcpFull, connection)
    sink = cast(_Writer, sink)
    writer = cast(SQLiteWriter, writer)
    reader = cast(sqlite3.Connection, reader)
    receipts = cast(FolderReceiptOwner, receipts)
    reached, release = asyncio.Event(), asyncio.Event()
    offers: list[str] = []
    receipts.bind_terminal_offer(lambda: offers.append("terminal"))
    pump_tasks: list[asyncio.Task[None]] = []

    async def pump(request: object) -> None:
        state = await sender._send_queue.next_state()
        mutation = isinstance(request, functions.messages.UpdateDialogFilterRequest)
        if scenario == "admission_cancel" and mutation:
            reached.set()
            await release.wait()
        try:
            await sender._prepare_state(state)
        except FolderWriteRejected:
            return
        packet = connection._send_queue.get_nowait()
        if not mutation:
            assert sender.write_packet(packet, connection._send)
            state.future.set_result(True)
            return
        if scenario == "admission_cancel":
            assert not sender.write_packet(packet, connection._send)
            return
        assert sender.write_packet(packet, connection._send)
        wire_owner = cast(_WireAttempt, packet.state.owner)
        assert wire_owner.response_timer is not None
        wire_owner.response_timer.reschedule(time.monotonic() + 0.1)
        reached.set()
        await release.wait()
        state.future.set_result(True)

    class Client:
        retained_auth_scope = gate.retained_auth_scope
        capture_image_scope = gate.capture_image_scope
        claim_image_scope = gate.claim_image_scope

        def retained_draft_peer(self, dialog_id: int) -> object:
            return types.InputPeerUser(dialog_id, 55)

        async def __call__(self, _request: object, **_kwargs: object) -> object:
            request = _request
            scope = current_rpc_scope()
            pump_tasks.append(asyncio.create_task(pump(request)))
            return await _AdmissionAwareSender(gate, scope).send(request)

    class Worker:
        async def force_refresh(self, budget: object = None) -> FolderRefreshResult:
            with rpc_scope(TelegramRpcSource.FOLDER_RECONCILIATION, acquisition_kind=AcquisitionKind.FOLDER_SNAPSHOT):
                order = await writer.run(allocate_observation_order)
                await client(functions.messages.GetDialogFiltersRequest())
            return FolderRefreshResult(0, 7, "initial", 100, observation_order=order)

    client = Client()
    receipts._client = client
    owner = FolderCommandOwner(
        client, writer, Worker(), reader, receipts, offer_verification=lambda: offers.append("early")
    )  # type: ignore[arg-type]
    request = {
        "action": "update",
        "operation_key": "once",
        "folder_id": 4,
        "expected_version": folder_catalog_version(reader, 42),
        "add_dialog_ids": [44],
    }
    task = asyncio.create_task(owner.execute(request))
    await asyncio.wait_for(reached.wait(), timeout=15)
    if scenario == "admission_cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["outcome"] == "unknown"
    assert offers == ["early"]
    release.set()
    await asyncio.gather(*pump_tasks, return_exceptions=True)
    await asyncio.gather(*sender._observing, return_exceptions=True)
    row = cast(
        tuple[object, ...] | None,
        reader.execute(
            "SELECT outcome,attempt_id FROM folder_command_receipts WHERE account_id=42 AND operation_key='once'"
        ).fetchone(),
    )
    assert row is not None
    if scenario == "admission_cancel":
        assert len(sink.frames) == 1 and row[0] == "not_sent" and "terminal" not in offers
    else:
        assert len(sink.frames) == 2 and row[0] == "acknowledged" and offers == ["early", "terminal"]
