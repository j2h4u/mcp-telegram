"""Decoded notification staging preserves complete original facts."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon import types
from telethon.tl.core.tlmessage import TLMessage

from mcp_telegram import telegram_update_ring as module
from mcp_telegram.telegram_update_ring import TelegramUpdateRing
from tests.test_telegram_rpc_wire_integration import wire as wire


def _envelope() -> types.Updates:
    return types.Updates([types.UpdateUser(55)], [types.User(55, first_name="Before")], [], datetime.now(UTC), 1)


def _records(ring: TelegramUpdateRing):
    return ring.peek_batch(max_count=32, max_bytes=module.MAX_ENTRY_BYTES)


@pytest.mark.parametrize("kind", ["combined", "short", "empty"])
def test_complete_decoded_snapshot(kind: str) -> None:
    now = datetime.now(UTC)
    user = types.User(55, first_name="Before")
    chat = types.ChatForbidden(7, "Group")
    if kind == "combined":
        item = types.UpdatesCombined([types.UpdateUser(55)], [user], [chat], now, 1, 2)
    elif kind == "short":
        item = types.UpdateShort(types.UpdateUser(55), now)
    else:
        item = types.Updates([], [user], [chat], now, 1)
    expected = item.to_dict()
    ring = TelegramUpdateRing()
    ring.capture(item, source="push")
    user.first_name = "After"
    payload = cast(dict[str, object], json.loads(_records(ring)[0].payload_json))
    assert payload["_"] == expected["_"]
    if kind != "short":
        assert cast(list[dict[str, object]], payload["users"])[0]["first_name"] == "Before"
        assert cast(list[dict[str, object]], payload["chats"])[0]["title"] == "Group"


def test_bounded_staging_and_ack(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = TelegramUpdateRing()
    ring.capture(_envelope(), source="push")
    first = _records(ring)
    ring.capture(_envelope(), source="push")
    ring.ack_batch(first)
    assert [r.sequence for r in _records(ring)] == [2]
    monkeypatch.setattr(module, "MAX_RETAINED_BYTES", _records(ring)[0].payload_bytes)
    ring.capture(_envelope(), source="push")
    assert ring.counters()["evicted_capacity"] == 1
    monkeypatch.setattr(module, "MAX_ENTRY_BYTES", 1)
    ring.capture(_envelope(), source="push")
    ring.capture(object(), source="push")
    assert ring.counters()["rejected_oversize"] == 1
    assert ring.counters()["serialization_failures"] == 1


async def test_sender_captures_push_rpc_and_library_synthetic(wire: object) -> None:
    from tests.test_telegram_rpc_wire_integration import _WireHarness

    gate, sender, _, _ = cast(_WireHarness, wire)
    await sender._handle_update(cast(TLMessage, SimpleNamespace(obj=_envelope())))
    await sender._handle_update(cast(TLMessage, SimpleNamespace(obj=types.UpdatesTooLong())))
    sender._store_own_updates(types.messages.InvitedUsers(_envelope(), []))
    sender._store_own_updates(types.UpdatesTooLong())
    sender._store_own_updates(types.messages.AffectedMessages(5, 1))
    gate._updates_queue.put_nowait(types.UpdatesTooLong())
    gate._updates_queue.put_nowait(gate._message_box.wakeup)
    sender._store_own_updates(types.User(55))
    entries = _records(gate.update_ring())
    assert [r.source for r in entries] == [
        "push",
        "push",
        "rpc_response",
        "rpc_response",
        "library_synthetic",
        "library_synthetic",
    ]


async def test_existing_observation_owner_flushes_decoded_envelope(tmp_path: Path) -> None:
    from mcp_telegram.config import RuntimeObservationConfig
    from mcp_telegram.runtime_observations import RuntimeObservationSink
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_db import ensure_sync_schema

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    ring = TelegramUpdateRing()
    sink.attach_update_ring(ring)
    try:
        ring.capture(_envelope(), source="push")
        ring.request_verified_account(42)
        await sink._update_ring_lifecycle()
        await sink.aclose()
        with closing(sqlite3.connect(path)) as conn:
            rows = cast(
                list[tuple[str, str]], conn.execute("SELECT source, payload_json FROM raw_telegram_updates").fetchall()
            )
            assert len(rows) == 1 and rows[0][0] == "push"
            payload = cast(dict[str, object], json.loads(rows[0][1]))
            assert cast(list[dict[str, object]], payload["users"])[0]["first_name"] == "Before"
            assert conn.execute(
                "SELECT value FROM daemon_state WHERE key='raw_telegram_updates.run_open'"
            ).fetchone() == ("0",)
    finally:
        await writer.aclose()


async def test_failed_batch_keeps_staging_until_committed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp_telegram import runtime_observations
    from mcp_telegram.config import RuntimeObservationConfig
    from mcp_telegram.runtime_observations import RuntimeObservationSink
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_db import ensure_sync_schema

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    ring = TelegramUpdateRing()
    sink.attach_update_ring(ring)
    try:
        ring.request_verified_account(42)
        await sink._update_ring_lifecycle()
        actual_write = runtime_observations.write_update_batch

        def fail(_conn: sqlite3.Connection, **_kwargs: object) -> None:
            raise sqlite3.OperationalError("injected")

        monkeypatch.setattr(runtime_observations, "write_update_batch", fail)
        ring.capture(_envelope(), source="push")
        assert not await sink._flush_updates()
        assert len(_records(ring)) == 1
        monkeypatch.setattr(runtime_observations, "write_update_batch", actual_write)
        assert await sink._flush_updates()
        assert _records(ring) == ()
        await sink.aclose()
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM raw_telegram_updates").fetchone() == (1,)
    finally:
        await writer.aclose()


def test_inflight_is_pinned_across_overflow_and_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    ring = TelegramUpdateRing()
    ring.capture(_envelope(), source="push")
    batch = _records(ring)
    monkeypatch.setattr(module, "MAX_RETAINED_BYTES", batch[0].payload_bytes)
    ring.capture(_envelope(), source="push")
    assert ring.counters()["evicted_capacity"] == 1
    assert ring.counters()["pending_records"] == 1
    ring.reset("account_changed")
    ring.capture(_envelope(), source="push")
    assert ring.counters()["evicted_capacity"] == 2
    ring.release_batch()
    ring.capture(_envelope(), source="push")
    assert ring.counters()["pending_records"] == 1


async def test_verified_identity_does_not_wait_for_diagnostic_writer(tmp_path: Path, wire: object) -> None:
    import asyncio
    import threading

    from mcp_telegram.config import RuntimeObservationConfig
    from mcp_telegram.runtime_observations import RuntimeObservationSink
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from tests.test_telegram_rpc_wire_integration import _WireHarness

    gate, _, _, _ = cast(_WireHarness, wire)
    path = tmp_path / "sync.db"
    writer = await SQLiteWriter.open(path)
    entered = threading.Event()
    release = threading.Event()

    def hold(_conn: sqlite3.Connection) -> None:
        entered.set()
        release.wait()

    blocked = asyncio.create_task(writer.run(hold))
    await asyncio.to_thread(entered.wait)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    sink.attach_update_ring(gate.update_ring())
    try:
        await asyncio.wait_for(gate.verify_startup_account(42), timeout=2)
        assert gate.update_ring().generation is None
        gate.update_ring().capture(_envelope(), source="push")
    finally:
        release.set()
        await blocked
        await sink.aclose()
        await writer.aclose()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM raw_telegram_updates").fetchone() == (1,)


def test_stale_bind_and_logout_do_not_activate_generation() -> None:
    ring = TelegramUpdateRing()
    ring.request_verified_account(42)
    old_revision = ring.request_revision
    ring.request_verified_account(42)
    assert ring.request_revision == old_revision
    ring.request_logout()
    ring.request_verified_account(42)
    ring.complete_bind(42, old_revision, 1)
    assert ring.generation is None
    assert ring.completed_logout_serial < ring.logout_serial
    ring.complete_bind(42, ring.request_revision, 2)
    assert ring.generation is None
    ring.completed_logout_serial = ring.logout_serial
    ring.complete_bind(42, ring.request_revision, 2)
    assert ring.generation == 2


async def test_stale_inflight_bind_drains_logout_then_rebind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio
    import threading

    from mcp_telegram import runtime_observations
    from mcp_telegram.config import RuntimeObservationConfig
    from mcp_telegram.runtime_observations import RuntimeObservationSink
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.sync_db import ensure_sync_schema

    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    writer = await SQLiteWriter.open(path)
    sink = RuntimeObservationSink(path, writer=writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig())
    ring = TelegramUpdateRing()
    sink.attach_update_ring(ring)
    entered, release = threading.Event(), threading.Event()
    original = runtime_observations.bind_verified_account

    def blocked(conn: sqlite3.Connection, account_id: int) -> tuple[int, bool]:
        entered.set()
        release.wait()
        return original(conn, account_id)

    monkeypatch.setattr(runtime_observations, "bind_verified_account", blocked)
    try:
        ring.request_verified_account(42)
        assert await asyncio.to_thread(entered.wait, 2)
        ring.request_logout()
        ring.request_verified_account(42)
        ring.capture(_envelope(), source="push")
        release.set()
        await sink.aclose()
        assert ring.generation is not None
        assert ring.completed_logout_serial == ring.logout_serial
        with closing(sqlite3.connect(path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM raw_telegram_updates").fetchone() == (1,)
    finally:
        release.set()
        await writer.aclose()
