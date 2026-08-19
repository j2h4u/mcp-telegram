from __future__ import annotations

import sqlite3

import pytest

from mcp_telegram.daemon_log_context import DialogLogContext, dialog_log_context


@pytest.mark.parametrize("with_dialogs_table", [False, True])
def test_missing_or_unreadable_dialog_context_is_minimal(with_dialogs_table: bool) -> None:
    """Operator context stays available when the local dialog snapshot is absent or empty."""
    conn = sqlite3.connect(":memory:")
    try:
        if with_dialogs_table:
            conn.execute(
                "CREATE TABLE dialogs (dialog_id INTEGER PRIMARY KEY, name TEXT, type TEXT, archived INTEGER, hidden INTEGER)"
            )
        assert dialog_log_context(conn, 42) == DialogLogContext(dialog_id=42)
    finally:
        conn.close()


def test_dialog_log_context_projects_snapshot_fields() -> None:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(
            "CREATE TABLE dialogs (dialog_id INTEGER PRIMARY KEY, name TEXT, type TEXT, archived INTEGER, hidden INTEGER)"
        )
        conn.execute(
            "INSERT INTO dialogs VALUES (?, ?, ?, ?, ?)",
            (7, "Project", "group", 0, 1),
        )
        assert dialog_log_context(conn, 7) == DialogLogContext(
            dialog_id=7,
            name="Project",
            type="group",
            archived=False,
            hidden=True,
        )
    finally:
        conn.close()
