"""Native receipt recovery uses actual SQLite and preserves other safety holds."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.contact_notes import receipts
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.folders import command_receipts as folder_receipts
from mcp_telegram.folders.commands import FolderReceiptOwner
from mcp_telegram.native_write_fence import (
    NativeWriteCheck,
    NativeWriteDomain,
    NativeWriteDomainOwner,
    NativeWriteReceiptBinding,
)
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_rpc import (
    TelegramRpcBudget,
    TelegramRpcGate,
    TelegramRpcSafetyLedger,
    _canonical_wire_method,
    _WireAttempt,
)
from mcp_telegram.telegram_rpc_recovery import RecoveryCause
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from tests.test_telegram_rpc_wire_integration import _Circuit, _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.parametrize("receipt_state", ["pending", "missing", "mismatch"])
@pytest.mark.parametrize("independent_fault", [False, True])
@pytest.mark.parametrize("domain", ["folder", "contact_note"])
async def test_restart_requires_exact_native_receipt(  # noqa: PLR0914, PLR0915
    wire: _WireHarness, tmp_path: Path, receipt_state: str, independent_fault: bool, domain: str
) -> None:
    gate, _, _, _ = wire
    auth = gate.retained_auth_scope()
    assert auth is not None
    binding = NativeWriteReceiptBinding(
        cast(NativeWriteDomain, domain), 42, auth.dc_id, auth.auth_key_id, "op", "update", 8, "f" * 64, 1
    )
    writer = await SQLiteWriter.open(tmp_path / "sync.db")
    try:
        if receipt_state != "missing":
            actual = (
                binding
                if receipt_state == "pending"
                else NativeWriteReceiptBinding(
                    cast(NativeWriteDomain, domain), 42, auth.dc_id, auth.auth_key_id, "op", "update", 9, "f" * 64, 1
                )
            )
            if domain == "folder":
                await writer.run(
                    lambda conn: folder_receipts.insert_operation(
                        conn,
                        actual,
                        operation_fingerprint="a" * 64,
                        expected_version="v1",
                        before=None,
                        intended=None,
                        now=1,
                    )
                )
                await writer.run(lambda conn: folder_receipts.bind_attempt(conn, actual, "native-attempt"))
            else:
                await writer.run(
                    lambda conn: receipts.reserve_operation(conn, actual, "a" * 64, 1, expected_version="b" * 64)
                )
                await writer.run(lambda conn: receipts.bind_attempt(conn, actual, "native-attempt"))
        ledger = gate._ensure_safety_ledger()
        request = (
            functions.messages.UpdateDialogFilterRequest
            if domain == "folder"
            else functions.contacts.UpdateContactNoteRequest
        )
        method = f"{request.__module__}.{request.__name__}:{request.CONSTRUCTOR_ID:08x}"
        await ledger.prepare("native-attempt", method, native_binding=binding)
        ledger.mark_claimed("native-attempt")
        if independent_fault:
            await ledger.prepare("independent-read", _canonical_wire_method(functions.updates.GetStateRequest()))
            ledger.mark_claimed("independent-read")
        restarted_gate = TelegramRpcGate(
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
        try:
            if domain == "folder":
                owner = FolderReceiptOwner(writer, restarted_gate)
                domain_owner = NativeWriteDomainOwner(
                    owner.verify_request,
                    owner.bind_attempt,
                    owner.record_wire_outcome,
                    owner.validate_binding,
                    owner.recover_binding,
                    owner.preclaim,
                    owner.notify_terminal,
                )
            else:
                domain_owner = NoteWriteAuthority(writer, restarted_gate.retained_auth_scope, lambda _: None).domain
            with pytest.raises(ValueError, match="invalid native write domain owner"):
                restarted_gate.register_native_write_domain(
                    cast(NativeWriteDomain, domain), replace(domain_owner, notify_terminal=None)
                )
            restarted_gate.register_native_write_domain(cast(NativeWriteDomain, domain), domain_owner)
            blocked = receipt_state != "pending" or independent_fault
            if blocked:
                with pytest.raises(TelegramRpcThrottled):
                    await restarted_gate.prepare_startup_safety()
            else:
                await restarted_gate.prepare_startup_safety()
            restarted = restarted_gate._ensure_safety_ledger()
            status = restarted.status()
            assert status["blocking_request_count"] == int(receipt_state != "pending") + int(independent_fault)
            assert restarted.protection_reason == ("unresolved_outbound_outcome" if blocked else None)
        finally:
            await restarted_gate._admission_scheduler.close()
        if receipt_state == "pending":
            query = (
                "SELECT outcome FROM folder_command_receipts WHERE operation_key='op'"
                if domain == "folder"
                else "SELECT outcome FROM contact_note_operation_receipts WHERE operation_key='op'"
            )
            outcome = await writer.run(
                lambda conn: cast(
                    str,
                    conn.execute(query).fetchone()[0],
                )
            )
            assert outcome == "unknown"
    finally:
        await writer.aclose()


@pytest.mark.parametrize("order", ["none", "read_before", "read_after"])
async def test_contained_native_unknown_preserves_independent_read_fault(
    wire: _WireHarness, tmp_path: Path, order: str
) -> None:
    gate, _, _, _ = wire
    auth = gate.retained_auth_scope()
    assert auth is not None
    binding = NativeWriteReceiptBinding(
        "contact_note", 42, auth.dc_id, auth.auth_key_id, "another-op", "update", 8, "f" * 64, 1
    )
    writer = await SQLiteWriter.open(tmp_path / "sync.db")
    try:
        await writer.run(lambda conn: receipts.reserve_operation(conn, binding, "a" * 64, 1, expected_version="b" * 64))
        await writer.run(lambda conn: receipts.bind_attempt(conn, binding, "native"))
        ledger = gate._ensure_safety_ledger()
        request = functions.contacts.UpdateContactNoteRequest
        method = f"{request.__module__}.{request.__name__}:{request.CONSTRUCTOR_ID:08x}"
        await ledger.prepare("native", method, native_binding=binding)
        ledger.mark_claimed("native")
        if order != "none":
            await ledger.prepare("read", _canonical_wire_method(functions.updates.GetStateRequest()))
            ledger.mark_claimed("read")
        if order == "read_before":
            await ledger.complete("read", known_outcome=False)
        recovery_before = ledger.recovery_identity()
        await writer.run(lambda conn: receipts.record_wire_outcome(conn, binding, "native", "unknown"))
        assert await writer.run(lambda conn: receipts.validate_binding(conn, binding, "native"))
        await ledger.complete_native_unknown("native", binding, None)
        assert ledger.recovery_identity() == recovery_before
        await ledger.complete("native", known_outcome=True)
        assert ledger.status()["historical_unknown_request_count"] == 1
        if order == "read_after":
            await ledger.complete("read", known_outcome=False)
        assert ledger.status()["blocking_request_count"] == int(order != "none")
        assert ledger.protection_reason == ("unresolved_outbound_outcome" if order != "none" else None)
    finally:
        await writer.aclose()


@pytest.mark.parametrize("native_first", [False, True])
async def test_native_transport_fault_and_independent_read_fault_survive_both_orders(
    wire: _WireHarness, native_first: bool
) -> None:
    gate, _, _, _ = wire
    auth = gate.retained_auth_scope()
    assert auth is not None
    binding = NativeWriteReceiptBinding("contact_note", 42, auth.dc_id, auth.auth_key_id, "op", "update", 8, "f", 1)

    async def revalidate() -> Callable[[], bool]:
        return lambda: True

    scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, time.monotonic() + 10, asyncio.current_task()
    )
    native = _WireAttempt(gate._wire_issuer, "native", "native", scope, claimed=1)
    native.native_check = NativeWriteCheck(binding, b"", revalidate)
    read = _WireAttempt(gate._wire_issuer, "read", "read", scope, claimed=1)
    events = [(native, ConnectionError("transport")), (read, RuntimeError("read"))]
    if not native_first:
        events.reverse()
    for owner, error in events:
        gate.observe_error_now(owner, error)
    assert gate._wire_fault == "transport_unavailable"
    assert gate._recovery_cause is RecoveryCause.READ_UNCERTAINTY
    assert gate._native_wire_fault_owner is None
    gate.observe_error_now(read, RuntimeError("later independent read"))
    assert gate._wire_fault == "transport_unavailable"


@pytest.mark.parametrize("domain", ["folder", "contact_note"])
async def test_restart_rejects_domain_method_swap(wire: _WireHarness, domain: str) -> None:
    gate, _, _, _ = wire
    auth = gate.retained_auth_scope()
    assert auth is not None
    binding = NativeWriteReceiptBinding(
        cast(NativeWriteDomain, domain), 42, auth.dc_id, auth.auth_key_id, "op", "update", 8, "f" * 64, 1
    )
    request = (
        functions.messages.UpdateDialogFilterRequest
        if domain == "folder"
        else functions.contacts.UpdateContactNoteRequest
    )
    method = f"{request.__module__}.{request.__name__}:{request.CONSTRUCTOR_ID:08x}"
    await gate._ensure_safety_ledger().prepare("attempt", method, native_binding=binding)
    raw = gate._safety_store.load_record()
    assert raw is not None
    record = cast(dict[str, object], json.loads(raw))
    intents = cast(dict[str, dict[str, object]], record["intents"])
    receipt_owner = cast(dict[str, dict[str, object]], intents["attempt"]["native_write_receipt_owner"])
    receipt_owner["binding"]["domain"] = "contact_note" if domain == "folder" else "folder"
    await gate._safety_store.save_record(raw, json.dumps(record))
    restarted = TelegramRpcSafetyLedger(gate._safety_store, dc_id=auth.dc_id, key_id=auth.auth_key_id)
    assert restarted.protection_reason == "safety_storage_failure"
    with pytest.raises(TelegramRpcThrottled):
        restarted.require_writable()
