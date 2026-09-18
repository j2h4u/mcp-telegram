"""Focused Slice 2 aggregate ordering and detail lifecycle contracts."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from mcp_telegram.reactions.contracts import (
    ReactionAggregate,
    ReactionDetailFetchResult,
    ReactionDetailPage,
    ReactionEvent,
)
from mcp_telegram.reactions.detail import ReactionDetailRefresher
from mcp_telegram.reactions.persistence import apply_aggregate_observation
from mcp_telegram.reactions.telegram_adapter import TelethonTelegramReactionGateway
from mcp_telegram.sync_db import _apply_migration_68, ensure_sync_schema


def _db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
    conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (1,1,'explicit',1)")
    conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,2,1)")
    return conn


def test_aggregate_ordering_accepts_empty_and_rejects_older(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 2)], source="history", observed_at=10, observation_sequence=1
    )
    assert apply_aggregate_observation(conn, 1, 2, [], source="raw_update", observed_at=10, observation_sequence=2)
    assert not apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("🔥", 1)], source="history", observed_at=9, observation_sequence=3
    )
    assert conn.execute("SELECT COUNT(*) FROM message_reactions").fetchone() == (0,)
    assert conn.execute("SELECT generation,source FROM message_reaction_aggregate_state").fetchone() == (
        2,
        "raw_update",
    )
    conn.close()


def test_detail_partial_offset_survives_restart_and_publishes(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    # A migrated aggregate may predate its detail receipt. The first page
    # acquisition must recreate that stale receipt transactionally.
    conn.execute("DELETE FROM message_reaction_event_status WHERE dialog_id=1 AND message_id=2")

    class Gateway:
        def __init__(self) -> None:
            self.offsets: list[str | None] = []

        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id
            del limit
            self.offsets.append(offset)
            events = (ReactionEvent(7, "👍", None),)
            return ReactionDetailFetchResult(page=ReactionDetailPage(events, "next" if offset is None else None))

    gateway = Gateway()
    refresher = ReactionDetailRefresher(conn, gateway, now=lambda: 20)
    first = asyncio.run(refresher.refresh_one(1, 2, 1, entity=SimpleNamespace(), now=20))
    assert first.status == "partial"
    assert conn.execute("SELECT next_offset FROM message_reaction_event_status").fetchone() == ("next",)

    second = asyncio.run(refresher.refresh_one(1, 2, 1, entity=SimpleNamespace(), offset="next", now=21))
    assert second.status == "complete"
    assert gateway.offsets == [None, "next"]
    assert conn.execute("SELECT status,display_generation FROM message_reaction_event_status").fetchone() == (
        "complete",
        1,
    )
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events WHERE display_generation=1").fetchone() == (2,)
    conn.close()


def test_detail_gateway_uses_one_page_and_never_get_messages() -> None:
    class Client:
        def __init__(self) -> None:
            self.pages = 0
            self.resolved: list[object] = []

        async def get_messages(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("detail acquisition must not call get_messages")

        async def get_input_entity(self, entity: object) -> object:
            self.resolved.append(entity)
            return SimpleNamespace(peer_id=entity)

        async def __call__(self, request: object) -> object:
            self.pages += 1
            assert request.__class__.__name__ == "GetMessageReactionsListRequest"
            return SimpleNamespace(reactions=(), next_offset=None)

    client = Client()
    result = asyncio.run(TelethonTelegramReactionGateway(client).fetch_reaction_page(42, 2, offset=None, limit=100))
    assert result.ok
    assert client.pages == 1
    assert client.resolved == [42]


def test_reaction_migration_preserves_empty_receipts_and_cleans_orphans() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER, applied_at INTEGER);
        CREATE TABLE messages(dialog_id INTEGER, message_id INTEGER, PRIMARY KEY(dialog_id, message_id));
        CREATE TABLE message_reactions_freshness(
            dialog_id INTEGER, message_id INTEGER, checked_at INTEGER,
            PRIMARY KEY(dialog_id, message_id)
        );
        CREATE TABLE message_reactions(
            dialog_id INTEGER, message_id INTEGER, emoji TEXT, count INTEGER,
            PRIMARY KEY(dialog_id, message_id, emoji)
        );
        CREATE TABLE message_reaction_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dialog_id INTEGER, message_id INTEGER, reactor_id INTEGER,
            emoji TEXT, reacted_at INTEGER, fetched_at INTEGER NOT NULL
        );
        CREATE TABLE message_reaction_event_status(
            dialog_id INTEGER, message_id INTEGER, checked_at INTEGER NOT NULL,
            status TEXT NOT NULL, returned_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(dialog_id, message_id)
        );
        INSERT INTO messages VALUES (1, 2), (1, 3);
        INSERT INTO message_reactions_freshness VALUES (1, 2, 100);
        INSERT INTO message_reaction_event_status VALUES (1, 2, 101, 'complete', 0);
        INSERT INTO message_reaction_event_status VALUES (1, 3, 102, 'unavailable', 1);
        INSERT INTO message_reaction_event_status VALUES (1, 99, 103, 'complete', 1);
        INSERT INTO message_reaction_events(dialog_id, message_id, emoji, fetched_at)
            VALUES (1, 3, '👍', 102), (1, 99, '🔥', 103);
        """
    )

    _apply_migration_68(conn, 67)

    assert conn.execute(
        "SELECT dialog_id, message_id, aggregate_row_count FROM message_reaction_aggregate_state ORDER BY message_id"
    ).fetchall() == [(1, 2, 0), (1, 3, 0)]
    assert conn.execute(
        "SELECT message_id, status, display_generation FROM message_reaction_event_status ORDER BY message_id"
    ).fetchall() == [(2, "complete", 1), (3, "unavailable", 1)]
    assert conn.execute("SELECT message_id FROM message_reaction_events").fetchall() == [(3,)]
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='message_reactions_freshness'").fetchone() is None
    conn.close()
