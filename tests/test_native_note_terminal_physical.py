"""A late native outcome re-enqueues profile evidence after an early refresh."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import replace
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.contact_notes.commands import ContactNoteCommandOwner
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contact_notes.state import read_note
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import CommandProfileAcquisition
from mcp_telegram.entity_profile.repository import EntityProfileRepository
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcGate, _AdmissionAwareSender, _WireAttempt
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.test_contact_note_background_refinement import _ScopeClient
from tests.test_entity_profile_full_user_pair import _pair_service
from tests.test_native_write_physical import physical_note as physical_note
from tests.test_telegram_transport import _Writer


@pytest.mark.parametrize(
    "mode", ["cancel_ack", "cancel_unknown", "timeout_ack", "timeout_unknown", "cancel_before_claim"]
)
async def test_late_note_terminal_rearms_existing_durable_profile_owner(  # noqa: PLR0914, PLR0915
    physical_note: tuple[object, ...], mode: str
) -> None:
    gate_obj, sender_obj, connection_obj, sink_obj, writer_obj, reader_obj, authority_obj = physical_note
    gate = cast(TelegramRpcGate, gate_obj)
    sender = cast(GuardedMTProtoSender, sender_obj)
    connection = cast(GuardedConnectionTcpFull, connection_obj)
    sink = cast(_Writer, sink_obj)
    writer = cast(SQLiteWriter, writer_obj)
    reader = cast(sqlite3.Connection, reader_obj)
    authority = cast(NoteWriteAuthority, authority_obj)
    client = _ScopeClient()
    client.capture_image_scope = gate.capture_image_scope
    client.claim_image_scope = gate.claim_image_scope
    service = cast(DaemonEntityInfoService, _pair_service(reader, client, enabled=True))
    service._deps = replace(service._deps, writer=writer, full_user_auth_scope=gate.retained_auth_scope)
    authority.bind_reconciliation(service.request_note_reconciliation)
    peer = types.InputUser(43, 55)
    reached, release = asyncio.Event(), asyncio.Event()
    pumps: list[asyncio.Task[None]] = []

    def seed(conn: sqlite3.Connection) -> None:
        repo = EntityProfileRepository(conn, section_ttl_seconds=300)
        repo.save_core({"id": 43, "type": "user", "name": "target"}, now=100)
        repo.mark_pending(43, now=100)
        with write_transaction(conn):
            conn.execute(
                "UPDATE entity_profile_refresh_state SET next_section='pinned_stories', acquisition_cursor=0 WHERE entity_id=43"
            )
            conn.execute(
                "UPDATE entity_detail_sections SET status='not_applicable',retry_at=NULL WHERE entity_id=43 AND section='pinned_stories'"
            )
        cursor = repo.next_due_refresh(now=100, entity_id=43)
        assert cursor is not None and repo.complete_unchanged_section(cursor, now=100)

    await writer.run(seed)

    async def pump(request: object) -> None:
        state = await sender._send_queue.next_state()
        if mode == "cancel_before_claim" and isinstance(request, functions.contacts.UpdateContactNoteRequest):
            reached.set()
            await release.wait()
        try:
            await sender._prepare_state(state)
        except asyncio.CancelledError:
            return
        packet = connection._send_queue.get_nowait()
        assert sender.write_packet(packet, connection._send)
        if not isinstance(request, functions.contacts.UpdateContactNoteRequest):
            state.future.set_result(True)
            return
        wire_owner = cast(_WireAttempt, packet.state.owner)
        if mode.startswith("timeout"):
            assert wire_owner.response_timer is not None
            wire_owner.response_timer.reschedule(time.monotonic() + 0.1)
        reached.set()
        await release.wait()
        if mode.endswith("unknown"):
            state.future.set_exception(RuntimeError("lost response"))
        else:
            state.future.set_result(True)

    async def call(request: object) -> object:
        scope = current_rpc_scope()
        pumps.append(asyncio.create_task(pump(request)))
        return await _AdmissionAwareSender(gate, scope).send(request)

    async def acquire(_target: int) -> CommandProfileAcquisition:
        await call(functions.users.GetFullUserRequest(peer))
        return CommandProfileAcquisition(True, True, 1)

    async def config(_hash: int) -> tuple[int, int | None]:
        return 0, 128

    owner = ContactNoteCommandOwner(
        call,
        reader,
        writer=writer,
        authority=authority,
        identity=gate.retained_auth_scope,
        resolve_input_user=lambda _target: peer,
        acquire_command_profile=acquire,
        acquire_config=config,
        request_reconciliation=service.request_note_reconciliation,
    )
    request = {
        "dialog_id": 43,
        "action": "create",
        "expected_version": read_note(reader, 42, 43)["expected_version"],
        "operation_key": "late",
        "text": "new",
    }
    task = asyncio.create_task(owner.execute(request))
    try:
        await asyncio.wait_for(reached.wait(), 15)
        if mode.startswith("cancel"):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result["outcome"] == "pending"
        if mode == "cancel_before_claim":
            release.set()
            await asyncio.gather(*pumps)
            await asyncio.gather(*sender._observing, return_exceptions=True)
            receipt = cast(
                tuple[str, str | None] | None,
                reader.execute(
                    "SELECT outcome,attempt_id FROM contact_note_operation_receipts WHERE account_id=42 AND operation_key='late'"
                ).fetchone(),
            )
            assert receipt == ("rejected", None) and len(sink.frames) == 1
            assert gate.account_safety_status() is None
            return
        before = cast(
            tuple[str, int] | None,
            reader.execute("SELECT status,generation FROM entity_profile_refresh_state WHERE entity_id=43").fetchone(),
        )
        assert before is not None and before[0] == "pending"

        def complete_early(conn: sqlite3.Connection) -> None:
            repo = EntityProfileRepository(conn, section_ttl_seconds=300)
            with write_transaction(conn):
                conn.execute(
                    "UPDATE entity_profile_refresh_state SET next_section='pinned_stories', acquisition_cursor=0 WHERE entity_id=43"
                )
                conn.execute(
                    "UPDATE entity_detail_sections SET status='not_applicable',retry_at=NULL WHERE entity_id=43 AND section='pinned_stories'"
                )
            cursor = repo.next_due_refresh(now=int(time.time()) + 1, entity_id=43)
            assert cursor is not None and repo.complete_unchanged_section(cursor, now=int(time.time()) + 1)

        await writer.run(complete_early)
        release.set()
        await asyncio.gather(*pumps)
        await asyncio.gather(*sender._observing, return_exceptions=True)
        after = cast(
            tuple[str, int, str] | None,
            reader.execute(
                "SELECT status,generation,next_section FROM entity_profile_refresh_state WHERE entity_id=43"
            ).fetchone(),
        )
        assert after == ("pending", before[1] + 1, "full_profile")
        expected = "unknown" if mode.endswith("unknown") else "acknowledged"
        row = cast(
            tuple[str] | None,
            reader.execute(
                "SELECT outcome FROM contact_note_operation_receipts WHERE account_id=42 AND operation_key='late'"
            ).fetchone(),
        )
        assert row is not None and row[0] == expected
        frames = len(sink.frames)
        repeated = await owner.execute(request)
        assert repeated["outcome"] == expected and len(sink.frames) == frames
    finally:
        release.set()
        await asyncio.gather(*pumps, return_exceptions=True)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.shutdown()
