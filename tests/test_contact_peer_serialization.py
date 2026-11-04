"""Contact delete and note commands serialize on the same account and peer."""

import asyncio
from pathlib import Path

from telethon.tl import types

from mcp_telegram.contact_notes.commands import ContactNoteCommandOwner
from mcp_telegram.contacts import receipts
from mcp_telegram.contacts.commands import ContactCommandOwner
from mcp_telegram.contacts.contracts import ContactProfileAcquisition
from mcp_telegram.contacts.guard import ContactPeerGuard
from mcp_telegram.entity_profile.contracts import CommandProfileAcquisition
from mcp_telegram.native_write_fence import NativeWriteReceiptBinding
from tests.test_contact_command_recovery import _fixture, _request


async def test_contact_delete_blocks_note_preflight_on_same_peer(tmp_path: Path) -> None:
    async with _fixture(tmp_path / "shared-guard.db") as fixture:
        guard = ContactPeerGuard()
        entered, release, note_started, note_preflight = (asyncio.Event() for _ in range(4))
        writes: list[object] = []

        async def client(request: object) -> object:
            writes.append(request)
            raise AssertionError("unconfirmed commands must not write")

        async def acquire_contact(_entity: int) -> ContactProfileAcquisition:
            entered.set()
            await release.wait()
            return ContactProfileAcquisition(False)

        async def acquire_note(_entity: int) -> CommandProfileAcquisition:
            return CommandProfileAcquisition(False, False)

        async def note_config(_hash: int) -> tuple[int, int | None]:
            note_preflight.set()
            return 0, 128

        async def reconcile(_entity: int) -> bool:
            return True

        contact = ContactCommandOwner(
            client,
            fixture.reader,
            writer=fixture.writer,
            authority=fixture.authority,
            guard=guard,
            identity=lambda: fixture.scope,
            resolve=lambda entity: types.InputUser(entity, 42),
            acquire=acquire_contact,
            reconcile=reconcile,
        )
        note = ContactNoteCommandOwner(
            client,
            fixture.reader,
            writer=fixture.writer,
            authority=fixture.note_authority,
            identity=lambda: fixture.scope,
            resolve_input_user=lambda entity: types.InputUser(entity, 42),
            acquire_command_profile=acquire_note,
            acquire_config=note_config,
            request_reconciliation=reconcile,
            peer_guard=guard,
        )

        async def run_note() -> dict[str, object]:
            note_started.set()
            return await note.execute(
                {
                    "dialog_id": 2,
                    "action": "create",
                    "operation_key": "note",
                    "expected_version": "0" * 64,
                    "text": "Example",
                }
            )

        delete_task = asyncio.create_task(contact.execute(_request(fixture.reader, "delete")))
        note_task: asyncio.Task[dict[str, object]] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            note_task = asyncio.create_task(run_note())
            await asyncio.wait_for(note_started.wait(), 5)
            await asyncio.sleep(0)
            assert not note_preflight.is_set() and not note_task.done()
            release.set()
            assert (await delete_task)["error"] == "contact_state_changed"
            assert (await note_task)["error"] == "contact_required"
            assert note_preflight.is_set() and writes == []
        finally:
            release.set()
            await asyncio.gather(delete_task, *([note_task] if note_task is not None else []), return_exceptions=True)


async def test_unknown_contact_outcome_refuses_new_delete_before_preflight(tmp_path: Path) -> None:
    async with _fixture(tmp_path / "unknown.db") as fixture:
        request = _request(fixture.reader, "new-delete")
        version = str(request["expected_version"])
        binding = NativeWriteReceiptBinding("contact", 1, 2, 3, "old-delete", "delete", 2, "b" * 64, 1)
        await fixture.writer.run(
            lambda db: receipts.reserve_operation(
                db, binding, "a" * 64, 100, expected_version=version, verification_fingerprint="f" * 64
            )
        )
        await fixture.writer.run(lambda db: receipts.bind_attempt(db, binding, "claimed"))
        await fixture.writer.run(lambda db: receipts.record_wire_outcome(db, binding, "claimed", "unknown"))

        async def forbidden(_value: object) -> object:
            raise AssertionError("quarantined contact reached preflight or dispatch")

        async def forbidden_acquire(_entity: int) -> ContactProfileAcquisition:
            raise AssertionError("quarantined contact reached preflight")

        async def forbidden_reconcile(_entity: int) -> bool:
            raise AssertionError("quarantined contact reached reconciliation")

        owner = ContactCommandOwner(
            forbidden,
            fixture.reader,
            writer=fixture.writer,
            authority=fixture.authority,
            guard=ContactPeerGuard(),
            identity=lambda: fixture.scope,
            resolve=lambda entity: types.InputUser(entity, 42),
            acquire=forbidden_acquire,
            reconcile=forbidden_reconcile,
        )
        result = await owner.execute(request)
        assert result["error"] == "contact_outcome_unresolved"
        assert fixture.reader.execute("SELECT count(*) FROM contact_operation_receipts").fetchone() == (1,)
