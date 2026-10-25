"""Finite private-peer admission is independent of message-history enrollment."""

import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from mcp_telegram.entity_profile.repository import (
    _read_profile_admission_state,
    _reconcile_profile_admission,
    admit_profile_peer,
    personal_profile_peer,
    profile_peer_managed,
    request_profile_directory_pass,
)
from mcp_telegram.sync_transactions import write_savepoint


def test_finite_cohort_reconciles_all_candidates_and_then_stays_idle(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    with write_savepoint(conn):
        conn.executemany(
            "INSERT INTO dialogs(dialog_id,type,name,hidden,archived) VALUES (?,?,?,1,1)",
            ((peer, "bot" if peer % 2 else "user", f"Peer {peer}") for peer in range(1, 448)),
        )
        conn.executemany(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (?,0,'explicit',1)",
            ((peer,) for peer in range(1, 448)),
        )
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES(9000,1,1,'private history')")
        conn.execute("INSERT INTO dialogs(dialog_id,type,name) VALUES(777000,'service','Service')")
    enrollment = conn.execute("SELECT * FROM full_history_enrollment").fetchall()
    examined = []
    while count := _reconcile_profile_admission(conn, 9000, now=100, limit=32, pair_mode="enabled"):
        assert count <= 32
        examined.append(count)
    assert _read_profile_admission_state(conn, 9000)["completed"]
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_refresh_state").fetchone() == (447,)
    assert conn.execute("SELECT * FROM full_history_enrollment").fetchall() == enrollment
    statements = []
    conn.set_trace_callback(statements.append)
    assert _reconcile_profile_admission(conn, 9000, now=200, limit=32, pair_mode="enabled") == 0
    conn.set_trace_callback(None)
    assert not any("FROM dialogs" in sql or "FROM messages" in sql for sql in statements)
    with write_savepoint(conn):
        conn.execute("INSERT INTO dialogs(dialog_id,type,name) VALUES(500,'user','New')")
        request_profile_directory_pass(conn, 9000, 2)
    while _reconcile_profile_admission(conn, 9000, now=300, limit=32, pair_mode="enabled"):
        pass
    assert profile_peer_managed(conn, 9000, 500)
    assert sum(examined) == 448


def test_checkpoint_rolls_back_with_admission_and_resumes_on_reopen(tmp_path: Path) -> None:
    from mcp_telegram.sync_db import ensure_sync_schema

    path = tmp_path / "admission.sqlite"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    with write_savepoint(conn):
        conn.executemany("INSERT INTO dialogs(dialog_id,type) VALUES (?,'user')", ((peer,) for peer in range(1, 40)))
    with patch("mcp_telegram.entity_profile.repository.admit_profile_peer", side_effect=RuntimeError("cancelled")):
        with pytest.raises(RuntimeError):
            _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled")
    assert _read_profile_admission_state(conn, 77)["last_peer"] == 0
    assert conn.execute("SELECT COUNT(*) FROM entity_profile_refresh_state").fetchone() == (0,)
    assert _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled") == 32
    conn.close()
    conn = sqlite3.connect(path)
    try:
        assert _read_profile_admission_state(conn, 77)["last_peer"] == 32
        while _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled"):
            pass
        assert conn.execute("SELECT COUNT(*) FROM entity_profile_refresh_state").fetchone() == (39,)
        with write_savepoint(conn):
            assert admit_profile_peer(conn, 77, 0, now=100) is False
            assert admit_profile_peer(conn, 77, 77, now=100) is False
            assert admit_profile_peer(conn, 77, 1, now=100) is False
            assert admit_profile_peer(conn, 77, 500, now=100)
        assert conn.execute("SELECT type FROM entities WHERE id=500").fetchone() == ("unknown",)
    finally:
        conn.close()


def test_existing_indexes_bound_each_candidate_phase(make_synced_db: Callable[[], sqlite3.Connection]) -> None:
    conn = make_synced_db()
    cases = (
        (
            "SELECT dialog_id FROM dialogs WHERE type=? AND dialog_id>? ORDER BY dialog_id LIMIT 32",
            ("user", 0),
            "idx_dialogs_type",
        ),
        (
            "SELECT dialog_id FROM messages WHERE dialog_id>? AND dialog_id>0 ORDER BY dialog_id LIMIT 1",
            (0,),
            "dialog_id>?",
        ),
    )
    for sql, args, expected in cases:
        plan = " ".join(
            str(row[3])
            for row in cast(list[tuple[object, ...]], conn.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall())
        )
        assert expected in plan and "TEMP B-TREE" not in plan


@pytest.mark.parametrize("outgoing", [False, True])
async def test_delivered_private_peer_admits_before_history_disabled_filter(
    make_synced_db: Callable[[], sqlite3.Connection], outgoing: bool
) -> None:
    import asyncio
    from unittest.mock import MagicMock

    from helpers import build_mock_message
    from tests.test_event_handlers import make_manager, make_new_message_event

    conn = make_synced_db()
    with write_savepoint(conn):
        conn.execute("INSERT INTO dialogs(dialog_id,type,hidden,archived) VALUES(42,'user',1,1)")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES(42,0,'explicit',1)"
        )
    manager = make_manager(MagicMock(), conn, asyncio.Event())
    manager._self_id = 77
    message = build_mock_message(id=1, text="private")
    message.out = outgoing
    message.sender = None
    await manager.on_new_message(make_new_message_event(42, message, is_private=True))
    assert profile_peer_managed(conn, 77, 42)
    assert conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id=42").fetchone() == (0,)
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE dialog_id=42").fetchone() == (0,)


@pytest.mark.parametrize("priority_name", ["FOREGROUND", "BACKFILL"])
def test_own_archive_uses_same_admission_without_fabricated_human_kind(
    make_synced_db: Callable[[], sqlite3.Connection], priority_name: str
) -> None:
    from helpers import build_mock_message
    from mcp_telegram.activity_sync import _persist_own_message_rows
    from mcp_telegram.hydration_queue import HydrationPriority
    from mcp_telegram.messages.telegram_adapter import extract_message_row

    conn = make_synced_db()
    with write_savepoint(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=77 WHERE singleton=1")
        message = build_mock_message(id=1, text="own archive")
        message.out = True
        _persist_own_message_rows(conn, [extract_message_row(42, message)], priority=HydrationPriority[priority_name])
    assert profile_peer_managed(conn, 77, 42)
    assert conn.execute("SELECT type FROM entities WHERE id=42").fetchone() == ("unknown",)
    assert conn.execute("SELECT COUNT(*) FROM full_history_enrollment").fetchone() == (0,)


def test_directory_publication_during_pass_revisits_new_lower_peer_once(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    with write_savepoint(conn):
        conn.executemany("INSERT INTO dialogs(dialog_id,type) VALUES(?,'user')", ((peer,) for peer in range(100, 170)))
        request_profile_directory_pass(conn, 77, 1)
    assert _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled") == 32
    with write_savepoint(conn):
        conn.execute("INSERT INTO dialogs(dialog_id,type) VALUES(50,'user')")
        request_profile_directory_pass(conn, 77, 2)
    # First traversal completes against its captured directory generation.
    while _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled"):
        pass
    # A publication during the traversal coalesces one further finite pass.
    while _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled"):
        pass
    assert profile_peer_managed(conn, 77, 50)
    assert _read_profile_admission_state(conn, 77)["directory_generation"] == 2
    assert _reconcile_profile_admission(conn, 77, now=100, limit=32, pair_mode="enabled") == 0


def test_group_only_user_identity_is_not_a_personal_correspondent(
    make_synced_db: Callable[[], sqlite3.Connection],
) -> None:
    conn = make_synced_db()
    with write_savepoint(conn):
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'user',1)")
    assert not personal_profile_peer(conn, 77, 42)
    with write_savepoint(conn):
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES(42,1,1)")
    assert personal_profile_peer(conn, 77, 42)


@pytest.mark.parametrize("existing_owner,requested_owner,preserved", [(7, 7, True), (8, 7, False)])
def test_pre_details_not_found_keeps_bound_negative_state(
    make_synced_db: Callable[[], sqlite3.Connection], existing_owner: int, requested_owner: int, preserved: bool
) -> None:
    from mcp_telegram.entity_profile.repository import EntityProfileRepository

    conn = make_synced_db()
    repo = EntityProfileRepository(conn, section_ttl_seconds=300)
    with write_savepoint(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=? WHERE singleton=1", (existing_owner,))
        conn.execute("INSERT INTO entities(id,type,updated_at) VALUES(42,'unknown',1)")
        repo.mark_pending(42, now=100)
        repo.mark_refresh_not_found(42, now=101)
    before = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT * FROM entity_profile_refresh_state WHERE entity_id=42").fetchone(),
    )
    assert conn.execute("SELECT COUNT(*) FROM entity_details").fetchone() == (0,)
    with write_savepoint(conn):
        queued = admit_profile_peer(conn, requested_owner, 42, now=200)
    after = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT * FROM entity_profile_refresh_state WHERE entity_id=42").fetchone(),
    )
    assert queued is not preserved
    assert (after == before) is preserved
    assert conn.execute("SELECT COUNT(*) FROM entity_details").fetchone() == (0,)
