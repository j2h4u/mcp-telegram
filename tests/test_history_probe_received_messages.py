"""A limit-one history probe retains authorized body without implying completeness."""

import asyncio
import sqlite3
import threading
import time
from collections.abc import Awaitable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types
from telethon.errors import ChannelPrivateError
from telethon.helpers import TotalList

from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.delta_sync import AccessProbePolicy, DeltaAccessProbeDemandAdapter, DeltaSyncWorker
from mcp_telegram.image_authorization import ScopeCell, ScopeToken
from mcp_telegram.message_history.ports import ForwardGapPagePort, FullHistoryPagePort
from mcp_telegram.message_history.telegram_adapter import TelethonHistoryAccessProbe
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.sync_worker import FullSyncWorker
from mcp_telegram.telegram_demand import RpcAttemptBudget


class _Client:
    def __init__(self, message: types.Message, *, revoke: bool) -> None:
        self.cell = ScopeCell()
        self.scope = TelegramAuthScope(AUTH_SCOPE_VERSION, 123, 2, 99)
        assert self.cell.publish(self.scope)
        self.message = message
        self.revoke = revoke
        self.calls = 0

    def capture_image_scope(self) -> ScopeToken | None:
        return self.cell.capture()

    def claim_image_scope(self, token: ScopeToken) -> bool:
        return self.cell.claim(token)

    async def get_messages(self, *, entity: int, limit: int) -> object:
        assert entity == 55 and limit == 1
        self.calls += 1
        result = TotalList([self.message])
        result.total = 42
        if self.revoke:
            change = self.cell.begin_change()
            self.cell.end_change(change)
            assert self.cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 124, 2, 99))
            change = self.cell.begin_change()
            self.cell.end_change(change)
            assert self.cell.publish(self.scope)
        return result


@pytest.mark.parametrize("wrong_peer", [False, True])
@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("revoke", [False, True])
async def test_probe_message_scope_and_existing_history_fences(
    tmp_path: Path, recovery: bool, revoke: bool, wrong_peer: bool
) -> None:
    path = tmp_path / "probe.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
            conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (55,'user')")
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',0)")
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id,status,sync_progress,delta_message_id,access_lost_at) VALUES (55,?,7,9,?)",
                ("access_lost" if recovery else "synced", 1 if recovery else None),
            )
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")

    await writer.run(seed)
    reader = open_sync_db_reader(path)
    message = types.Message(
        id=20,
        peer_id=types.PeerUser(99 if wrong_peer else 55),
        from_id=types.PeerUser(55),
        date=datetime.fromtimestamp(100, UTC),
        message="probe fixture",
    )
    client = _Client(message, revoke=revoke)
    probe = TelethonHistoryAccessProbe(client)
    try:
        if recovery:
            worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), reader, asyncio.Event(), writer=writer)
            adapter = DeltaAccessProbeDemandAdapter(
                worker, AccessProbePolicy(enabled=True, cooldown_seconds=1), probe, account_id=123
            )
            status = adapter.status(time.time())
            assert status is not None and status.selected is not None
            captured = probe.capture_scope()
            assert captured is not None and captured.scope.account_id == 123
            assert adapter._validate_selected(status.selected, time.time()) is None
            assert await writer.run(lambda conn: adapter._probe_revision(conn, 55)) == status.selected.revision
            outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
            assert client.calls == 1, (
                status,
                outcome,
                reader.execute("SELECT account_id FROM dialog_directory_state").fetchone(),
            )
        else:
            worker = FullSyncWorker(
                cast(FullHistoryPagePort, object()), reader, asyncio.Event(), writer=writer, total_messages_probe=probe
            )
            await worker.repair_one_total_messages(expected_dialog_id=55, profile_owner_account_id=123)
        assert client.calls == 1
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=20").fetchone() == (
            None if revoke or wrong_peer else ("probe fixture",)
        )
        assert reader.execute(
            "SELECT sync_progress,delta_message_id FROM synced_dialogs WHERE dialog_id=55"
        ).fetchone() == (7, 9)
        assert reader.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'fixture'").fetchone() == (
            0 if revoke or wrong_peer else 1,
        )
    finally:
        reader.close()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT count(*) FROM messages WHERE dialog_id=55 AND message_id=20").fetchone() == (
            0 if revoke or wrong_peer else 1,
        )


@pytest.mark.parametrize("recovery", [False, True])
async def test_stale_probe_metadata_does_not_discard_eligible_message(tmp_path: Path, recovery: bool) -> None:
    path = tmp_path / "stale.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
            conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (55,'user')")
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',0)")
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id,status,sync_progress,delta_message_id,access_lost_at) VALUES (55,?,7,9,?)",
                ("access_lost" if recovery else "synced", 1 if recovery else None),
            )
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")

    await writer.run(seed)
    reader = open_sync_db_reader(path)
    message = types.Message(
        id=20,
        peer_id=types.PeerUser(55),
        from_id=types.PeerUser(55),
        date=datetime.fromtimestamp(100, UTC),
        message="stale metadata fixture",
    )

    class ChangedClient(_Client):
        async def get_messages(self, *, entity: int, limit: int) -> object:
            result = await super().get_messages(entity=entity, limit=limit)

            def supersede(conn: sqlite3.Connection) -> None:
                with write_transaction(conn):
                    if recovery:
                        conn.execute("UPDATE synced_dialogs SET status='synced' WHERE dialog_id=55")
                    else:
                        conn.execute("UPDATE synced_dialogs SET total_messages=77 WHERE dialog_id=55")

            await writer.run(supersede)
            return result

    client = ChangedClient(message, revoke=False)
    probe = TelethonHistoryAccessProbe(client)
    try:
        if recovery:
            worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), reader, asyncio.Event(), writer=writer)
            adapter = DeltaAccessProbeDemandAdapter(
                worker, AccessProbePolicy(enabled=True, cooldown_seconds=1), probe, account_id=123
            )
            status = adapter.status(time.time())
            assert status is not None and status.selected is not None
            await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
        else:
            worker = FullSyncWorker(
                cast(FullHistoryPagePort, object()), reader, asyncio.Event(), writer=writer, total_messages_probe=probe
            )
            await worker.repair_one_total_messages(expected_dialog_id=55, profile_owner_account_id=123)
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=20").fetchone() == (
            "stale metadata fixture",
        )
        assert reader.execute(
            "SELECT sync_progress,delta_message_id FROM synced_dialogs WHERE dialog_id=55"
        ).fetchone() == (7, 9)
        if not recovery:
            assert reader.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id=55").fetchone() == (77,)
    finally:
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize("recovery", [False, True])
async def test_probe_scope_revoked_while_writer_is_busy(tmp_path: Path, recovery: bool) -> None:  # noqa: PLR0915
    path = tmp_path / "writer-wait.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
            conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (55,'user')")
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',0)")
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id,status,sync_progress,delta_message_id,access_lost_at) VALUES (55,?,7,9,?)",
                ("access_lost" if recovery else "synced", 1 if recovery else None),
            )
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")

    await writer.run(seed)
    reader = open_sync_db_reader(path)
    message = types.Message(
        id=20, peer_id=types.PeerUser(55), date=datetime.fromtimestamp(100, UTC), message="waiting fixture"
    )
    started = threading.Event()
    release = threading.Event()
    rpc_finished = asyncio.Event()
    blockers: list[asyncio.Task[None]] = []

    class BusyClient(_Client):
        async def get_messages(self, *, entity: int, limit: int) -> object:
            result = await super().get_messages(entity=entity, limit=limit)

            def hold(_conn: sqlite3.Connection) -> None:
                started.set()
                assert release.wait(10)

            blockers.append(asyncio.create_task(writer.run(hold)))
            assert await asyncio.to_thread(started.wait, 5)
            rpc_finished.set()
            return result

    client = BusyClient(message, revoke=False)
    probe = TelethonHistoryAccessProbe(client)
    task: asyncio.Task[None] | None = None

    async def finish(operation: Awaitable[object]) -> None:
        await operation

    try:
        if recovery:
            worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), reader, asyncio.Event(), writer=writer)
            adapter = DeltaAccessProbeDemandAdapter(
                worker, AccessProbePolicy(enabled=True, cooldown_seconds=1), probe, account_id=123
            )
            status = adapter.status(time.time())
            assert status is not None and status.selected is not None
            task = asyncio.create_task(finish(adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))))
        else:
            worker = FullSyncWorker(
                cast(FullHistoryPagePort, object()), reader, asyncio.Event(), writer=writer, total_messages_probe=probe
            )
            task = asyncio.create_task(
                finish(worker.repair_one_total_messages(expected_dialog_id=55, profile_owner_account_id=123))
            )
        await asyncio.wait_for(rpc_finished.wait(), 5)
        await asyncio.sleep(0)
        change = client.cell.begin_change()
        client.cell.end_change(change)
        assert client.cell.publish(client.scope)
        release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.gather(*blockers)
        assert reader.execute("SELECT count(*) FROM messages WHERE dialog_id=55").fetchone() == (0,)
        assert reader.execute(
            "SELECT sync_progress,delta_message_id FROM synced_dialogs WHERE dialog_id=55"
        ).fetchone() == (7, 9)
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*blockers, return_exceptions=True)
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize("recovery", [False, True])
@pytest.mark.parametrize("change_at", ["none", "rpc", "writer"])
async def test_probe_error_is_fenced_to_original_scope(tmp_path: Path, recovery: bool, change_at: str) -> None:  # noqa: PLR0914, PLR0915
    path = tmp_path / "error.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)

    def seed(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=123 WHERE singleton=1")
            conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES (55,'user')")
            conn.execute("INSERT INTO entities(id,type,updated_at) VALUES (55,'user',0)")
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id,status,sync_progress,delta_message_id,access_lost_at) VALUES (55,?,7,9,?)",
                ("access_lost" if recovery else "synced", 1 if recovery else None),
            )
            conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,updated_at) VALUES (55,1,0)")

    await writer.run(seed)
    reader = open_sync_db_reader(path)
    started = threading.Event()
    release = threading.Event()
    failing = asyncio.Event()
    blockers: list[asyncio.Task[None]] = []
    message = types.Message(id=20, peer_id=types.PeerUser(55), date=datetime.fromtimestamp(100, UTC), message="fixture")

    class ErrorClient(_Client):
        async def get_messages(self, *, entity: int, limit: int) -> object:
            assert entity == 55 and limit == 1
            self.calls += 1
            if change_at == "rpc":
                change = self.cell.begin_change()
                self.cell.end_change(change)
                assert self.cell.publish(self.scope)
            if change_at == "writer":

                def hold(_conn: sqlite3.Connection) -> None:
                    started.set()
                    assert release.wait(10)

                blockers.append(asyncio.create_task(writer.run(hold)))
                assert await asyncio.to_thread(started.wait, 5)
                failing.set()
            raise ChannelPrivateError(request=None)

    client = ErrorClient(message, revoke=False)
    probe = TelethonHistoryAccessProbe(client)
    task: asyncio.Task[None] | None = None

    async def finish(operation: Awaitable[object]) -> None:
        await operation

    try:
        if recovery:
            worker = DeltaSyncWorker(cast(ForwardGapPagePort, object()), reader, asyncio.Event(), writer=writer)
            adapter = DeltaAccessProbeDemandAdapter(
                worker, AccessProbePolicy(enabled=True, cooldown_seconds=1), probe, account_id=123
            )
            status = adapter.status(time.time())
            assert status is not None and status.selected is not None
            task = asyncio.create_task(finish(adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))))
        else:
            worker = FullSyncWorker(
                cast(FullHistoryPagePort, object()), reader, asyncio.Event(), writer=writer, total_messages_probe=probe
            )
            task = asyncio.create_task(
                finish(worker.repair_one_total_messages(expected_dialog_id=55, profile_owner_account_id=123))
            )
        if change_at == "writer":
            await asyncio.wait_for(failing.wait(), 5)
            await asyncio.sleep(0)
            change = client.cell.begin_change()
            client.cell.end_change(change)
            assert client.cell.publish(client.scope)
            release.set()
        await asyncio.wait_for(task, 5)
        await asyncio.gather(*blockers)
        row = cast(
            "tuple[object, ...] | None",
            reader.execute("SELECT status,access_next_revalidate_at FROM synced_dialogs WHERE dialog_id=55").fetchone(),
        )
        assert row is not None
        if change_at == "none":
            assert row[0] == "access_lost"
            if recovery:
                assert row[1] is not None
        else:
            assert row == ("access_lost" if recovery else "synced", None)
            assert reader.execute(
                "SELECT count(*) FROM dialog_fact_observations WHERE dialog_id=55 AND field IN ('access_state','access_recovery_witness')"
            ).fetchone() == (0,)
    finally:
        release.set()
        if task is not None and not task.done():
            await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(*blockers, return_exceptions=True)
        reader.close()
        await writer.aclose()
