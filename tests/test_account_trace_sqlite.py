from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

from mcp_telegram.account_trace_sqlite import (
    TraceMessageQueryRequest,
    account_by_username,
    build_evidence_query,
    coverage_fragments,
    dialog_metadata,
    evidence_page,
)

_SQL_WRITE_RE = re.compile(r"(?<![\w.])(?:INSERT|UPDATE|DELETE|REPLACE)\b", flags=re.IGNORECASE)


def _assert_read_only_source(source: str) -> None:
    assert _SQL_WRITE_RE.search(source) is None


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE messages (
            dialog_id INTEGER, message_id INTEGER, sent_at INTEGER, text TEXT,
            sender_id INTEGER, media_kind TEXT, media_payload TEXT, forum_topic_id INTEGER,
            post_author TEXT, is_deleted INTEGER, is_service INTEGER, out INTEGER
        );
        CREATE TABLE dialogs (dialog_id INTEGER, name TEXT, type TEXT, hidden INTEGER);
        CREATE TABLE entities (id INTEGER, name TEXT, username TEXT, name_normalized TEXT, type TEXT, updated_at INTEGER);
        CREATE TABLE topic_metadata (dialog_id INTEGER, topic_id INTEGER, title TEXT);
        CREATE TABLE synced_dialogs (dialog_id INTEGER, status TEXT);
        CREATE TABLE trace_coverage_fragments (
            target_user_id INTEGER, dialog_id INTEGER, topic_id INTEGER, coverage_kind TEXT,
            status TEXT, fetched_at INTEGER, checkpoint TEXT, last_error TEXT,
            next_retry_at INTEGER, created_at INTEGER, updated_at INTEGER
        );
        """
    )
    return conn


def test_evidence_page_keeps_limit_plus_one_order_and_row_mapping() -> None:
    conn = _conn()
    try:
        conn.executemany(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, 0, 0)",
            [(42, 1, 10, "older", 7), (42, 2, 20, "newer", 7)],
        )

        rows = evidence_page(conn, TraceMessageQueryRequest(target_user_id=7, self_id=None, limit=2))

        assert [row["message_id"] for row in rows] == [2, 1]
        assert rows[0]["authorship_basis"] == "effective_sender_id"
    finally:
        conn.close()


def test_sqlite_reads_preserve_scopes_and_metadata_defaults() -> None:
    conn = _conn()
    try:
        conn.execute("INSERT INTO entities VALUES (7, 'Alice', 'alice', 'alice', 'User', 1)")
        conn.execute(
            "INSERT INTO trace_coverage_fragments VALUES (7, 42, 0, 'authored_message', 'partial', NULL, NULL, NULL, NULL, 1, 2)"
        )

        sql, params = build_evidence_query(
            TraceMessageQueryRequest(target_user_id=7, self_id=7, limit=2, scope_dialog_ids=[42, 43])
        )

        assert "m.dialog_id IN (:scope_0, :scope_1)" in sql
        assert params["scope_0"] == 42
        assert (
            account_by_username(conn, "ALICE")
            == conn.execute("SELECT id, name, username, name_normalized FROM entities WHERE id = 7").fetchone()
        )
        assert coverage_fragments(conn, target_user_id=7)[0]["status"] == "partial"
        assert dialog_metadata(conn, 999) == {"dialog_type": "Unknown", "status": "not_synced", "hidden": False}
    finally:
        conn.close()


def test_account_trace_sqlite_remains_read_only_and_telegram_free() -> None:
    path = Path(__file__).parents[1] / "src/mcp_telegram/account_trace_sqlite.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    _assert_read_only_source(source)

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            assert "telethon" not in ast.unparse(node).casefold()
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"commit", "rollback"}


@pytest.mark.parametrize("keyword", ("INSERT", "UPDATE", "DELETE", "REPLACE"))
def test_account_trace_read_only_guard_rejects_sql_writes(keyword: str) -> None:
    with pytest.raises(AssertionError):
        _assert_read_only_source(f"{keyword} INTO messages VALUES (1)")
