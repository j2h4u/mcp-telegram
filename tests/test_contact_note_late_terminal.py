"""Late wire outcomes schedule new durable reconciliation after earlier work completes."""

# pyright: reportAny=false
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Never, cast

from telethon.tl import types

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.contact_notes.commands import ContactNoteCommandOwner, _parse
from mcp_telegram.contact_notes.domain import NoteWriteAuthority
from mcp_telegram.contact_notes.receipts import bind_attempt, record_wire_outcome, reserve_operation
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.contracts import UserReference
from mcp_telegram.image_authorization import ScopeCell
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from tests.test_contact_note_background_refinement import _ScopeClient
from tests.test_entity_profile_full_user_pair import _pair_service


async def test_late_terminal_schedules_new_generation_and_replay_is_local(tmp_path: Path) -> None:  # noqa: PLR0915
    path = tmp_path / "profile.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99)
    cell = ScopeCell()
    assert cell.publish(scope)
    client = _ScopeClient()
    client.capture_image_scope = cell.capture
    client.claim_image_scope = cell.claim
    service = cast(DaemonEntityInfoService, _pair_service(reader, client, enabled=True))
    service._deps = replace(service._deps, writer=writer)
    authority = NoteWriteAuthority(
        writer,
        lambda: scope,
        lambda entity: UserReference(entity, 123),
        capture_scope=cell.capture,
        claim_scope=cell.claim,
    )
    authority.bind_reconciliation(service.request_note_reconciliation)
    request = {
        "dialog_id": 7,
        "action": "update",
        "expected_version": "v" * 64,
        "operation_key": "late",
        "text": "new",
    }
    binding = NativeWriteReceiptBinding("contact_note", 42, 2, 99, "late", "update", 7, "b" * 64, 1)

    def seed(conn: sqlite3.Connection) -> None:
        from mcp_telegram.entity_profile.repository import EntityProfileRepository

        repo = EntityProfileRepository(conn, section_ttl_seconds=300)
        repo.save_core({"id": 7, "type": "user", "name": "target"}, now=100)
        repo.mark_pending(7, now=100)
        with write_transaction(conn):
            conn.execute(
                "UPDATE entity_profile_refresh_state SET next_section='pinned_stories', acquisition_cursor=0 WHERE entity_id=7"
            )
            conn.execute(
                "UPDATE entity_detail_sections SET status='not_applicable',retry_at=NULL "
                "WHERE entity_id=7 AND section='pinned_stories'"
            )
        reserve_operation(conn, binding, _parse(request, 42).fingerprint(42), 100, expected_version="v" * 64)
        bind_attempt(conn, binding, "attempt")
        cursor = repo.next_due_refresh(now=100, entity_id=7)
        assert cursor is not None and repo.complete_unchanged_section(cursor, now=100)

    async def no_rpc(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("replay must remain local")

    try:
        await writer.run(seed)
        before = reader.execute(
            "SELECT status,generation FROM entity_profile_refresh_state WHERE entity_id=7"
        ).fetchone()
        assert before[0] == "complete"
        with authority.watch(7):
            token = cell.capture()
            assert token is not None and cell.claim(token)
            change = cell.begin_change()
            assert not cell.claim(token)
            cell.end_change(change)
            assert cell.publish(scope)
            await writer.run(lambda conn: record_wire_outcome(conn, binding, "attempt", "unknown"))
            await authority.notify_terminal(binding, "unknown")
        after = reader.execute(
            "SELECT status,generation,next_section FROM entity_profile_refresh_state WHERE entity_id=7"
        ).fetchone()
        assert tuple(after) == ("pending", before[1] + 1, "full_profile")
        owner = ContactNoteCommandOwner(
            no_rpc,
            reader,
            writer=writer,
            authority=authority,
            identity=lambda: scope,
            resolve_input_user=lambda entity: types.InputUser(entity, 123),
            acquire_command_profile=no_rpc,
            acquire_config=no_rpc,
            request_reconciliation=service.request_note_reconciliation,
        )
        assert await owner.execute(request) == {"outcome": "unknown", "verification": "pending"}
        assert tuple(
            reader.execute(
                "SELECT status,generation,next_section FROM entity_profile_refresh_state WHERE entity_id=7"
            ).fetchone()
        ) == tuple(after)
    finally:
        await service.shutdown()
        reader.close()
        await writer.aclose()
