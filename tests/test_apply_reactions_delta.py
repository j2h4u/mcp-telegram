"""Tests for the reaction repository's per-message aggregate replacement."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.reactions.contracts import ReactionAggregate
from mcp_telegram.reactions.persistence import replace_reaction_aggregates
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "sync.db"
    ensure_sync_schema(db)
    c = cast(sqlite3.Connection, _open_sync_db(db))
    c.execute(
        "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        (12345,),
    )
    c.commit()
    yield c
    c.close()


def _rows(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> list[tuple[str, int]]:
    return cast(
        list[tuple[str, int]],
        conn.execute(
            "SELECT emoji, count FROM message_reactions WHERE dialog_id=? AND message_id=? ORDER BY emoji",
            (dialog_id, message_id),
        ).fetchall(),
    )


def _r(emoji: str, count: int) -> ReactionAggregate:
    return ReactionAggregate(emoji=emoji, count=count)


def test_replace_reaction_aggregates_inserts_fresh_rows(conn: sqlite3.Connection) -> None:
    rows = [_r("👍", 3), _r("❤", 1)]
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, rows)
    assert _rows(conn, 12345, 100) == [("❤", 1), ("👍", 3)]


def test_replace_reaction_aggregates_replaces_existing(conn: sqlite3.Connection) -> None:
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, [_r("👍", 3), _r("❤", 1)])
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, [_r("🔥", 5)])
    assert _rows(conn, 12345, 100) == [("🔥", 5)]


def test_replace_reaction_aggregates_empty_rows_deletes_existing(conn: sqlite3.Connection) -> None:
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, [_r("👍", 3)])
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, [])
    assert _rows(conn, 12345, 100) == []


def test_replace_reaction_aggregates_idempotent(conn: sqlite3.Connection) -> None:
    rows = [_r("👍", 3), _r("❤", 1)]
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, rows)
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, rows)
    assert _rows(conn, 12345, 100) == [("❤", 1), ("👍", 3)]


def test_replace_reaction_aggregates_scoped_to_message_id(conn: sqlite3.Connection) -> None:
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, [_r("👍", 3)])
        replace_reaction_aggregates(conn, 12345, 101, [_r("❤", 7)])
    # Now mutate only msg 100; msg 101 must be untouched.
    with conn:
        replace_reaction_aggregates(conn, 12345, 100, [_r("🔥", 1)])
    assert _rows(conn, 12345, 100) == [("🔥", 1)]
    assert _rows(conn, 12345, 101) == [("❤", 7)]


def test_replace_reaction_aggregates_respects_caller_transaction(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN")
    replace_reaction_aggregates(conn, 12345, 100, [_r("👍", 3)])
    conn.rollback()
    assert _rows(conn, 12345, 100) == []
