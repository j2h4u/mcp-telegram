"""TG-98 regressions through durable admission and real canonical callbacks."""

# pyright: reportAny=false

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon import events, functions, types
from telethon.sessions import SQLiteSession

from mcp_telegram.delivered_update import DeliveredUpdate, current_delivered_update, delivered_update_context
from mcp_telegram.delta_sync import _SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL, DeltaSyncWorker
from mcp_telegram.event_handlers import EventHandlerManager, UpdateProcessingBarrier, _EventHandlerClient, _MessageLike
from mcp_telegram.message_history.contracts import ForwardGapPage
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import _open_sync_db, open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcGate, _canonical_wire_method
from test_update_delivery import DATE, _client, _database, _message, _pending, _saved
from tests.history_enrollment_helpers import seed_full_history_enrollment


@dataclass
class _EditEvent:
    chat_id: int | None
    message: _MessageLike


def _seed(path: Path, dialog_id: int, *, hidden: bool = False) -> None:
    with closing(_open_sync_db(path)) as conn:
        kind = "user" if dialog_id > 0 else "group"
        conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES(?,?,?)", (dialog_id, kind, int(hidden)))
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(?,?,1)", (dialog_id, kind))
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id,status,delta_message_id) VALUES(?,'synced',10)", (dialog_id,)
        )
        seed_full_history_enrollment(conn, dialog_id, enabled=True)
        conn.commit()
        with write_transaction(conn):
            message = _message(5)
            message.peer_id = types.PeerUser(dialog_id) if dialog_id > 0 else types.PeerChat(-dialog_id)
            message.message = "original"
            insert_messages_with_fts(conn, [extract_message_row(dialog_id, message)])
        conn.commit()


def _manager(
    client: TelegramRpcGate,
    reader: sqlite3.Connection,
    writer: SQLiteWriter,
    barrier: UpdateProcessingBarrier | None = None,
) -> EventHandlerManager:
    manager = EventHandlerManager(
        cast(_EventHandlerClient, client),
        reader,
        asyncio.Event(),
        barrier,
        writer=writer,
        profile_detail_ttl_seconds=300,
    )
    manager.set_self_id(99)
    manager.bind_demand_sink(MagicMock())
    manager.register()
    return manager


def _body(mid: int, text: str, *, group: bool = False) -> types.Message:
    message = _message(mid)
    message.message = text
    if group:
        message.peer_id = types.PeerChat(7)
    else:
        message.edit_date = DATE
    return message


async def _admit(client: TelegramRpcGate, updates: list[object], *, include_fixture_user: bool = True) -> list[object]:
    processed: list[object] = []
    for update in updates:
        client._message_box.process_updates(
            types.UpdateShort(cast(types.TypeUpdate, update), DATE), client._mb_entity_cache, processed
        )
    assert len(processed) == len(updates)
    users: list[object] = [types.User(55, access_hash=555, first_name="Fixture")] if include_fixture_user else []
    return await client._preprocess_updates(processed, users, [types.ChatForbidden(7, "Fixture")])


async def _write(writer: SQLiteWriter, sql: str) -> None:
    def apply(conn: sqlite3.Connection) -> None:
        with write_transaction(conn):
            conn.execute(sql)

    await writer.run(apply)


async def test_hard_method_hold_keeps_unknown_sender_forward_update_local(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    _manager(client, reader, writer)
    method = _canonical_wire_method(functions.users.GetUsersRequest([types.InputUser(77, 0)]))
    ledger = client._ensure_safety_ledger()
    episodes = cast(dict[str, dict[str, object]], ledger._record["methods"])
    ledger._record_method_flood(episodes, method, None)
    calls = 0
    get_entity = client.get_entity

    async def count_get_entity(entity: types.PeerUser) -> object:
        nonlocal calls
        calls += 1
        return await get_entity(entity)

    client.get_entity = count_get_entity  # type: ignore[method-assign]
    try:
        await client.start_update_delivery(99)
        new_message = _body(11, "forwarded")
        new_message.fwd_from = types.MessageFwdHeader(date=DATE, from_id=types.PeerUser(77))
        new_update = await _admit(
            client,
            [types.UpdateNewMessage(new_message, 11, 1)],
            include_fixture_user=False,
        )
        assert _pending(path) is not None
        await client._dispatch_update(new_update[0])
        assert _pending(path) is None
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=11").fetchone() == (
            "forwarded",
        )
        assert reader.execute(
            "SELECT fwd_from_peer_id,fwd_from_name FROM message_forwards WHERE dialog_id=55 AND message_id=11"
        ).fetchone() == (77, None)

        assert reader.execute(
            "SELECT entity_id,status FROM entity_profile_refresh_state WHERE entity_id IN (55,77) ORDER BY entity_id"
        ).fetchall() == [(55, "pending"), (77, "pending")]
        assert calls == 0
        assert ledger.method_release_at(method) > 0
    finally:
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()


async def test_edit_forward_paths_use_local_names_and_profile_queue_under_hold(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _seed(path, 55)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    manager = _manager(client, reader, writer)
    method = _canonical_wire_method(functions.users.GetUsersRequest([types.InputUser(77, 0)]))
    ledger = client._ensure_safety_ledger()
    ledger._record_method_flood(cast(dict[str, dict[str, object]], ledger._record["methods"]), method, None)
    calls = 0
    get_entity = client.get_entity

    async def count_get_entity(entity: types.PeerUser) -> object:
        nonlocal calls
        calls += 1
        return await get_entity(entity)

    client.get_entity = count_get_entity  # type: ignore[method-assign]
    try:
        missing = _body(6, "missing edit")
        missing.fwd_from = types.MessageFwdHeader(date=DATE, from_id=types.PeerUser(77))
        await manager.on_message_edited(_EditEvent(chat_id=55, message=cast(_MessageLike, missing)))
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=6").fetchone() == (
            "missing edit",
        )
        assert reader.execute(
            "SELECT fwd_from_peer_id,fwd_from_name FROM message_forwards WHERE dialog_id=55 AND message_id=6"
        ).fetchone() == (77, None)

        changed = _body(5, "changed edit")
        changed.fwd_from = types.MessageFwdHeader(date=DATE, from_id=types.PeerUser(88))
        await manager.on_message_edited(_EditEvent(chat_id=55, message=cast(_MessageLike, changed)))
        assert reader.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=5").fetchone() == (
            "changed edit",
        )
        assert reader.execute(
            "SELECT entity_id,status FROM entity_profile_refresh_state WHERE entity_id IN (77,88) ORDER BY entity_id"
        ).fetchall() == [(77, "pending"), (88, "pending")]
        assert calls == 0
    finally:
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize("scenario", ["visible_new", "hidden_new", "old_edit", "group_delete"])
async def test_required_canonical_write_failure_replays_tg98_cases_1_to_4(  # noqa: PLR0915 - one recovery lifecycle
    tmp_path: Path,
    scenario: str,
) -> None:
    path = _database(tmp_path)
    _seed(path, -7, hidden=scenario == "hidden_new")
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    _manager(client, reader, writer)
    if scenario in {"visible_new", "hidden_new"}:
        update = types.UpdateNewMessage(_body(11, "new", group=True), 11, 1)
        predicate = "BEFORE INSERT ON messages WHEN NEW.message_id=11"
    elif scenario == "old_edit":
        message = _body(5, "edited", group=True)
        message.edit_date = DATE
        update = types.UpdateEditMessage(message, 11, 1)
        predicate = "BEFORE INSERT ON messages WHEN NEW.message_id=5"
    else:
        update = types.UpdateDeleteMessages([5], 11, 1)
        predicate = "BEFORE UPDATE OF is_deleted ON messages WHEN NEW.is_deleted=1"
    try:
        await client.start_update_delivery(99)
        baseline = _saved(client)
        updates = await _admit(client, [update])
        assert _pending(path) is not None
        # The fault is installed only after raw TL bytes and tokens committed.
        await _write(
            writer,
            f"CREATE TRIGGER tg101_fault {predicate} BEGIN SELECT RAISE(ABORT,'required canonical write'); END",
        )
        await client._dispatch_update(updates[0])
        assert _pending(path) is not None
        assert _saved(client) == baseline
        assert reader.execute("SELECT message_id,text,is_deleted FROM messages").fetchall() == [(5, "original", 0)]
        candidates = [row[0] for row in reader.execute(_SELECT_SYNCED_DIALOGS_FOR_DELTA_SQL)]
        assert (-7 in candidates) is (scenario != "hidden_new")

        port = None
        if scenario == "visible_new":

            class Port:
                def __init__(self) -> None:
                    self.calls: list[int] = []

                async def fetch_page(
                    self, dialog_id: int, *, after_message_id: int, should_stop: Callable[[], bool]
                ) -> ForwardGapPage:
                    assert dialog_id == -7 and not should_stop()
                    self.calls.append(after_message_id)
                    return ForwardGapPage((extract_message_row(-7, _body(11, "new", group=True)),), complete=True)

            port = Port()
            with pytest.raises(sqlite3.IntegrityError, match="required canonical write"):
                await DeltaSyncWorker(port, reader, asyncio.Event(), writer=writer).fetch_delta_slice_for_dialog(-7)
            # Fault still installed: the actual delta owner cannot repair until storage recovers.
            assert port.calls == [10]
            assert reader.execute("SELECT COUNT(*) FROM messages WHERE message_id=11").fetchone() == (0,)

        await _write(writer, "DROP TRIGGER tg101_fault")
        if scenario == "visible_new":
            assert port is not None
            await DeltaSyncWorker(port, reader, asyncio.Event(), writer=writer).fetch_delta_slice_for_dialog(-7)
            assert port.calls == [10, 10]
            assert reader.execute("SELECT delta_message_id FROM synced_dialogs").fetchone() == (11,)
            assert reader.execute("SELECT text FROM messages WHERE message_id=11").fetchone() == ("new",)
            assert _pending(path) is not None  # Delta success cannot acknowledge the retained update.
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()
        writer = await SQLiteWriter.open(path)
        reader = open_sync_db_reader(path)
        client = _client(tmp_path / "session", writer, channel=False)
        _manager(client, reader, writer)
        await client.start_update_delivery(99)
        assert _pending(path) is None
        assert _saved(client)[0][0] == 11
        expected = {
            "visible_new": [(5, "original", 0), (11, "new", 0)],
            "hidden_new": [(5, "original", 0), (11, "new", 0)],
            "old_edit": [(5, "edited", 0)],
            "group_delete": [(5, "original", 1)],
        }
        assert (
            reader.execute("SELECT message_id,text,is_deleted FROM messages ORDER BY message_id").fetchall()
            == expected[scenario]
        )
        assert reader.execute("SELECT delta_message_id FROM synced_dialogs").fetchone() == (
            11 if scenario == "visible_new" else 10,
        )
    finally:
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize("a_committed", [False, True], ids=["a_fail_b_commit", "a_lost_ack_b_commit"])
async def test_same_second_concurrent_edit_history_replay_and_legitimate_return_to_a(  # noqa: PLR0915 - one replay lifecycle
    tmp_path: Path,
    a_committed: bool,
) -> None:
    path = _database(tmp_path)
    _seed(path, 55)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    _manager(client, reader, writer)
    if a_committed:

        async def lose_ack(update: object) -> None:
            if (
                isinstance(update, types.UpdateEditMessage)
                and isinstance(update.message, types.Message)
                and update.message.message == "A"
            ):
                raise RuntimeError("required acknowledgement lost after canonical commit")

        client.add_event_handler(lose_ack, events.Raw())
    try:
        await client.start_update_delivery(99)
        updates = await _admit(
            client,
            [
                types.UpdateEditMessage(_body(5, "A"), 11, 1),
                types.UpdateEditMessage(_body(5, "B"), 12, 1),
            ],
        )
        active = client._active_delivery
        assert active is not None
        contexts = list(active.contexts.values())
        assert [context.observation_order for context in contexts] == [2, 3]
        assert contexts[0].captured_at == contexts[1].captured_at
        if not a_committed:
            await _write(
                writer,
                "CREATE TRIGGER tg101_fault BEFORE INSERT ON messages WHEN NEW.text='A' "
                "BEGIN SELECT RAISE(ABORT,'A canonical write failed'); END",
            )
        await asyncio.gather(*(client._dispatch_update(update) for update in updates))
        assert reader.execute("SELECT text FROM messages").fetchone() == ("B",)
        history = [row[0] for row in reader.execute("SELECT old_text FROM message_versions ORDER BY version")]
        assert history == (["original", "A"] if a_committed else ["original"])
        assert _pending(path) is not None
        if not a_committed:
            await _write(writer, "DROP TRIGGER tg101_fault")
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()
        writer = await SQLiteWriter.open(path)
        reader = open_sync_db_reader(path)
        client = _client(tmp_path / "session", writer, channel=False)
        _manager(client, reader, writer)
        await client.start_update_delivery(99)
        assert _pending(path) is None
        assert reader.execute("SELECT text FROM messages").fetchone() == ("B",)
        assert [row[0] for row in reader.execute("SELECT old_text FROM message_versions ORDER BY version")] == history
        later = await _admit(client, [types.UpdateEditMessage(_body(5, "A"), 13, 1)])
        await client._dispatch_update(later[0])
        assert reader.execute("SELECT text FROM messages").fetchone() == ("A",)
        assert [row[0] for row in reader.execute("SELECT old_text FROM message_versions ORDER BY version")] == [
            *history,
            "B",
        ]
        assert _pending(path) is None
    finally:
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()


async def test_cancel_after_real_writer_admission_waits_for_canonical_commit(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    entered = threading.Event()
    release = threading.Event()
    delivered = DeliveredUpdate(2, DATE.timestamp(), 2)

    def commit(conn: sqlite3.Connection) -> None:
        assert current_delivered_update() is delivered
        entered.set()
        assert release.wait(timeout=5)
        with write_transaction(conn):
            insert_messages_with_fts(conn, [extract_message_row(55, _message(11))])

    async def deliver() -> None:
        with delivered_update_context(delivered):
            await writer.run(commit)

    task = asyncio.create_task(deliver())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT text FROM messages WHERE dialog_id=55 AND message_id=11").fetchone() == (
                "fixture",
            )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await writer.aclose()


async def test_closed_handler_barrier_cancellation_retains_raw_update_for_restart(tmp_path: Path) -> None:
    path = _database(tmp_path)
    _seed(path, -7)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    barrier = UpdateProcessingBarrier(closed=True)
    _manager(client, reader, writer, barrier)
    try:
        await client.start_update_delivery(99)
        baseline = _saved(client)
        updates = await _admit(client, [types.UpdateNewMessage(_body(11, "new", group=True), 11, 1)])
        task = asyncio.create_task(client._dispatch_update(updates[0]))
        await asyncio.sleep(0)
        assert not task.done()
        barrier.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert _pending(path) is not None
        assert _saved(client) == baseline
        assert reader.execute("SELECT COUNT(*) FROM messages WHERE message_id=11").fetchone() == (0,)
        cast(SQLiteSession, client.session).close()
        client = _client(tmp_path / "session", writer, channel=False)
        _manager(client, reader, writer)
        await client.start_update_delivery(99)
        assert _pending(path) is None
        assert reader.execute("SELECT text FROM messages WHERE message_id=11").fetchone() == ("new",)
    finally:
        cast(SQLiteSession, client.session).close()
        reader.close()
        await writer.aclose()
