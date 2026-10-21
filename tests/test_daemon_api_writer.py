"""Runtime API writes must work with a truly read-only loop connection."""

import asyncio
import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from account_trace_fixtures import open_trace_db, seed_dialog, seed_entity, seed_synced_dialog
from mcp_telegram.sqlite_writer import SQLiteWriter
from tests.test_account_trace_enrichment import fake_message
from tests.test_daemon_api import make_server


@pytest.mark.asyncio
async def test_api_enrollment_entities_and_trace_use_owner(tmp_path: Path) -> None:
    seed = open_trace_db(tmp_path)
    seed_entity(seed, entity_id=7)
    seed_dialog(seed, dialog_id=222, name="Group", dialog_type="Supergroup")
    seed_synced_dialog(seed, dialog_id=222)
    seed.commit()
    seed.close()
    path = tmp_path / "sync.db"
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    writer = await SQLiteWriter.open(path)
    client = AsyncMock()
    client.return_value = SimpleNamespace(users=[SimpleNamespace(id=8, first_name="Bob", username="bob", bot=False)])
    threads: list[int] = []
    loop_thread = threading.get_ident()

    def hydration(conn: sqlite3.Connection, _dialog_id: int, _now: int) -> None:
        threads.append(threading.get_ident())
        assert conn.in_transaction

    server = make_server(reader, client, writer=writer, hydration_requester=hydration)
    server.bind_demand_sink(MagicMock())
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM entities")
        reader.rollback()
        assert (await server._mark_dialog_for_sync({"dialog_id": 222}))["ok"]
        assert threads and threads[0] != loop_thread
        assert (await server._upsert_entities({"entities": [{"id": 9, "type": "User", "name": "Nine"}]}))["ok"]
        assert reader.execute("SELECT name FROM entities WHERE id=9").fetchone()[0] == "Nine"
        service = server._trace_service()
        assert (await service._resolve_trace_username("bob"))["account_id"] == 8
        result = await service._trace_enrich_one_candidate(
            target_user_id=7,
            candidate={"dialog_id": 222, "topic_id": None, "strategy": "hidden"},
            max_per_dialog=1,
            deadline_ms=1000,
            deadline_at=time.monotonic() + 1,
        )
        assert result["status"] == "unsupported"
        assert reader.execute("SELECT status FROM trace_coverage_fragments").fetchone()[0] == "unsupported"

        async def messages():
            yield fake_message(message_id=1, text="owner persisted")

        client.iter_messages = MagicMock(side_effect=lambda *_args, **_kwargs: messages())
        result = await service._trace_enrich_one_candidate(
            target_user_id=7,
            candidate={"dialog_id": 222, "topic_id": None, "strategy": "author_search"},
            max_per_dialog=2,
            deadline_ms=1000,
            deadline_at=time.monotonic() + 1,
        )
        assert result["status"] == "complete"
        assert result["messages_persisted"] == 1
        assert (
            reader.execute("SELECT text FROM messages WHERE dialog_id=222 AND message_id=1").fetchone()[0]
            == "owner persisted"
        )
        assert (
            int(
                cast(
                    str,
                    reader.execute(
                        "SELECT value FROM daemon_state WHERE key='canonical_observation_sequence'"
                    ).fetchone()[0],
                )
            )
            >= 1
        )
    finally:
        await server.shutdown()
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
async def test_api_owner_wait_keeps_loop_responsive_and_rolls_back_enrollment(tmp_path: Path) -> None:
    seed = open_trace_db(tmp_path)
    seed.close()
    path = tmp_path / "sync.db"
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    writer = await SQLiteWriter.open(path)
    entered = threading.Event()
    release = threading.Event()

    def fail_hydration(conn: sqlite3.Connection, _dialog_id: int, _now: int) -> None:
        entered.set()
        release.wait(timeout=5)
        raise ValueError("hydration rejected")

    server = make_server(reader, writer=writer, hydration_requester=fail_hydration)
    try:
        task = asyncio.create_task(server._mark_dialog_for_sync({"dialog_id": 222}))
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.001)
        assert entered.is_set()
        assert not task.done()
        release.set()
        with pytest.raises(ValueError, match="hydration rejected"):
            await task
        assert reader.execute("SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=222").fetchone()[0] == 0
    finally:
        release.set()
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
async def test_shutdown_closes_idle_clients_and_settles_accepted_write(tmp_path: Path) -> None:  # noqa: PLR0915
    seed = open_trace_db(tmp_path)
    seed.close()
    path = tmp_path / "sync.db"
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    writer = await SQLiteWriter.open(path)
    entered = threading.Event()
    release = threading.Event()

    def hydrate(conn: sqlite3.Connection, _dialog_id: int, _now: int) -> None:
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release accepted write")
        conn.execute("INSERT INTO daemon_state(key,value) VALUES ('shutdown_receipt','committed')")

    api = make_server(reader, writer=writer, hydration_requester=hydrate)
    api.bind_demand_sink(MagicMock())
    socket_path = tmp_path / "api.sock"
    listener = await asyncio.start_unix_server(api.handle_client, path=socket_path)
    idle_reader, idle_client = await asyncio.open_unix_connection(socket_path)
    _, active_client = await asyncio.open_unix_connection(socket_path)
    shutdown: asyncio.Task[None] | None = None
    try:
        active_client.write(json.dumps({"method": "mark_dialog_for_sync", "dialog_id": 222}).encode() + b"\n")
        await active_client.drain()
        async with asyncio.timeout(2):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        assert len(api._client_handlers) == 2
        listener.close()
        shutdown = asyncio.create_task(api.shutdown_clients())
        async with asyncio.timeout(2):
            assert await idle_reader.read() == b""
        assert not shutdown.done()
        assert reader.execute("SELECT value FROM daemon_state WHERE key='shutdown_receipt'").fetchone() is None
        release.set()
        await asyncio.wait_for(shutdown, timeout=2)
        await api.shutdown()
        await asyncio.wait_for(listener.wait_closed(), timeout=2)
        assert not api._client_handlers
        assert (
            reader.execute("SELECT value FROM daemon_state WHERE key='shutdown_receipt'").fetchone()[0] == "committed"
        )
        assert reader.execute("SELECT COUNT(*) FROM synced_dialogs WHERE dialog_id=222").fetchone()[0] == 1
    finally:
        release.set()
        if shutdown is not None:
            await shutdown
        listener.close()
        await api.shutdown_clients()
        idle_client.close()
        active_client.close()
        await asyncio.gather(idle_client.wait_closed(), active_client.wait_closed(), return_exceptions=True)
        await listener.wait_closed()
        await writer.aclose()
        reader.close()


@pytest.mark.asyncio
async def test_shutdown_rejects_handler_that_arrives_after_closing(tmp_path: Path) -> None:
    seed = open_trace_db(tmp_path)
    seed.close()
    path = tmp_path / "sync.db"
    reader = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    writer = await SQLiteWriter.open(path)
    api = make_server(reader, writer=writer)
    socket_path = tmp_path / "api.sock"
    listener = await asyncio.start_unix_server(api.handle_client, path=socket_path)
    try:
        await api.shutdown_clients()
        client_reader, client_writer = await asyncio.open_unix_connection(socket_path)
        try:
            async with asyncio.timeout(2):
                assert await client_reader.read() == b""
            assert not api._client_handlers
        finally:
            client_writer.close()
            await client_writer.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()
        await writer.aclose()
        reader.close()
