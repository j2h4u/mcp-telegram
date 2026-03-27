"""Tests for FTS stemming engine and schema migration v3 (SYNC-07).

Tests are ordered: unit tests first, then integration tests.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.fts import (
    MESSAGES_FTS_DDL,
    INSERT_FTS_SQL,
    backfill_fts_index,
    stem_query,
    stem_text,
)
from mcp_telegram.sync_db import (
    _open_sync_db,
    ensure_sync_schema,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_sync_db_path(tmp_path: Path) -> Path:
    """Return a path to a temporary sync.db file (not yet created)."""
    return tmp_path / "sync.db"


@pytest.fixture()
def fts_conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with messages_fts table ready."""
    conn = sqlite3.connect(":memory:")
    conn.execute(MESSAGES_FTS_DDL)
    conn.commit()
    return conn


# ---------------------------------------------------------------------------
# SYNC-07: stem_text — Russian morphology
# ---------------------------------------------------------------------------


def test_stem_text_russian_morphology() -> None:
    """stem_text reduces Russian morphological variants to the same stem."""
    # "написал" (wrote, masc) and "написали" (wrote, plural) should share a stem
    assert stem_text("написал") == stem_text("написали"), (
        "Russian morphological variants must produce identical stems"
    )
    # "сообщение" (message, nom) and "сообщениями" (messages, instrumental)
    assert stem_text("сообщение") == stem_text("сообщениями"), (
        "Russian noun case variants must produce identical stems"
    )


# ---------------------------------------------------------------------------
# SYNC-07: stem_text — None and empty
# ---------------------------------------------------------------------------


def test_stem_text_none_and_empty() -> None:
    """stem_text(None) and stem_text('') both return empty string."""
    assert stem_text(None) == "", "stem_text(None) must return ''"
    assert stem_text("") == "", "stem_text('') must return ''"


# ---------------------------------------------------------------------------
# SYNC-07: stem_text — English words and numbers
# ---------------------------------------------------------------------------


def test_stem_text_english_and_numbers() -> None:
    """stem_text handles English words and passes through numbers."""
    result = stem_text("hello world 123")
    tokens = result.split()
    # Each token should be a stemmed word or number — there must be 3 tokens
    assert len(tokens) == 3, f"Expected 3 tokens, got {tokens!r}"
    # 123 should pass through unchanged
    assert "123" in tokens, f"Number '123' should be preserved, got {tokens!r}"
    # English words get stemmed (hello -> hello or similar, world -> world or similar)
    assert len(tokens[0]) > 0 and len(tokens[1]) > 0, "English tokens must not be empty"


# ---------------------------------------------------------------------------
# SYNC-07: stem_query
# ---------------------------------------------------------------------------


def test_stem_query() -> None:
    """stem_query produces same stems as stem_text for equivalent input."""
    # stem_query and stem_text should be functionally equivalent for word extraction
    assert stem_query("написал сообщение") == stem_text("написал сообщение"), (
        "stem_query and stem_text must produce same output for equivalent input"
    )
    # stem_query on Russian verbs produces consistent output
    q1 = stem_query("написал")
    q2 = stem_query("написали")
    assert q1 == q2, f"stem_query must normalize morphology: {q1!r} != {q2!r}"


# ---------------------------------------------------------------------------
# SYNC-07: FTS table created by migration v3
# ---------------------------------------------------------------------------


def test_fts_table_created_by_migration(tmp_sync_db_path: Path) -> None:
    """After ensure_sync_schema, messages_fts virtual table exists in sqlite_master."""
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchall()
        assert len(rows) == 1, "messages_fts table must exist after migration v3"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-07: FTS morphological search
# ---------------------------------------------------------------------------


def test_fts_morphological_search(fts_conn: sqlite3.Connection) -> None:
    """FTS MATCH on stem_query('написал') returns row containing 'написали'."""
    # Insert stemmed text for "написали сообщение"
    stemmed = stem_text("написали сообщение")
    fts_conn.execute(
        "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (1, 100, stemmed),
    )
    fts_conn.commit()

    # Search using morphological variant
    query = stem_query("написал")
    rows = fts_conn.execute(
        "SELECT message_id FROM messages_fts WHERE messages_fts MATCH ? AND dialog_id = ?",
        (query, 1),
    ).fetchall()
    assert len(rows) == 1, (
        f"FTS MATCH for stem of 'написал' must find row containing 'написали', "
        f"stemmed={stemmed!r}, query={query!r}"
    )
    assert rows[0][0] == 100


# ---------------------------------------------------------------------------
# SYNC-07: FTS dialog scope
# ---------------------------------------------------------------------------


def test_fts_dialog_scope(fts_conn: sqlite3.Connection) -> None:
    """FTS search scoped to dialog_id=1 does not return rows from dialog_id=2."""
    stemmed = stem_text("написал сообщение")
    fts_conn.execute(
        "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (1, 100, stemmed),
    )
    fts_conn.execute(
        "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (2, 200, stemmed),
    )
    fts_conn.commit()

    query = stem_query("написал")
    rows = fts_conn.execute(
        "SELECT dialog_id, message_id FROM messages_fts WHERE messages_fts MATCH ? AND dialog_id = ?",
        (query, 1),
    ).fetchall()
    dialog_ids = {row[0] for row in rows}
    assert 2 not in dialog_ids, (
        f"FTS scoped to dialog_id=1 must not return dialog_id=2 rows, got {rows!r}"
    )
    assert 1 in dialog_ids, "FTS must return the matching row for dialog_id=1"


# ---------------------------------------------------------------------------
# SYNC-07: Schema migration v3 idempotent
# ---------------------------------------------------------------------------


def test_schema_migration_v3_idempotent(tmp_sync_db_path: Path) -> None:
    """Running ensure_sync_schema twice does not raise any error."""
    ensure_sync_schema(tmp_sync_db_path)
    ensure_sync_schema(tmp_sync_db_path)  # must not raise
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchall()
        assert len(rows) == 1, "messages_fts must still exist after second ensure_sync_schema"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SYNC-07: backfill_fts_index
# ---------------------------------------------------------------------------


def test_backfill_fts_index(tmp_sync_db_path: Path) -> None:
    """backfill_fts_index populates messages_fts from messages table.

    - Insert 3 messages into messages table (with ensure_sync_schema first)
    - Call backfill_fts_index
    - Verify messages_fts has 3 rows
    - Verify FTS search finds expected content
    """
    ensure_sync_schema(tmp_sync_db_path)
    conn = _open_sync_db(tmp_sync_db_path)
    try:
        # Insert 3 messages — mix of Russian and English
        messages = [
            (1, 100, 1700000000, "написал сообщение"),
            (1, 101, 1700000001, "hello world"),
            (1, 102, 1700000002, None),  # NULL text — should produce empty stemmed
        ]
        conn.executemany(
            "INSERT INTO messages (dialog_id, message_id, sent_at, text) VALUES (?, ?, ?, ?)",
            messages,
        )
        conn.commit()

        count = backfill_fts_index(conn)
        assert count == 3, f"backfill_fts_index must return count=3, got {count}"

        # Verify FTS table has 3 rows
        rows = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()
        assert rows is not None and rows[0] == 3, f"Expected 3 rows in messages_fts, got {rows}"

        # Verify FTS search finds Russian content via morphology
        query = stem_query("написал")
        results = conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH ? AND dialog_id = ?",
            (query, 1),
        ).fetchall()
        assert len(results) >= 1, (
            f"FTS must find message with 'написал' via stem query {query!r}"
        )
        assert results[0][0] == 100
    finally:
        conn.close()
