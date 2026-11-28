"""Scheduled commands remain safe across access and acquisition changes."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types

from mcp_telegram.access_lifecycle import restore_access_after_revalidation, set_access_lost
from mcp_telegram.dialog_directory import CanonicalDialogDirectory
from mcp_telegram.scheduled_commands import read_scheduled_operation
from mcp_telegram.scheduled_messages import (
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    upsert_scheduled_message,
)
from mcp_telegram.scheduled_write_fence import ScheduledMutationFence
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.sqlite_writer_helpers import inline_writer
from tests.test_dialog_directory import _dialog, _FakeClient, _pinned_response, _writer
from tests.test_scheduled_commands import _owner_create_request, _OwnerClient, _OwnerHarness
from tests.test_scheduled_messages import _message, _ScheduledSnapshotClient
from tests.test_telegram_rpc_scheduled_mutation import _check, _clock, _expect_refusal, _prepare, _WireHarness
from tests.test_telegram_rpc_scheduled_mutation import wire as wire


@pytest.mark.parametrize(
    ("kind", "block"),
    [
        ("subscriber", "permission_denied"),
        ("creator", None),
        ("admin", None),
        ("group", None),
        ("min", "admin_required"),
    ],
)
@pytest.mark.parametrize("existing_dialog", [True, False])
async def test_directory_publication_applies_full_channel_permissions(
    tmp_path: Path, kind: str, block: str | None, existing_dialog: bool
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    if existing_dialog:
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES(-1000000000090,'channel')")
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,acquisition_block) VALUES(-1000000000090,1,'admin_required')"
    )
    conn.commit()
    dialog = _dialog(90, 8)
    dialog.peer = types.PeerChannel(90)
    channel = types.Channel(
        id=90,
        title="feed",
        photo=types.ChatPhotoEmpty(),
        date=None,
        broadcast=kind != "group",
        megagroup=kind == "group",
        creator=kind == "creator",
        min=kind == "min",
        admin_rights=types.ChatAdminRights(post_messages=True) if kind == "admin" else None,
    )
    response = types.messages.Dialogs(dialogs=[dialog], messages=[], chats=[channel], users=[])
    directory = CanonicalDialogDirectory(
        _FakeClient([_pinned_response(), _pinned_response(), response]), path, asyncio.Event(), writer=_writer(path)
    )
    try:
        await directory.bind_account_id(100)
        for _ in range(3):
            await directory.run_slice()
        assert conn.execute("SELECT status FROM dialog_directory_state").fetchone() == ("complete",)
        assert conn.execute(
            "SELECT acquisition_block FROM scheduled_reconciliation_state WHERE dialog_id=-1000000000090"
        ).fetchone() == (block,)
    finally:
        conn.close()


async def test_access_loss_after_permit_prevents_wire_dispatch(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock = _clock(wire, monkeypatch)
    _, sender, connection, sink = wire
    schedule_at = int(clock.time()) + 300
    check = _check("create", schedule_at, ScheduledMutationFence())
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, "create", schedule_at, check, budget)
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    try:
        with write_transaction(conn):
            conn.execute("UPDATE scheduled_sync_state SET account_id=42 WHERE key='account'")
            set_access_lost(conn, 42, int(clock.time()))
        assert not sender.write_packet(packet, connection._send)
        await _expect_refusal(response, sender._observing, "scheduled_version_changed")
        assert sink.frames == [] and budget.attempts == evidence.actual_attempts == 0
    finally:
        conn.close()


@pytest.mark.parametrize("change", ["identical", "title", "rejected"])
async def test_directory_publication_does_not_rearm_on_unrelated_or_rejected_evidence(
    tmp_path: Path, change: str
) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    dialog = _dialog(90, 8)
    dialog.peer = types.PeerChannel(90)

    def response(*, title: str, creator: bool = False) -> object:
        channel = types.Channel(
            id=90, title=title, photo=types.ChatPhotoEmpty(), date=None, broadcast=True, creator=creator
        )
        return types.messages.Dialogs(dialogs=[dialog], messages=[], chats=[channel], users=[])

    directory = CanonicalDialogDirectory(
        _FakeClient(
            [
                _pinned_response(),
                _pinned_response(),
                response(title="original"),
                _pinned_response(),
                _pinned_response(),
                response(title="changed" if change == "title" else "original", creator=change == "rejected"),
            ]
        ),
        path,
        asyncio.Event(),
        writer=_writer(path),
    )
    await directory.bind_account_id(100)
    for _ in range(3):
        await directory.run_slice()
    conn = sqlite3.connect(path)
    try:
        with write_transaction(conn):
            conn.execute(
                "UPDATE scheduled_reconciliation_state SET acquisition_block='admin_required', dirty_generation=dirty_generation+1 "
                "WHERE dialog_id=-1000000000090"
            )
        for _ in range(2):
            await directory.run_slice()
        if change == "rejected":
            with write_transaction(conn):
                conn.execute("UPDATE dialogs SET revision=revision+1 WHERE dialog_id=-1000000000090")
        await directory.run_slice()
        assert conn.execute(
            "SELECT acquisition_block FROM scheduled_reconciliation_state WHERE dialog_id=-1000000000090"
        ).fetchone() == ("admin_required",)
    finally:
        conn.close()


async def test_unknown_receipt_is_returned_before_access_check_without_replay(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        request = _owner_create_request(harness, "lost-ack")
        harness.client.fail_after_dispatch = True
        first = await harness.owner.execute(request)
        assert first["outcome"] == "unknown" and harness.client.dispatched_count == 1
        with write_transaction(harness.conn):
            set_access_lost(harness.conn, 200, 100)
        retry_client = _OwnerClient(harness)
        retry = harness.new_owner(retry_client)
        assert await retry.execute(request) == first
        assert retry_client.requests == []
        assert harness.first_use_requests == []
    finally:
        harness.close()


async def test_lost_ack_and_failed_recovery_blocks_new_key_after_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    harness = _OwnerHarness(path, schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            upsert_scheduled_message(harness.conn, 200, _message(11, "retained"), now=100)
        generation = cast(
            tuple[int],
            harness.conn.execute(
                "SELECT dirty_generation FROM scheduled_reconciliation_state WHERE dialog_id=200"
            ).fetchone(),
        )[0]
        harness.client.fail_after_dispatch = True
        harness.fail_recovery = True

        def fail_wake() -> None:
            raise RuntimeError("wake unavailable")

        harness.owner._wake_recovery = fail_wake
        request = _owner_create_request(harness, "lost")
        first = await harness.owner.execute(request)
        assert first["outcome"] == "unknown"
        assert tuple(
            cast(
                tuple[object, ...],
                harness.conn.execute(
                    "SELECT coverage_state,dirty_generation FROM scheduled_reconciliation_state WHERE dialog_id=200"
                ).fetchone(),
            )
        ) == ("unknown", generation + 1)
        harness.conn.close()
        harness.conn = sqlite3.connect(path)
        harness.conn.row_factory = sqlite3.Row
        client = _OwnerClient(harness)
        owner = harness.new_owner(client)
        assert await owner.execute(request) == first
        blocked = await owner.execute(_owner_create_request(harness, "new"))
        assert blocked["error"] == "scheduled_state_unknown"
        assert client.dispatched_count == 0
        assert tuple(
            cast(
                tuple[object, ...],
                harness.conn.execute(
                    "SELECT text,message_state FROM scheduled_messages WHERE message_id=11"
                ).fetchone(),
            )
        ) == ("retained", "scheduled")
        worker = ScheduledMessageReconciler(
            _ScheduledSnapshotClient({200: [_message(11, "retained")]}),
            harness.conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
            writer=inline_writer(harness.conn),
        )
        await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY)
        assert (await owner.execute(_owner_create_request(harness, "new")))["outcome"] == "acknowledged"
        assert client.dispatched_count == 1
    finally:
        harness.close()


async def test_failed_terminal_transaction_keeps_pending_and_blocks_new_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram import scheduled_commands

    path = tmp_path / "sync.db"
    harness = _OwnerHarness(path, schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        harness.client.fail_after_dispatch = True
        harness.fail_recovery = True

        def fail_invalidation(*_args: object) -> None:
            raise sqlite3.OperationalError("injected failure")

        monkeypatch.setattr(scheduled_commands, "invalidate_scheduled_continuity", fail_invalidation)
        request = _owner_create_request(harness, "pending")
        result = await harness.owner.execute(request)
        assert result["outcome"] == "unknown" and result["mutation_dispatched"] is True
        assert result["receipt_persisted"] is False
        receipt = read_scheduled_operation(harness.conn, 100, "pending")
        assert receipt is not None and receipt.outcome == "pending"
        with write_transaction(harness.conn):
            harness.conn.execute(
                "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,snapshot_observation_order,coverage_state) "
                "VALUES(201,1,1,'complete')"
            )
        assert scheduled_commands._current_state(harness.conn, 100, 201, None) == (None, 1)
        assert (await harness.owner.execute(_owner_create_request(harness, "other")))[
            "error"
        ] == "scheduled_state_unknown"
        harness.conn.close()
        harness.conn = sqlite3.connect(path)
        harness.conn.row_factory = sqlite3.Row
        client = _OwnerClient(harness)
        owner = harness.new_owner(client)
        assert (await owner.execute(request))["outcome"] == "unknown"
        assert (await owner.execute(_owner_create_request(harness, "other")))["error"] == "scheduled_state_unknown"
        assert client.dispatched_count == 0
    finally:
        harness.close()


@pytest.mark.parametrize("failures", [1, 2])
async def test_terminal_write_recovers_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    from mcp_telegram import scheduled_commands

    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    original = scheduled_commands.invalidate_scheduled_continuity
    attempts = 0

    def transient_failure(conn: sqlite3.Connection, dialog_id: int, now: int, reason: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= failures:
            raise sqlite3.OperationalError("temporary failure")
        original(conn, dialog_id, now, reason)

    monkeypatch.setattr(scheduled_commands, "invalidate_scheduled_continuity", transient_failure)
    try:
        with write_transaction(harness.conn):
            upsert_scheduled_message(harness.conn, 200, _message(11, "retained"), now=100)
        harness.client.fail_after_dispatch = True
        harness.fail_recovery = True
        first = await harness.owner.execute(_owner_create_request(harness, "lost"))
        assert first["outcome"] == "unknown"
        assert first["receipt_persisted"] is False
        fresh = _owner_create_request(harness, "fresh")
        assert (await harness.owner.execute(fresh))["error"] == "scheduled_state_unknown"
        if failures == 2:
            assert (await harness.owner.execute(fresh))["error"] == "scheduled_state_unknown"
        assert harness.client.dispatched_count == 1
        receipt = read_scheduled_operation(harness.conn, 100, "lost")
        assert receipt is not None and receipt.outcome == "unknown"
        worker = ScheduledMessageReconciler(
            _ScheduledSnapshotClient({200: [_message(11, "retained")]}),
            harness.conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
            writer=inline_writer(harness.conn),
        )
        await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY)
        harness.client.fail_after_dispatch = False
        resumed = await harness.owner.execute(fresh)
        assert resumed.get("outcome") == "acknowledged", resumed
        assert harness.client.dispatched_count == 2
    finally:
        harness.close()


async def test_failed_acknowledged_receipt_is_settled_before_new_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram import scheduled_commands

    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    original = scheduled_commands.finish_scheduled_operation

    def fail_acknowledged(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError("temporary failure")

    monkeypatch.setattr(scheduled_commands, "finish_scheduled_operation", fail_acknowledged)
    try:
        with write_transaction(harness.conn):
            upsert_scheduled_message(harness.conn, 200, _message(11, "retained"), now=100)
        first = await harness.owner.execute(_owner_create_request(harness, "ack"))
        assert first["outcome"] == "acknowledged" and first["receipt_persisted"] is False
        receipt = read_scheduled_operation(harness.conn, 100, "ack")
        assert receipt is not None and receipt.outcome == "pending"
        fresh = _owner_create_request(harness, "fresh")
        assert (await harness.owner.execute(fresh))["error"] == "scheduled_state_unknown"
        receipt = read_scheduled_operation(harness.conn, 100, "ack")
        assert receipt is not None and receipt.outcome == "unknown"
        assert harness.client.dispatched_count == 1
        worker = ScheduledMessageReconciler(
            _ScheduledSnapshotClient({200: [_message(11, "retained")]}),
            harness.conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
            writer=inline_writer(harness.conn),
        )
        await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY)
        monkeypatch.setattr(scheduled_commands, "finish_scheduled_operation", original)
        assert (await harness.owner.execute(fresh))["outcome"] == "acknowledged"
        assert harness.client.dispatched_count == 2
    finally:
        harness.close()


async def test_cancelled_operation_with_failed_settlement_is_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram import scheduled_commands

    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    original = scheduled_commands.invalidate_scheduled_continuity

    class CancelledClient(_OwnerClient):
        async def __call__(self, request: object) -> object:
            try:
                return await super().__call__(request)
            except TimeoutError as error:
                raise asyncio.CancelledError from error

    def fail_invalidation(*_args: object) -> None:
        raise sqlite3.OperationalError("temporary failure")

    try:
        client = CancelledClient(harness)
        client.fail_after_dispatch = True
        harness.fail_recovery = True
        owner = harness.new_owner(client)
        monkeypatch.setattr(scheduled_commands, "invalidate_scheduled_continuity", fail_invalidation)
        with pytest.raises(asyncio.CancelledError):
            await owner.execute(_owner_create_request(harness, "cancelled"))
        receipt = read_scheduled_operation(harness.conn, 100, "cancelled")
        assert receipt is not None and receipt.outcome == "pending"
        monkeypatch.setattr(scheduled_commands, "invalidate_scheduled_continuity", original)
        assert (await owner.execute(_owner_create_request(harness, "fresh")))["error"] == "scheduled_state_unknown"
        receipt = read_scheduled_operation(harness.conn, 100, "cancelled")
        assert receipt is not None and receipt.outcome == "unknown"
        assert client.dispatched_count == 1
    finally:
        harness.close()


async def test_fresh_command_rearms_block_but_only_requests_baseline(tmp_path: Path) -> None:
    harness = _OwnerHarness(tmp_path / "sync.db", schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            harness.conn.execute(
                "UPDATE scheduled_reconciliation_state SET acquisition_block='admin_required' WHERE dialog_id=200"
            )
        harness.first_use_accept = True
        result = await harness.owner.execute(_owner_create_request(harness, "fresh"))
        assert result["error"] == "scheduled_state_unknown"
        assert result["mutation_dispatched"] is False and result["receipt_persisted"] is False
        assert harness.first_use_requests == [(100, 200)] and harness.client.requests == []
        assert read_scheduled_operation(harness.conn, 100, "fresh") is None
        row = cast(
            sqlite3.Row,
            harness.conn.execute(
                "SELECT acquisition_block,authorization_pending FROM scheduled_reconciliation_state WHERE dialog_id=200"
            ).fetchone(),
        )
        assert tuple(row) == (None, 1)
    finally:
        harness.close()


async def test_restored_access_requires_fresh_snapshot_and_retains_facts_across_reopen(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    harness = _OwnerHarness(path, schedule_at=int(datetime.now(UTC).timestamp()) + 900)
    try:
        with write_transaction(harness.conn):
            harness.conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES(200,'synced')")
            upsert_scheduled_message(harness.conn, 200, _message(11, "retained"), now=100)
            set_access_lost(harness.conn, 200, 101)
        harness.conn.close()
        harness.conn = sqlite3.connect(path)
        harness.conn.row_factory = sqlite3.Row
        harness.owner = harness.new_owner(harness.client)
        client = _ScheduledSnapshotClient({200: [_message(11, "retained")]})
        worker = ScheduledMessageReconciler(
            client,
            harness.conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
            writer=inline_writer(harness.conn),
        )
        assert await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY) == 0
        assert client.requests == []
        with write_transaction(harness.conn):
            restore_access_after_revalidation(harness.conn, 200, 102)
        harness.first_use_accept = True
        pending = await harness.owner.execute(_owner_create_request(harness, "after-restore"))
        assert pending["error"] == "scheduled_state_unknown" and harness.client.requests == []
        assert tuple(
            cast(
                sqlite3.Row,
                harness.conn.execute(
                    "SELECT text,message_state FROM scheduled_messages WHERE message_id=11"
                ).fetchone(),
            )
        ) == ("retained", "scheduled")
        await worker.run_demand_slice(DemandKind.SCHEDULED_REPAIR)
        await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY)
        assert len(client.requests) == 1
        assert await harness.owner.execute(_owner_create_request(harness, "after-restore")) != pending
        assert harness.client.dispatched_count == 1
    finally:
        harness.close()
