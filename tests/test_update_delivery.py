"""Offline protocol/canonical recovery at the real Telethon and SQLite seams."""

# pyright: reportAny=false

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
import telethon
from telethon import TelegramClient, events, types
from telethon._updates import ChannelState, SessionState
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon.sessions import SQLiteSession

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.delivered_update import (
    DeliveredUpdate,
    current_delivered_update,
    delivered_observed_at,
    delivered_update_context,
    fail_delivered_update,
)
from mcp_telegram.observation_order import delivered_observation_order
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcBudget, TelegramRpcGate
from mcp_telegram.update_delivery import UpdateDeliveryRepository

DATE = datetime(2026, 10, 1, tzinfo=UTC)


@dataclass
class _Circuit:
    open: bool = False

    def detail(self) -> str:
        return "offline"


def _client(path: Path, writer: SQLiteWriter, *, channel: bool = True) -> TelegramRpcGate:
    client = TelegramRpcGate(
        SQLiteSession(str(path)),
        12345,
        "offline-test",
        rpc_budget=TelegramRpcBudget(max_calls_per_period=0, period_seconds=60),
        circuit_status=_Circuit,
        fallback_wait_seconds=60,
        cooldown_buffer_seconds=1.0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
    )
    client._mb_entity_cache.set_self_user(99, False, 999)
    channels = [ChannelState(123, 20)] if channel else []
    client._message_box.load(SessionState(99, 0, False, 10, 3, int(DATE.timestamp()), 0, None), channels)
    if channel:
        entity = types.ChannelForbidden(123, 456, "fixture")
        cast(SQLiteSession, client.session).process_entities(
            types.contacts.ResolvedPeer(types.PeerChannel(123), [entity], [])
        )
        client._mb_entity_cache.extend([], [entity])
    client.configure_update_delivery(UpdateDeliveryRepository(writer))
    return client


def _message(mid: int, *, channel: bool = False) -> types.Message:
    return types.Message(
        id=mid,
        peer_id=types.PeerChannel(123) if channel else types.PeerUser(55),
        date=DATE,
        message="fixture",
        from_id=types.PeerUser(55),
    )


def _saved(client: TelegramRpcGate) -> dict[int, tuple[int, int, int, datetime]]:
    session = SQLiteSession(cast(SQLiteSession, client.session).filename)
    try:
        return {
            entity: (state.pts, state.qts, state.seq, cast(datetime, state.date))
            for entity, state in session.get_update_states()
        }
    finally:
        session.close()


def _pending(path: Path) -> bytes | None:
    with closing(sqlite3.connect(path)) as conn:
        return cast(bytes | None, conn.execute("SELECT pending FROM update_delivery").fetchone()[0])


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    return path


async def _mixed(client: TelegramRpcGate) -> list[object]:
    client._message_box.try_begin_get_diff(ENTRY_ACCOUNT, "offline fixture")
    difference = types.updates.Difference(
        new_messages=[_message(11), _message(12)],
        new_encrypted_messages=[],
        other_updates=[types.UpdateNewChannelMessage(_message(13, channel=True), 21, 1)],
        chats=[],
        users=[],
        state=types.updates.State(12, 4, DATE + timedelta(seconds=10), 1, 0),  # pyright: ignore[reportArgumentType] - vendor State annotation names its module
    )
    result = client._message_box.apply_difference(difference, client._mb_entity_cache)
    assert result is not None
    updates, users, chats = result
    return await client._preprocess_updates(list[object](updates), list[object](users), list[object](chats))


@pytest.mark.parametrize("failure", [False, True])
async def test_whole_snapshot_waits_for_all_callbacks_and_survives_other_session_flush(
    tmp_path: Path,
    failure: bool,
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    release = asyncio.Event()
    completed: list[int] = []

    async def handler(update: types.UpdateNewMessage) -> None:
        mid = update.message.id
        if mid == 11:
            await release.wait()
            if failure:
                fail_delivered_update()  # Models a required exception caught by its domain owner.
                return
        completed.append(mid)

    client.add_event_handler(handler, events.Raw())
    try:
        await client.start_update_delivery(99)
        baseline = _saved(client)
        updates = await _mixed(client)
        assert _pending(path) is not None  # Durable before the first callback runs.
        tasks = [asyncio.create_task(client._dispatch_update(update)) for update in updates]
        await asyncio.sleep(0)
        assert set(completed) == {12, 13}
        await client._save_states_and_entities()
        cast(SQLiteSession, client.session).set_dc(2, "offline.invalid", 443)
        cast(
            SQLiteSession, client.session
        ).save()  # DC/auth/download/keepalive commits cannot publish the mutable head.
        assert _saved(client) == baseline
        release.set()
        await asyncio.gather(*tasks)
        assert (_pending(path) is not None) is failure
        assert _saved(client) == (
            baseline
            if failure
            else {
                0: (12, 4, 1, DATE + timedelta(seconds=10)),
                123: (21, 0, 0, _saved(client)[123][3]),
            }
        )
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


async def test_required_exception_vendor_swallows_retains_obligation(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)

    async def failure(_update: object) -> None:
        raise ValueError("offline injected required callback failure")

    client.add_event_handler(failure, events.Raw())
    try:
        await client.start_update_delivery(99)
        baseline = _saved(client)
        for update in await _mixed(client):
            await client._dispatch_update(update)
        assert _pending(path) is not None
        assert _saved(client) == baseline
        with pytest.raises(RuntimeError, match="required Telegram update"):
            await client.wait_update_delivery_failure()
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


@pytest.mark.parametrize("crash_after_effect", [False, True])
async def test_pending_restart_keeps_original_tokens_time_and_first_channel_entities(
    tmp_path: Path,
    crash_after_effect: bool,
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer, channel=False)
    seen: list[tuple[int, float, int]] = []

    async def handler(_update: object) -> None:
        context = current_delivered_update()
        assert context is not None
        seen.append((context.observation_order, context.captured_at, context.reaction_sequence))
        if len(seen) == 1:
            fail_delivered_update()

    client.add_event_handler(handler, events.Raw())
    reopened: TelegramRpcGate | None = None
    try:
        await client.start_update_delivery(99)
        updates: list[object] = []
        item = types.UpdateNewChannelMessage(_message(11, channel=True), 21, 1)
        client._message_box.process_updates(types.UpdateShort(item, DATE), client._mb_entity_cache, updates)
        channel = types.ChannelForbidden(123, 456, "fixture")
        await client._preprocess_updates(updates, [], [channel])
        if crash_after_effect:
            await client._dispatch_update(updates[0])
        active = client._active_delivery
        assert active is not None
        original = next(iter(active.contexts.values()))
        cast(SQLiteSession, client.session).close()
        reopened = _client(tmp_path / "session", writer, channel=False)

        async def recovered(_update: object) -> None:
            context = current_delivered_update()
            assert context is not None
            seen.append((context.observation_order, context.captured_at, context.reaction_sequence))

        reopened.add_event_handler(recovered, events.Raw())
        await reopened.start_update_delivery(99)
        assert seen[-1] == (original.observation_order, original.captured_at, original.reaction_sequence)
        assert _pending(path) is None
        assert _saved(reopened)[123][0] == 21
        assert reopened._message_box.session_state()[1] == {123: 21}
        cached = reopened._mb_entity_cache.get(123)
        assert cached is not None and cached.hash == 456
    finally:
        if reopened is not None:
            cast(SQLiteSession, reopened.session).close()
        else:
            cast(SQLiteSession, client.session).close()
        await writer.aclose()


async def test_completion_before_session_mirror_restores_without_fresh_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    try:
        await client.start_update_delivery(99)
        updates = await _mixed(client)

        async def mirror_failure() -> None:
            raise OSError("offline crash before checkpoint mirror")

        monkeypatch.setattr(client, "_save_states_and_entities", mirror_failure)
        for update in updates[:-1]:
            await client._dispatch_update(update)
        with pytest.raises(OSError, match="offline crash"):
            await client._dispatch_update(updates[-1])
        assert _pending(path) is None
        assert _saved(client)[0][0] == 10
        cast(SQLiteSession, client.session).close()
        reopened = _client(tmp_path / "session", writer)
        calls: list[object] = []

        async def unexpected(update: object) -> None:
            calls.append(update)

        reopened.add_event_handler(unexpected, events.Raw())
        await reopened.start_update_delivery(99)
        assert calls == []
        assert _saved(reopened)[0][:3] == (12, 4, 1)
        assert reopened._message_box.session_state()[0]["pts"] == 12
        cast(SQLiteSession, reopened.session).close()
    finally:
        await writer.aclose()


@pytest.mark.parametrize("corruption", ["format", "payload", "payload_version", "tokens", "positions", "account"])
async def test_startup_rejects_corruption_and_wrong_account_without_erasing_pending(
    tmp_path: Path,
    corruption: str,
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    try:
        await client.start_update_delivery(99)
        await _mixed(client)
        cast(SQLiteSession, client.session).close()

        def corrupt(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                if corruption == "format":
                    conn.execute("UPDATE update_delivery SET format=99")
                elif corruption == "payload":
                    conn.execute("UPDATE update_delivery SET pending=x'00'")
                elif corruption == "payload_version":
                    payload = json.loads(conn.execute("SELECT pending FROM update_delivery").fetchone()[0])
                    payload["version"] = 2
                    conn.execute("UPDATE update_delivery SET pending=?", (json.dumps(payload).encode("utf-8"),))
                elif corruption == "tokens":
                    conn.execute("DELETE FROM update_delivery_tokens WHERE position=0")
                elif corruption == "positions":
                    conn.execute("UPDATE update_delivery_tokens SET position=99 WHERE position=0")

        await writer.run(corrupt)
        before = _pending(path)
        reopened = _client(tmp_path / "session", writer)
        with pytest.raises(RuntimeError):
            await reopened.start_update_delivery(55 if corruption == "account" else 99)
        assert not reopened._delivery_ready.is_set()
        assert _pending(path) == before
        cast(SQLiteSession, reopened.session).close()
    finally:
        await writer.aclose()


async def test_vendor_intake_waits_for_startup_and_closed_admission_does_not_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    entered = asyncio.Event()

    async def vendor(_client: TelegramClient) -> None:
        entered.set()

    monkeypatch.setattr(TelegramClient, "_update_loop", vendor)
    task = asyncio.create_task(client._update_loop())
    try:
        await asyncio.sleep(0)
        assert not entered.is_set()
        await client._save_states_and_entities()
        cast(SQLiteSession, client.session).save()
        assert _saved(client) == {}
        with pytest.raises(RuntimeError, match="admission is closed"):
            await client._preprocess_updates([], [], [])
        await client.start_update_delivery(99)
        await task
        assert entered.is_set()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


def test_managed_vendor_compatibility_covers_preprocessing_and_all_checkpoint_writers() -> None:
    assert telethon.__version__ == "1.45.0"
    update_loop = inspect.getsource(TelegramClient._update_loop)
    assert update_loop.count("await self._preprocess_updates(") == 3
    writer = inspect.getsource(TelegramClient._save_states_and_entities)
    assert writer.count("self.session.set_update_state(") == 2
    assert "self._message_box.session_state()" in writer
    assert "await self._save_states_and_entities()" in inspect.getsource(TelegramClient._disconnect_coro)


async def test_retained_context_reaches_writer_but_does_not_leak_to_background_task(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path))
    try:
        with delivered_update_context(DeliveredUpdate(999, 123.0, 888)):
            assert delivered_observed_at() == 123
            assert await writer.run(delivered_observation_order) == 999

            async def background() -> int:
                assert current_delivered_update() is None
                assert delivered_observed_at() > 123
                return await writer.run(delivered_observation_order)

            assert await asyncio.create_task(background()) != 999
    finally:
        await writer.aclose()


@pytest.mark.integration
async def test_hard_process_exit_after_admission_replays_all_retained_updates(tmp_path: Path) -> None:
    path = _database(tmp_path)
    script = """
import asyncio, os, sys
from pathlib import Path
sys.path.insert(0, 'tests')
from test_update_delivery import _client, _mixed
from mcp_telegram.sqlite_writer import SQLiteWriter
async def main():
    root = Path(sys.argv[1])
    writer = await SQLiteWriter.open(root / 'sync.db')
    client = _client(root / 'session', writer)
    await client.start_update_delivery(99)
    await _mixed(client)
    os._exit(0)
asyncio.run(main())
"""
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert _pending(path) is not None
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    recovered: list[int] = []

    async def callback(update: types.UpdateNewMessage) -> None:
        recovered.append(update.message.id)

    client.add_event_handler(callback, events.Raw())
    try:
        await client.start_update_delivery(99)
        assert set(recovered) == {11, 12, 13}
        assert _pending(path) is None
        assert _saved(client)[0][0] == 12
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


async def test_channel_difference_admission_uses_captured_consumed_snapshot(tmp_path: Path) -> None:
    writer = await SQLiteWriter.open(_database(tmp_path))
    client = _client(tmp_path / "session", writer)
    try:
        await client.start_update_delivery(99)
        client._message_box.try_begin_get_diff(123, "offline fixture")
        request = client._message_box.get_channel_difference(client._mb_entity_cache)
        assert request is not None
        difference = types.updates.ChannelDifference(
            pts=21,
            new_messages=[_message(13, channel=True)],
            other_updates=[],
            chats=[types.ChannelForbidden(123, 456, "fixture")],
            users=[],
            final=True,
        )
        result = client._message_box.apply_channel_difference(request, difference, client._mb_entity_cache)
        assert result is not None
        updates, users, chats = result
        admitted = await client._preprocess_updates(list[object](updates), list[object](users), list[object](chats))
        assert _saved(client)[123][0] == 20
        # A later mutable directory initialization must not alter this envelope's snapshot.
        client._message_box.try_set_channel_state(123, 999)
        await asyncio.gather(*(client._dispatch_update(update) for update in admitted))
        assert _saved(client)[123][0] == 21
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


async def test_one_pending_batch_and_waiting_snapshot_captured_before_prior_settles(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    release = asyncio.Event()

    async def hold(update: types.UpdateNewMessage) -> None:
        if update.message.id == 11:
            await release.wait()

    client.add_event_handler(hold, events.Raw())
    try:
        await client.start_update_delivery(99)
        first = await _mixed(client)
        pending = _pending(path)
        dispatches = [asyncio.create_task(client._dispatch_update(update)) for update in first]
        client._message_box.try_set_channel_state(456, 30)
        next_update = types.UpdateNewMessage(_message(14), 13, 1)
        waiting = asyncio.create_task(client._preprocess_updates([next_update], [], []))
        await asyncio.sleep(0)
        assert not waiting.done()
        assert _pending(path) == pending
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM update_delivery").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM update_delivery_tokens").fetchone()[0] == 3
        # Independent directory work can initialize a later channel while preprocessing waits.
        client._message_box.try_set_channel_state(789, 40)
        # Supply the required session entity for the channel captured by the waiting batch.
        cast(SQLiteSession, client.session).process_entities(
            types.contacts.ResolvedPeer(
                types.PeerChannel(456),
                [types.ChannelForbidden(456, 777, "fixture")],
                [],
            )
        )
        release.set()
        await asyncio.gather(*dispatches)
        second = await waiting
        await client._dispatch_update(second[0])
        assert set(_saved(client)) == {0, 123, 456}
        assert _pending(path) is None
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM update_delivery_tokens").fetchone()[0] == 0
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


async def test_real_disconnect_cancels_callbacks_without_advancing_checkpoint(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    entered = asyncio.Event()

    async def hold(_update: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    client.add_event_handler(hold, events.Raw())
    try:
        await client.start_update_delivery(99)
        baseline = _saved(client)
        updates = await _mixed(client)
        tasks = [asyncio.create_task(client._dispatch_update(update)) for update in updates]
        client._event_handler_tasks.update(tasks)
        await entered.wait()
        await client._disconnect_coro()
        assert all(task.cancelled() for task in tasks)
        assert _pending(path) is not None
        assert _saved(client) == baseline
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()


async def test_missing_channel_entities_fails_closed_and_min_hash_never_replaces_full_hash(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer)
    try:
        await client.start_update_delivery(99)
        partial = types.Channel(123, "min", types.ChatPhotoEmpty(), DATE, min=True, access_hash=777)
        assert client._capture_delivery_snapshot([partial]).channel_hashes[123] == 456
        baseline = _saved(client)
        updates: list[object] = []
        item = types.UpdateNewChannelMessage(_message(11, channel=True), 21, 1)
        item.message.peer_id = types.PeerChannel(789)
        client._message_box.process_updates(types.UpdateShort(item, DATE), client._mb_entity_cache, updates)
        await client._preprocess_updates(updates, [], [])
        with pytest.raises(ValueError, match="Could not find"):
            await client._dispatch_update(updates[0])
        assert _pending(path) is not None
        await client._save_states_and_entities()
        cast(SQLiteSession, client.session).save()
        assert _saved(client) == baseline
    finally:
        cast(SQLiteSession, client.session).close()
        await writer.aclose()
