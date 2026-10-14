from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl import types

from helpers import build_mock_message
from mcp_telegram.chat_export import _message_reactions
from mcp_telegram.message_fact_refresh import (
    _claim_reaction_pages,
    _reaction_candidates,
    _reaction_discovery_page,
    _reaction_page_candidate,
    _reaction_release_at,
)
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.reactions.contracts import (
    ReactionAggregate,
    ReactionDetailFetchResult,
    ReactionDetailPage,
    ReactionEvent,
)
from mcp_telegram.reactions.detail import ReactionDetailRefresher
from mcp_telegram.reactions.persistence import apply_aggregate_observation
from mcp_telegram.reactions.projection import project_reaction_visibility
from mcp_telegram.sync_db import _apply_migration_82, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_fact_queries import reaction_event_projection
from mcp_telegram.telegram_message_projection import MessageLike, message_to_dict


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = sqlite3.connect(path)
    connection.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
    connection.execute("INSERT INTO full_history_enrollment VALUES (1,1,'explicit',1)")
    connection.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,2,1)")
    connection.commit()
    yield connection
    connection.close()


@pytest.mark.parametrize(
    ("reactions", "expected"),
    [
        (None, None),
        (SimpleNamespace(), None),
        (SimpleNamespace(can_see_list=True), True),
        (SimpleNamespace(can_see_list=False), False),
        (SimpleNamespace(can_see_list=False, min=True), None),
        (SimpleNamespace(can_see_list=None, min=True), None),
        (SimpleNamespace(can_see_list=True, min=True), True),
    ],
)
def test_visibility_preserves_partial_unknown(reactions: object | None, expected: bool | None) -> None:
    assert project_reaction_visibility(reactions) is expected


def _observe(conn: sqlite3.Connection, sequence: int, visibility: bool | None) -> bool:
    accepted = apply_aggregate_observation(
        conn,
        1,
        2,
        [ReactionAggregate("👍", 1)],
        source="history",
        observed_at=sequence,
        observation_sequence=sequence,
        can_see_list=visibility,
    )
    conn.commit()
    return accepted


def test_visibility_change_invalidates_same_count_and_unknown_cannot_revoke(conn: sqlite3.Connection) -> None:
    assert _observe(conn, 1, True)
    conn.execute(
        "INSERT INTO message_reaction_events(dialog_id,message_id,reactor_id,emoji,fetched_at,detail_generation,display_generation) "
        "VALUES(1,2,7,'👍',1,1,1)"
    )
    conn.execute("UPDATE message_reaction_event_status SET display_generation=1,status='complete'")
    conn.commit()
    assert _observe(conn, 2, False)
    assert conn.execute("SELECT generation,can_see_list FROM message_reaction_aggregate_state").fetchone() == (2, 0)
    assert conn.execute("SELECT status,failure_kind,next_attempt_at FROM message_reaction_event_status").fetchone() == (
        "unavailable",
        "visibility_unavailable",
        None,
    )
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events").fetchone() == (0,)
    assert conn.execute("SELECT count FROM message_reactions").fetchone() == (1,)
    assert _observe(conn, 3, None)
    assert conn.execute("SELECT generation,can_see_list FROM message_reaction_aggregate_state").fetchone() == (2, 0)
    assert not _observe(conn, 1, True)
    assert _observe(conn, 4, True)
    assert conn.execute("SELECT generation,can_see_list FROM message_reaction_aggregate_state").fetchone() == (3, 1)
    assert conn.execute("SELECT status,next_attempt_at FROM message_reaction_event_status").fetchone() == ("stale", 4)


@pytest.mark.parametrize("visibility", [False, None, True])
def test_scheduler_gates_known_false_everywhere(conn: sqlite3.Connection, visibility: bool | None) -> None:
    _observe(conn, 1, visibility)
    expected = [] if visibility is False else [(1, 2, 1, None)]
    assert _reaction_candidates(conn, stale_before_utc=10, limit=5) == expected
    rows = _reaction_discovery_page(conn, None, (1, 2))
    assert (_reaction_page_candidate(rows[0], 10) is None) is (visibility is False)
    assert (_reaction_release_at(conn, 10) is None) is (visibility is False)
    assert (
        _claim_reaction_pages(
            conn, now=10, max_pages=1, cycle_seconds=600, candidate_limit=1, candidates=[(1, 2, 1, None)]
        )
        == expected
    )


def test_hidden_cached_actors_and_authoritative_empty_are_withdrawn(conn: sqlite3.Connection) -> None:
    _observe(conn, 1, False)
    conn.execute(
        "INSERT INTO message_reaction_events(dialog_id,message_id,reactor_id,emoji,fetched_at,detail_generation,display_generation) "
        "VALUES(1,2,7,'👍',1,1,1)"
    )
    conn.commit()
    assert reaction_event_projection(conn, 1, [2])[0] == {}
    apply_aggregate_observation(
        conn, 1, 2, [], source="history", observed_at=2, observation_sequence=2, can_see_list=False
    )
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM message_reactions").fetchone() == (0,)
    assert _reaction_candidates(conn, stale_before_utc=10, limit=5) == []


@pytest.mark.asyncio
async def test_detail_gate_and_inflight_visibility_fence(conn: sqlite3.Connection) -> None:
    _observe(conn, 1, False)

    class Gateway:
        calls = 0

        async def fetch_reaction_page(self, *_args: object, **_kwargs: object) -> ReactionDetailFetchResult:
            self.calls += 1
            _observe(conn, 3, False)
            return ReactionDetailFetchResult(page=ReactionDetailPage((ReactionEvent(7, "👍", 1),), None))

    gateway = Gateway()
    refresher = ReactionDetailRefresher(conn, gateway)
    denied = await refresher.refresh_one(1, 2, 1, entity=1)
    assert denied.failure_kind == "visibility_unavailable"
    assert gateway.calls == 0
    _observe(conn, 2, True)
    result = await refresher.refresh_one(1, 2, 2, entity=1)
    assert result.status == "stale_writer"
    assert gateway.calls == 1
    assert reaction_event_projection(conn, 1, [2])[0] == {}


@pytest.mark.parametrize("forwarded", [False, True])
@pytest.mark.parametrize("visibility", [False, None, True])
def test_sync_and_direct_reads_share_visibility(
    conn: sqlite3.Connection, forwarded: bool, visibility: bool | None
) -> None:
    reactions = SimpleNamespace(
        results=[SimpleNamespace(reaction=types.ReactionEmoji("👍"), count=1)],
        can_see_list=visibility,
        recent_reactions=[SimpleNamespace(peer_id=types.PeerUser(7), reaction=types.ReactionEmoji("👍"), date=None)],
    )
    message = build_mock_message(id=2, reactions=reactions)
    message.out = False
    if forwarded:
        message.fwd_from = SimpleNamespace(from_id=types.PeerChannel(9), date=None, channel_post=4)
    extracted = extract_message_row(1, message)
    assert extracted.reaction_can_see_list is visibility
    with write_transaction(conn):
        insert_messages_with_fts(conn, [extracted])
    row = conn.execute("SELECT can_see_list FROM message_reaction_aggregate_state").fetchone()
    assert row == (visibility,)
    direct = message_to_dict(cast(MessageLike, message), dialog_id=1)
    assert bool(direct["reaction_events"]) is (visibility is not False)
    assert direct["reactions_display"] == "[👍×1]"


@pytest.mark.parametrize(
    "visibility,min_flag,expected", [(False, False, False), (False, True, None), (True, True, True)]
)
def test_export_uses_shared_visibility(
    conn: sqlite3.Connection, visibility: bool, min_flag: bool, expected: bool | None
) -> None:
    reactions = types.MessageReactions(
        [types.ReactionCount(types.ReactionEmoji("👍"), 1)], can_see_list=visibility, min=min_flag
    )
    assert _message_reactions(reactions, {}, conn)["can_view_list"] is expected


def test_migration_82_keeps_old_capability_unknown_and_preserves_terminal_state() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            "CREATE TABLE schema_version(version INTEGER,applied_at INTEGER);"
            "CREATE TABLE message_reaction_aggregate_state(dialog_id INTEGER,message_id INTEGER,generation INTEGER);"
            "CREATE TABLE message_reaction_event_status(status TEXT,failure_kind TEXT,next_attempt_at INTEGER);"
            "INSERT INTO message_reaction_aggregate_state VALUES(1,2,3);"
            "INSERT INTO message_reaction_event_status VALUES('unavailable','invalid_target',NULL);"
        )
        assert _apply_migration_82(conn, 81) == 82
        assert _apply_migration_82(conn, 82) == 82
        assert conn.execute("SELECT generation,can_see_list FROM message_reaction_aggregate_state").fetchone() == (
            3,
            None,
        )
        assert conn.execute("SELECT * FROM message_reaction_event_status").fetchone() == (
            "unavailable",
            "invalid_target",
            None,
        )
    finally:
        conn.close()
