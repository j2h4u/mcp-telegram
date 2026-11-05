"""Profile invalidation survives vendor sequence handling and durable delivery."""

import asyncio
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import cast

import pytest
from telethon import types
from telethon._updates.messagebox import ENTRY_ACCOUNT
from telethon.sessions import SQLiteSession

from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import open_sync_db_reader
from mcp_telegram.sync_transactions import write_transaction
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_telegram_update_fairness_delivery import _Delivery, _stop
from tests.test_telegram_update_fairness_delivery import delivery as delivery
from tests.test_update_delivery import _client
from tests.test_update_delivery_handlers import _manager


@pytest.fixture
def active_lease(delivery: _Delivery, tmp_path: Path) -> Iterator[None]:
    with telegram_session_lease(tmp_path) as lease:
        delivery.wire[0].bind_session_lease(lease)
        yield


async def _seed_completed_profile(delivery: _Delivery) -> int:
    def seed(conn: sqlite3.Connection) -> int:
        with write_transaction(conn):
            conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
            conn.execute("UPDATE entities SET name='Retained' WHERE id=55")
            conn.execute(
                "INSERT INTO entity_details(entity_id,detail_json,fetched_at,profile_owner_account_id) VALUES(55,?,1,42)",
                (json.dumps({"schema": 1, "id": 55, "type": "user", "name": "Retained", "about": "retained"}),),
            )
            conn.execute(
                "INSERT INTO entity_profile_refresh_state(entity_id,status,updated_at,next_section,generation) "
                "VALUES(55,'complete',1,'full_profile',1)"
            )
            order = allocate_observation_order(conn)
            conn.execute(
                "INSERT INTO entity_profile_field_observations(entity_id,field,owner_account_id,observation_order,observed_at,source) "
                "VALUES(55,'__invalidation__:user',42,?,1,'update')",
                (order,),
            )
            return order

    return await delivery.writer.run(seed)


def _envelope(now: datetime, seq: int) -> types.Updates:
    return types.Updates(
        updates=[types.UpdateUser(55)],
        users=[types.User(55, access_hash=555, first_name="Stale sidecar")],
        chats=[],
        date=now,
        seq=seq,
    )


async def _wait_pending(delivery: _Delivery, old_order: int) -> int:
    async with asyncio.timeout(3):
        while True:
            row = cast(
                tuple[str, int, int] | None,
                delivery.reader.execute(
                    "SELECT s.status,s.generation,o.observation_order FROM entity_profile_refresh_state s "
                    "JOIN entity_profile_field_observations o ON o.entity_id=s.entity_id "
                    "WHERE s.entity_id=55 AND o.field='__invalidation__:user'"
                ).fetchone(),
            )
            if row is not None and row[0] == "pending" and row[2] > old_order:
                return cast(int, row[2])
            await asyncio.sleep(0)


@pytest.mark.parametrize("sequence", ["duplicate", "gap"])
async def test_rejected_envelope_keeps_profile_invalidation_not_stale_sidecar(
    delivery: _Delivery, active_lease: None, sequence: str
) -> None:
    old_order = await _seed_completed_profile(delivery)
    gate, _, _, wire_output = delivery.wire
    seq = delivery.box.seq if sequence == "duplicate" else delivery.box.seq + 2
    gate._updates_queue.put_nowait(_envelope(delivery.now, seq))
    loop = asyncio.create_task(gate._update_loop())
    try:
        new_order = await _wait_pending(delivery, old_order)
        assert delivery.reader.execute("SELECT name FROM entities WHERE id=55").fetchone()[0] == "Retained"
        row = cast(
            tuple[str], delivery.reader.execute("SELECT detail_json FROM entity_details WHERE entity_id=55").fetchone()
        )
        assert json.loads(row[0])["about"] == "retained"
        assert (
            delivery.reader.execute(
                "SELECT generation FROM entity_profile_refresh_state WHERE entity_id=55"
            ).fetchone()[0]
            == 2
        )
        assert new_order > old_order and not wire_output.frames
        if sequence == "gap":
            assert ENTRY_ACCOUNT in delivery.box.getting_diff_for
    finally:
        await _stop(loop)


async def test_auth_aba_during_admission_rejects_original_receipt(
    delivery: _Delivery, active_lease: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_order = await _seed_completed_profile(delivery)
    gate = delivery.wire[0]
    repository = gate._delivery_repository
    assert repository is not None
    original = repository.admit
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked(payload: bytes, count: int) -> object:
        entered.set()
        await release.wait()
        return await original(payload, count)

    monkeypatch.setattr(repository, "admit", blocked)
    gate._updates_queue.put_nowait(_envelope(delivery.now, delivery.box.seq))
    envelope = gate._updates_queue.get_nowait()
    updates: list[object] = []
    users, chats = delivery.box.process_updates(envelope, gate._mb_entity_cache, updates)
    task = asyncio.create_task(gate._preprocess_updates(updates, users, chats))
    await asyncio.wait_for(entered.wait(), 3)
    token = gate.capture_image_scope()
    assert token is not None
    gate._image_scope_cell.publish(replace(token.scope, account_id=43))
    gate._image_scope_cell.publish(token.scope)
    release.set()
    with pytest.raises(RuntimeError, match="receipt authorization scope changed"):
        await task
    assert delivery.reader.execute(
        "SELECT status,generation FROM entity_profile_refresh_state WHERE entity_id=55"
    ).fetchone() == ("complete", 1)
    assert delivery.reader.execute(
        "SELECT observation_order FROM entity_profile_field_observations "
        "WHERE entity_id=55 AND field='__invalidation__:user'"
    ).fetchone() == (old_order,)


async def test_difference_covers_inflight_update_without_losing_invalidation(
    delivery: _Delivery, active_lease: None
) -> None:
    old_order = await _seed_completed_profile(delivery)
    gate, sender, connection, _ = delivery.wire
    delivery.box.try_begin_get_diff(ENTRY_ACCOUNT, "test")
    loop = asyncio.create_task(gate._update_loop())
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        seq = delivery.box.seq + 1
        gate._updates_queue.put_nowait(_envelope(delivery.now, seq))
        state.future.set_result(types.updates.DifferenceEmpty(date=delivery.now, seq=seq))
        await _wait_pending(delivery, old_order)
        assert delivery.reader.execute("SELECT name FROM entities WHERE id=55").fetchone() == ("Retained",)
        assert delivery.reader.execute(
            "SELECT generation FROM entity_profile_refresh_state WHERE entity_id=55"
        ).fetchone() == (2,)
    finally:
        await _stop(loop)


async def test_pending_rescued_update_survives_reopen_and_replay(
    delivery: _Delivery, active_lease: None, tmp_path: Path
) -> None:
    old_order = await _seed_completed_profile(delivery)
    gate = delivery.wire[0]
    gate._updates_queue.put_nowait(_envelope(delivery.now, delivery.box.seq))
    envelope = gate._updates_queue.get_nowait()
    updates: list[object] = []
    users, chats = delivery.box.process_updates(envelope, gate._mb_entity_cache, updates)
    await gate._preprocess_updates(updates, users, chats)
    with closing(open_sync_db_reader(delivery.path)) as reopened:
        assert reopened.execute("SELECT pending IS NOT NULL FROM update_delivery").fetchone() == (1,)
        order = cast(tuple[int], reopened.execute("SELECT observation_order FROM update_delivery_tokens").fetchone())[0]
        assert reopened.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=55").fetchone() == (
            "complete",
        )
    client = _client(tmp_path / "replay_session", delivery.writer, channel=False)
    client._safety_store = gate._safety_store
    client._safety = None
    client._mb_entity_cache.set_self_user(42, False, 999)
    assert gate._session_lease is not None
    client.bind_session_lease(gate._session_lease)
    client._authorized = True
    manager = _manager(client, delivery.reader, delivery.writer)
    manager.set_self_id(42)
    try:
        await client.verify_startup_account(42)
        await client.start_update_delivery(42)
    finally:
        cast(SQLiteSession, client.session).close()
    assert await _wait_pending(delivery, old_order) == order
    with closing(open_sync_db_reader(delivery.path)) as reopened:
        assert reopened.execute("SELECT pending IS NULL FROM update_delivery").fetchone() == (1,)
        assert reopened.execute(
            "SELECT generation FROM entity_profile_refresh_state WHERE entity_id=55"
        ).fetchone() == (2,)
        assert reopened.execute("SELECT name FROM entities WHERE id=55").fetchone() == ("Retained",)
