import sqlite3
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import types
from telethon.tl.types import InputPeerUser

from mcp_telegram.auth_scope import TelegramAuthScope
from mcp_telegram.folders.commands import (
    FolderCommandClient,
    FolderCommandOwner,
    FolderReceiptClient,
    FolderReceiptOwner,
    folder_catalog_version,
)
from mcp_telegram.folders.contracts import FolderRule, FolderRuleObservation
from mcp_telegram.folders.refresh import FolderRefreshResult
from mcp_telegram.folders.sqlite_repository import SQLiteFolderSnapshotRepository, read_folder_rules
from mcp_telegram.folders.telegram_adapter import folder_update_from_native
from mcp_telegram.image_authorization import ScopeToken
from mcp_telegram.native_write_fence import current_native_write_check
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_savepoint


def test_verification_status_selects_first_unexcluded_receipt() -> None:
    from types import SimpleNamespace

    scope = TelegramAuthScope(1, 1, 2, 3)
    rows = [("first", "a", 1, 12.0, 20.0), ("second", "b", 0, None, None)]
    reader = SimpleNamespace(execute=lambda *_args: SimpleNamespace(fetchall=lambda: rows))
    owner = object.__new__(FolderCommandOwner)
    owner._client = cast(FolderCommandClient, SimpleNamespace(retained_auth_scope=lambda: scope))
    owner._reader_conn = cast(sqlite3.Connection, reader)
    first = owner.verification_status(10.0, frozenset())
    assert first is not None and first.selected is not None and first.release_at == 12.0
    second = owner.verification_status(10.0, frozenset({first.selected}))
    assert second is not None and second.selected is not None and second.release_at == 10.0
    assert owner.verification_status(10.0, frozenset({first.selected, second.selected})) is None
    owner._client = cast(FolderCommandClient, SimpleNamespace(retained_auth_scope=lambda: None))
    assert owner.verification_status(10.0, frozenset()) is None


@pytest.mark.asyncio
async def test_owner_uses_one_attempt_and_duplicate_reads_receipt(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    with write_savepoint(conn):
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=7,observation_started_at=10,observation_completed_at=11 WHERE singleton=1"
        )
    repository = SQLiteFolderSnapshotRepository(conn)
    repository.project_observation(
        FolderRuleObservation((FolderRule(4, "Old", included_ids=(42,)),), "initial", 100), completed_at=100
    )
    version = folder_catalog_version(conn, 1)
    assert version is not None

    class Writer:
        async def run(self, callback: object) -> object:
            return callback(conn)  # type: ignore[operator]

    writer = cast(SQLiteWriter, Writer())
    scope = TelegramAuthScope(1, 1, 2, 3)
    from mcp_telegram.image_authorization import ScopeCell

    scope_cell = ScopeCell()
    scope_cell.publish(scope)

    class Client:
        attempts = 0

        def retained_auth_scope(self) -> TelegramAuthScope:
            return scope

        def retained_draft_peer(self, dialog_id: int) -> object:
            return InputPeerUser(dialog_id, 99)

        def capture_image_scope(self):
            return scope_cell.capture()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return scope_cell.claim(token)

        async def __call__(self, request: object, **_kwargs: object) -> object:
            self.attempts += 1
            check = current_native_write_check()
            assert check is not None and (await check.revalidate())()
            assert (await receipts.preclaim(check.binding))()
            assert await receipts.verify_request(check.binding, request)
            await receipts.bind_attempt(check.binding, "one")
            native_rule = folder_update_from_native(types.UpdateDialogFilter(request.id, request.filter)).rule  # type: ignore[attr-defined]
            assert native_rule is not None
            repository.project_observation(FolderRuleObservation((native_rule,), "changed", 101), completed_at=101)
            await receipts.record_wire_outcome(check.binding, "one", "acknowledged")
            return True

    class Worker:
        calls = 0

        async def force_refresh(self, budget: object = None) -> FolderRefreshResult:
            self.calls += 1
            return FolderRefreshResult(0, 7, "initial", 100, observation_order=100 + self.calls)

    client, worker = Client(), Worker()
    receipts = FolderReceiptOwner(writer, client)
    owner = FolderCommandOwner(client, writer, worker, conn, receipts, offer_verification=lambda: None)  # type: ignore[arg-type]
    request = {
        "action": "update",
        "operation_key": "once",
        "folder_id": 4,
        "expected_version": version,
        "add_dialog_ids": [43],
    }
    first = await owner.execute(request)
    assert first["outcome"] == "acknowledged"
    assert first["verification"] == "matched"
    assert read_folder_rules(conn)[0].included_ids == (42, 43)
    second = await owner.execute(request)
    assert second["outcome"] == "acknowledged" and client.attempts == 1
    assert (await owner.execute({**request, "add_dialog_ids": [44]}))["error"] == "operation_key_conflict"
    conn.close()


@pytest.mark.asyncio
async def test_authoritative_permit_invalidates_and_cannot_be_rearmed(tmp_path: Path) -> None:
    import sqlite3
    import time

    from mcp_telegram.folders.command_receipts import insert_operation
    from mcp_telegram.folders.intake import invalidate_folder_intake, marker
    from mcp_telegram.native_write_fence import FolderWriteRejectedError, NativeWriteReceiptBinding

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path, isolation_level=None)
    with write_savepoint(conn):
        conn.execute(
            "UPDATE dialog_directory_publication SET account_id=1,generation=7,observation_started_at=10,observation_completed_at=11 WHERE singleton=1"
        )
    SQLiteFolderSnapshotRepository(conn).project_observation(
        FolderRuleObservation((FolderRule(4, "Old", included_ids=(42,)),), "initial", 100), completed_at=100
    )
    version = folder_catalog_version(conn, 1)
    assert version is not None

    class Writer:
        async def run(self, callback: object) -> object:
            return callback(conn)  # type: ignore[operator]

    class Client:
        def retained_auth_scope(self) -> TelegramAuthScope:
            return TelegramAuthScope(1, 1, 2, 3)

        def retained_draft_peer(self, dialog_id: int) -> object:
            return InputPeerUser(dialog_id, 99)

    writer = cast(SQLiteWriter, Writer())
    scope = TelegramAuthScope(1, 1, 2, 3)
    binding = NativeWriteReceiptBinding("folder", 1, 2, 3, "op", "update", 4, "hash", 1)
    insert_operation(
        conn,
        binding,
        operation_fingerprint="op-hash",
        expected_version=version,
        before="before",
        intended="after",
        now=100,
    )
    owner = FolderReceiptOwner(writer, cast(FolderReceiptClient, Client()))
    await owner.arm(binding, scope, version, marker(), time.monotonic() + 10)
    claim = await owner.preclaim(binding)
    assert claim()
    invalidate_folder_intake()
    assert not claim()
    owner.disarm(binding)
    with pytest.raises(FolderWriteRejectedError):
        await owner.arm(binding, scope, version, marker(), time.monotonic() + 10)
    with pytest.raises(FolderWriteRejectedError):
        await owner.preclaim(binding)
    conn.close()
