"""Deterministic enrollment-disable races at Telegram persistence boundaries."""
# pyright: reportAny=false, reportArgumentType=false, reportMissingParameterType=false

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from helpers import build_mock_message
from mcp_telegram.daemon import (
    _apply_read_positions_from_dialogs,
    _backfill_blank_unsupported_messages,
    _backfill_total_message_dialog,
)
from mcp_telegram.history_enrollment import disable_history
from mcp_telegram.message_fact_refresh import MessageFactRefreshDeps, refresh_message_facts_once
from mcp_telegram.reactions.contracts import ReactionAggregate, ReactionFetchResult, ReactionSnapshot
from mcp_telegram.reactions.refresh import ReactionFreshener
from mcp_telegram.reactions.sqlite_repository import SQLiteReactionSnapshotRepository
from mcp_telegram.sync_db import _apply_migrations


def _dbs() -> tuple[sqlite3.Connection, sqlite3.Connection]:
    first = sqlite3.connect(":memory:")
    _apply_migrations(first)
    second = sqlite3.connect(":memory:")
    _apply_migrations(second)
    return first, second


@pytest.mark.asyncio
async def test_media_backfill_disable_after_fetch_discards_body(tmp_path) -> None:
    path = tmp_path / "race.db"
    first = sqlite3.connect(path)
    second = sqlite3.connect(path)
    _apply_migrations(first)
    first.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
    first.execute("INSERT INTO full_history_enrollment VALUES (1,1,'explicit',1)")
    first.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,media_description) VALUES (1,7,1,'','MessageMediaUnsupported')"
    )
    first.commit()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(**_: object) -> list[object]:
        entered.set()
        await release.wait()
        return [build_mock_message(id=7, text="recovered")]

    client = MagicMock(get_messages=AsyncMock(side_effect=fetch))
    task = asyncio.create_task(_backfill_blank_unsupported_messages(client, first, asyncio.Event()))
    await entered.wait()
    disable_history(second, 1, now=2)
    second.commit()
    release.set()
    await task
    assert first.execute("SELECT text FROM messages WHERE dialog_id=1 AND message_id=7").fetchone() == ("",)
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_message_fact_refresh_disable_during_reaction_await_discards_snapshot() -> None:
    from tests.test_message_fact_refresh import _make_db, _policy

    conn = _make_db()
    conn.execute("INSERT INTO synced_dialogs VALUES (4, 'synced')")
    conn.execute("INSERT INTO full_history_enrollment VALUES (4, 1, 'explicit', 1)")
    conn.execute("INSERT INTO messages VALUES (4, 1, 1, 0)")
    conn.execute("INSERT INTO message_reactions VALUES (4, 1, '👍', 1)")
    conn.commit()
    entered = asyncio.Event()
    release = asyncio.Event()

    class Gateway:
        async def fetch_reactions(self, _entity: object, _ids: list[int]) -> ReactionFetchResult:
            entered.set()
            await release.wait()
            return ReactionFetchResult((ReactionSnapshot(1, (ReactionAggregate("🔥", 9),)),))

    freshener = ReactionFreshener(
        SQLiteReactionSnapshotRepository(conn), Gateway(), freshness_ttl_seconds=1, now=lambda: 1000
    )
    task = asyncio.create_task(
        refresh_message_facts_once(MessageFactRefreshDeps(conn, freshener, None), _policy(read_at_max=0), now=1000)
    )
    await entered.wait()
    conn.execute("UPDATE full_history_enrollment SET enabled = 0 WHERE dialog_id = 4")
    conn.commit()
    release.set()
    await task
    assert conn.execute("SELECT emoji, count FROM message_reactions WHERE dialog_id=4").fetchall() == [("👍", 1)]
    assert conn.execute("SELECT * FROM message_reactions_freshness").fetchall() == []
    conn.close()


@pytest.mark.asyncio
async def test_new_message_disable_between_permission_checks_discards_body(tmp_path) -> None:
    from tests.test_event_handlers import make_manager, make_new_message_event

    path = tmp_path / "event-race.db"
    first = sqlite3.connect(path)
    second = sqlite3.connect(path)
    _apply_migrations(first)
    first.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (5, 'synced')")
    first.execute("INSERT INTO full_history_enrollment VALUES (5, 1, 'explicit', 1)")
    first.commit()
    manager = make_manager(MagicMock(), first, asyncio.Event())
    original = manager._realtime_coverage
    calls = 0

    def coverage(dialog_id: int):
        nonlocal calls
        calls += 1
        if calls == 2:
            disable_history(second, dialog_id, now=2)
            second.commit()
        return original(dialog_id)

    manager._realtime_coverage = coverage
    await manager.on_new_message(make_new_message_event(5, build_mock_message(id=1, text="stale")))
    assert first.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=5").fetchone() == (0,)
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_own_only_outgoing_remains_allowed_without_full_enrollment(tmp_path) -> None:
    from mcp_telegram.realtime_history_policy import RealtimeHistoryCoverage, allows_new_message

    assert allows_new_message(RealtimeHistoryCoverage.OWN_OUTGOING, outgoing=True)
    assert not allows_new_message(RealtimeHistoryCoverage.OWN_OUTGOING, outgoing=False)


@pytest.mark.asyncio
async def test_total_messages_disable_during_fetch_discards_update(tmp_path) -> None:
    path = tmp_path / "race.db"
    first = sqlite3.connect(path)
    second = sqlite3.connect(path)
    _apply_migrations(first)
    first.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (2,'synced')")
    first.execute("INSERT INTO full_history_enrollment VALUES (2,1,'explicit',1)")
    first.commit()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(**_: object) -> object:
        entered.set()
        await release.wait()
        return SimpleNamespace(total=99)

    client = MagicMock(get_messages=AsyncMock(side_effect=fetch))
    task = asyncio.create_task(_backfill_total_message_dialog(client, first, asyncio.Event(), 2))
    await entered.wait()
    disable_history(second, 2, now=2)
    second.commit()
    release.set()
    await task
    assert first.execute("SELECT total_messages FROM synced_dialogs WHERE dialog_id=2").fetchone() == (None,)
    first.close()
    second.close()


@pytest.mark.asyncio
async def test_read_cursor_disable_before_apply_discards_positions(tmp_path) -> None:
    path = tmp_path / "race.db"
    first = sqlite3.connect(path)
    second = sqlite3.connect(path)
    _apply_migrations(first)
    first.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (3,'synced')")
    first.execute("INSERT INTO full_history_enrollment VALUES (3,1,'explicit',1)")
    first.commit()
    disable_history(second, 3, now=2)
    second.commit()
    result = SimpleNamespace(
        dialogs=[SimpleNamespace(peer=SimpleNamespace(), read_inbox_max_id=7, read_outbox_max_id=8)]
    )
    import mcp_telegram.daemon as daemon

    old = daemon.telethon_utils.get_peer_id
    daemon.telethon_utils.get_peer_id = lambda _: 3
    try:
        assert _apply_read_positions_from_dialogs(first, result) == 0
    finally:
        daemon.telethon_utils.get_peer_id = old
    assert first.execute(
        "SELECT read_inbox_max_id,read_outbox_max_id FROM synced_dialogs WHERE dialog_id=3"
    ).fetchone() == (None, None)
    first.close()
    second.close()
