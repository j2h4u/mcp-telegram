"""Startup reoffers only unresolved notes through the existing profile owner."""

import asyncio
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.contacts.guard import ContactPeerGuard
from mcp_telegram.daemon import (
    _note_recovery_watermark,
    _reconcile_note_receipts,
    _SyncMainContext,
    _unresolved_note_batch,
)
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.sync_db import ensure_sync_schema


@pytest.mark.asyncio
async def test_startup_recovery_pages_unique_current_account_and_stops_on_scope_change(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    for entity in range(1, 70):
        conn.execute(
            "INSERT INTO contact_note_operation_receipts(account_id,dc_id,key_id,operation_key,request_fingerprint,semantic_fingerprint,expected_version,action,entity_id,outcome,verification,created_at,updated_at) VALUES (1,2,'3',?,?,?,?,'update',?,'unknown','pending',0,0)",
            (f"key-{entity}", "a" * 64, "b" * 64, "c" * 64, entity),
        )
    conn.execute(
        "INSERT INTO contact_note_operation_receipts(account_id,dc_id,key_id,operation_key,request_fingerprint,semantic_fingerprint,expected_version,action,entity_id,outcome,verification,created_at,updated_at) VALUES (2,2,'3','other',?,?,?,'update',100,'unknown','pending',0,0)",
        ("a" * 64, "b" * 64, "c" * 64),
    )
    assert len(_unresolved_note_batch(conn, 1, 0, 69)) == 32
    assert _unresolved_note_batch(conn, 1, 64, 69) == [65, 66, 67, 68, 69]
    assert _note_recovery_watermark(conn, 1) == 69
    conn.execute(
        "INSERT INTO contact_note_operation_receipts(account_id,dc_id,key_id,operation_key,request_fingerprint,semantic_fingerprint,expected_version,action,entity_id,outcome,verification,created_at,updated_at) VALUES (1,2,'3','late',?,?,?,'update',100,'unknown','pending',0,0)",
        ("a" * 64, "b" * 64, "c" * 64),
    )
    assert _unresolved_note_batch(conn, 1, 64, 69) == [65, 66, 67, 68, 69]

    class Writer:
        async def run[T](self, callback: Callable[[sqlite3.Connection], T]) -> T:
            return callback(conn)

    scope_cell = ScopeCell()
    scope_cell.publish(TelegramAuthScope(1, 1, 2, 3))
    token = scope_cell.capture()
    assert token is not None
    called = []

    async def offer(entity_id: int) -> bool:
        called.append(entity_id)
        if entity_id == 3:
            scope_cell.begin_change()
        return True

    context = SimpleNamespace(
        client=SimpleNamespace(claim_image_scope=scope_cell.claim),
        shutdown_event=asyncio.Event(),
        writer=Writer(),
        api_server=SimpleNamespace(_get_entity_info_service=lambda: SimpleNamespace(request_note_reconciliation=offer)),
    )
    await _reconcile_note_receipts(cast(_SyncMainContext, context), token)
    assert called == [1, 2, 3]
    conn.close()


@pytest.mark.asyncio
async def test_recovery_finishes_initial_population_without_absorbing_later_receipts(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)

    def insert(account: int, key: str, entity: int, auth_key: str = "3") -> None:
        conn.execute(
            "INSERT INTO contact_note_operation_receipts(account_id,dc_id,key_id,operation_key,request_fingerprint,semantic_fingerprint,expected_version,action,entity_id,outcome,verification,created_at,updated_at) VALUES (?,2,?,?, ?,?,?,'update',?,'unknown','pending',0,0)",
            (account, auth_key, key, "a" * 64, "b" * 64, "c" * 64, entity),
        )

    for entity in range(1, 70):
        insert(1, f"key-{entity}", entity)
    insert(1, "old-auth-duplicate", 1, "4")
    insert(1, "old-auth-unique", 70, "4")
    insert(2, "other-account", 200)
    cell = ScopeCell()
    cell.publish(TelegramAuthScope(1, 1, 2, 3))
    token = cell.capture()
    assert token is not None
    called = []

    class Writer:
        async def run[T](self, callback: Callable[[sqlite3.Connection], T]) -> T:
            return callback(conn)

    async def offer(entity_id: int) -> bool:
        called.append(entity_id)
        if entity_id == 1:
            insert(1, "arrived-later", 100)
        return entity_id != 3

    context = SimpleNamespace(
        client=SimpleNamespace(claim_image_scope=cell.claim),
        shutdown_event=asyncio.Event(),
        writer=Writer(),
        api_server=SimpleNamespace(_get_entity_info_service=lambda: SimpleNamespace(request_note_reconciliation=offer)),
    )
    await _reconcile_note_receipts(cast(_SyncMainContext, context), token)
    assert called == list(range(1, 71))
    assert conn.execute(
        "SELECT outcome FROM contact_note_operation_receipts WHERE operation_key='key-3'"
    ).fetchone() == ("unknown",)
    conn.close()


@pytest.mark.asyncio
async def test_recovery_rearm_waits_for_cancelled_writer_and_deduplicates_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_telegram import daemon

    cell = ScopeCell()
    scope = TelegramAuthScope(1, 1, 2, 3)
    cell.publish(scope)
    entered, release = asyncio.Event(), asyncio.Event()
    invocations = []

    class Writer:
        async def run(self, _callback: object) -> object:
            invocations.append(1)
            if len(invocations) == 1:
                entered.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    await release.wait()
                    raise
            return

    context = SimpleNamespace(
        client=SimpleNamespace(capture_image_scope=cell.capture, claim_image_scope=cell.claim),
        note_recovery_token=None,
        note_recovery_task=None,
        background_tasks=set(),
        shutdown_event=asyncio.Event(),
        writer=Writer(),
        api_server=SimpleNamespace(_get_entity_info_service=lambda: object()),
    )
    outcomes = []
    monkeypatch.setattr(
        daemon,
        "_handle_tracked_task_completion",
        lambda _ctx, task, *, critical: outcomes.append((task.cancelled(), critical)),
    )
    ctx = cast(_SyncMainContext, context)
    daemon._start_note_receipt_recovery(ctx)
    first = ctx.note_recovery_task
    await entered.wait()
    daemon._start_note_receipt_recovery(ctx)
    assert ctx.note_recovery_task is first
    change = cell.begin_change()
    cell.end_change(change)
    cell.publish(scope)
    daemon._start_note_receipt_recovery(ctx)
    second = ctx.note_recovery_task
    assert second is not None
    await asyncio.sleep(0)
    assert second is not first and len(invocations) == 1
    release.set()
    await second
    assert len(invocations) == 2
    await asyncio.sleep(0)
    assert all(critical for _, critical in outcomes)


@pytest.mark.asyncio
async def test_recovery_offer_failure_routes_to_critical_shutdown() -> None:
    from unittest.mock import MagicMock

    from mcp_telegram import daemon

    cell = ScopeCell()
    cell.publish(TelegramAuthScope(1, 1, 2, 3))

    class Writer:
        calls = 0

        async def run(self, _callback: object) -> object:
            self.calls += 1
            return 1 if self.calls == 1 else [1]

    async def fail(_entity_id: int) -> bool:
        raise OSError("storage unavailable")

    server = SimpleNamespace(
        _get_entity_info_service=lambda: SimpleNamespace(request_note_reconciliation=fail),
        _ready=True,
        startup_detail="",
    )
    shutdown_request = MagicMock()
    shutdown = SimpleNamespace(request=shutdown_request)
    context = SimpleNamespace(
        client=SimpleNamespace(capture_image_scope=cell.capture, claim_image_scope=cell.claim),
        note_recovery_token=None,
        note_recovery_task=None,
        background_tasks=set(),
        shutdown_event=asyncio.Event(),
        writer=Writer(),
        api_server=server,
        shutdown_request=shutdown,
        rpc_observation_sink=None,
    )
    ctx = cast(_SyncMainContext, context)
    daemon._start_note_receipt_recovery(ctx)
    assert ctx.note_recovery_task is not None
    await asyncio.gather(ctx.note_recovery_task, return_exceptions=True)
    await asyncio.sleep(0)
    assert not ctx.api_server._ready
    shutdown_request.assert_called_once_with("critical_task_failed")


@pytest.mark.asyncio
async def test_reconciliation_offer_exception_requests_shutdown_but_false_does_not() -> None:
    from unittest.mock import AsyncMock, MagicMock

    from mcp_telegram import daemon

    reconcile = AsyncMock(return_value=False)
    service = SimpleNamespace(acquire_command_profile=AsyncMock(), request_note_reconciliation=reconcile)
    bind_reconciliation = MagicMock()
    authority = SimpleNamespace(bind_reconciliation=bind_reconciliation)
    gateway = SimpleNamespace(get_user_reference=MagicMock(), fetch_contact_note_limit=AsyncMock())
    shutdown_request = MagicMock()
    shutdown = SimpleNamespace(request=shutdown_request)
    context = SimpleNamespace(
        user_profile_port=gateway,
        contact_peer_guard=ContactPeerGuard(),
        note_write_authority=authority,
        client=SimpleNamespace(retained_auth_scope=lambda: None),
        conn=object(),
        writer=object(),
        api_server=SimpleNamespace(_get_entity_info_service=lambda: service),
        shutdown_request=shutdown,
    )
    daemon._compose_note_command_owner(cast(_SyncMainContext, context))
    assert bind_reconciliation.call_args is not None
    callback = cast(Callable[[int], Awaitable[bool]], cast(tuple[object, ...], bind_reconciliation.call_args.args)[0])
    assert await callback(7) is False
    shutdown_request.assert_not_called()
    reconcile.side_effect = OSError("storage unavailable")
    with pytest.raises(OSError):
        await callback(7)
    shutdown_request.assert_called_once_with("native_reconciliation_failed")


def test_folder_offer_exception_requests_shutdown() -> None:
    from unittest.mock import MagicMock

    from mcp_telegram import daemon

    shutdown_request = MagicMock()
    shutdown = SimpleNamespace(request=shutdown_request)
    context = SimpleNamespace(
        coordinator=SimpleNamespace(offer=MagicMock(side_effect=OSError("failure"))), shutdown_request=shutdown
    )
    with pytest.raises(OSError):
        daemon._offer_folder_verification(cast(_SyncMainContext, context))
    shutdown_request.assert_called_once_with("native_reconciliation_failed")


def test_startup_recovery_excludes_inconclusive_but_keeps_other_waits(tmp_path: Path) -> None:
    path = tmp_path / "recovery.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    for table in ("contact_note_operation_receipts", "contact_operation_receipts"):
        for entity, outcome, verification in (
            (10, "unknown", "pending"),
            (11, "unknown", "unavailable"),
            (12, "unknown", "inconclusive"),
            (13, "acknowledged", "pending"),
            (14, "acknowledged", "unavailable"),
            (15, "acknowledged", "inconclusive"),
        ):
            conn.execute(
                f"INSERT INTO {table}(account_id,dc_id,key_id,operation_key,request_fingerprint,semantic_fingerprint,expected_version,action,entity_id,outcome,verification,created_at,updated_at) VALUES (1,2,'3',?,?,?,?,'update',?,?,?,0,0)",
                (f"{table}-{entity}", "a" * 64, "b" * 64, "c" * 64, entity, outcome, verification),
            )
    assert _note_recovery_watermark(conn, 1) == 14
    assert _unresolved_note_batch(conn, 1, 0, 20) == [10, 11, 13, 14]
    conn.close()
