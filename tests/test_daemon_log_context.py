from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from mcp_telegram.daemon_log_context import DialogLogContext, dialog_log_context


@pytest.fixture()
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def _dialogs_schema_conn(_conn: sqlite3.Connection) -> sqlite3.Connection:
    _conn.execute(
        "CREATE TABLE dialogs (dialog_id INTEGER PRIMARY KEY, name TEXT, type TEXT, archived INTEGER, hidden INTEGER)"
    )
    return _conn


class TestDialogLogContextDbError:
    def test_sqlite_error_returns_minimal_context(self, _conn: sqlite3.Connection):
        _conn.execute("CREATE TABLE not_dialogs (id INTEGER)")

        result = dialog_log_context(_conn, dialog_id=42)

        assert isinstance(result, DialogLogContext)
        assert result.dialog_id == 42
        assert result.name is None
        assert result.type is None
        assert result.archived is None
        assert result.hidden is None


class TestDialogLogContextNotFound:
    def test_missing_dialog_returns_minimal_context(self, _dialogs_schema_conn: sqlite3.Connection):
        result = dialog_log_context(_dialogs_schema_conn, dialog_id=99)

        assert isinstance(result, DialogLogContext)
        assert result.dialog_id == 99
        assert result.name is None
        assert result.type is None
        assert result.archived is None
        assert result.hidden is None


class TestDialogLogContextFound:
    def test_existing_dialog_returns_full_context(self, _dialogs_schema_conn: sqlite3.Connection):
        _dialogs_schema_conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, archived, hidden) VALUES (?, ?, ?, ?, ?)",
            (1, "Test Chat", "group", 0, 0),
        )
        _dialogs_schema_conn.commit()

        result = dialog_log_context(_dialogs_schema_conn, dialog_id=1)

        assert isinstance(result, DialogLogContext)
        assert result.dialog_id == 1
        assert result.name == "Test Chat"
        assert result.type == "group"
        assert result.archived is False
        assert result.hidden is False

    def test_existing_dialog_with_nulls_returns_partial_context(self, _dialogs_schema_conn: sqlite3.Connection):
        _dialogs_schema_conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, archived, hidden) VALUES (?, ?, ?, ?, ?)",
            (2, None, None, None, None),
        )
        _dialogs_schema_conn.commit()

        result = dialog_log_context(_dialogs_schema_conn, dialog_id=2)

        assert result.dialog_id == 2
        assert result.name is None
        assert result.type is None
        assert result.archived is None
        assert result.hidden is None
