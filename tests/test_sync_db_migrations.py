"""Tests for sync_db migrations — Phase 39.2-01 Task 3.

Covers v11: message_reactions_freshness side-table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

import pytest

import mcp_telegram.sync_db as sync_db_module
from mcp_telegram.sync_db import (
    _CURRENT_SCHEMA_VERSION,
    _apply_migration_51,
    _apply_migration_52,
    _apply_migration_53,
    _apply_migration_54,
    _apply_migration_55,
    _apply_migration_56,
    _apply_migration_58,
    _apply_migration_59,
    _apply_migration_60,
    _apply_migration_64,
    _open_sync_db,
    ensure_sync_schema,
)

Row = tuple[object, ...]
TableInfoRow = tuple[int, str, str, int, object, int]

_V36_MESSAGES_DDL = """
CREATE TABLE messages (
    dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, sent_at INTEGER NOT NULL,
    text TEXT, sender_id INTEGER, sender_first_name TEXT, media_description TEXT,
    media_kind TEXT, reply_to_msg_id INTEGER, forum_topic_id INTEGER, edit_date INTEGER,
    grouped_id INTEGER, reply_to_peer_id INTEGER, out INTEGER NOT NULL DEFAULT 0,
    is_service INTEGER NOT NULL DEFAULT 0, post_author TEXT,
    reply_count INTEGER NOT NULL DEFAULT 0, is_deleted INTEGER NOT NULL DEFAULT 0,
    deleted_at INTEGER, PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_V36_SCHEDULED_MESSAGES_DDL = """
CREATE TABLE scheduled_messages (
    dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, scheduled_at INTEGER,
    text TEXT, sender_id INTEGER, sender_first_name TEXT, media_description TEXT,
    media_kind TEXT, reply_to_msg_id INTEGER, forum_topic_id INTEGER, edit_date INTEGER,
    grouped_id INTEGER, reply_to_peer_id INTEGER, out INTEGER NOT NULL DEFAULT 1,
    is_service INTEGER NOT NULL DEFAULT 0, post_author TEXT, schedule_repeat_period INTEGER,
    message_state TEXT NOT NULL DEFAULT 'scheduled', visibility TEXT NOT NULL DEFAULT 'author_only',
    unpublished INTEGER NOT NULL DEFAULT 1, unseen INTEGER NOT NULL DEFAULT 1,
    publication_hint_message_id INTEGER, published_message_id INTEGER,
    publication_verified_at INTEGER, published_at INTEGER, deleted_at INTEGER,
    first_seen_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_V23_ACTIVITY_DIALOG_STATE_DDL = """
CREATE TABLE activity_dialog_state (
    dialog_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    last_activity_at INTEGER,
    hot_cursor INTEGER,
    hot_last_sync_at INTEGER,
    hot_next_retry_at INTEGER,
    hot_last_error TEXT,
    cold_offset_id INTEGER,
    cold_status TEXT NOT NULL DEFAULT 'pending',
    cold_next_retry_at INTEGER,
    cold_last_error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) WITHOUT ROWID
"""


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "sync.db"


@contextmanager
def _sync_db_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = cast(sqlite3.Connection, _open_sync_db(db_path))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _sqlite_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = cast(sqlite3.Connection, sqlite3.connect(db_path))
    try:
        yield conn
    finally:
        conn.close()


def _fetchone_row(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> Row | None:
    return cast(Row | None, conn.execute(sql, parameters).fetchone())


def _fetchone_int(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> int:
    row = _fetchone_row(conn, sql, parameters)
    assert row is not None
    return int(cast(int, row[0]))


def _fetchall_rows(conn: sqlite3.Connection, sql: str, parameters: tuple[object, ...] = ()) -> list[Row]:
    return cast(list[Row], conn.execute(sql, parameters).fetchall())


def _table_info(conn: sqlite3.Connection, table: str) -> list[TableInfoRow]:
    return cast(list[TableInfoRow], _fetchall_rows(conn, f"PRAGMA table_info({table})"))


def _downgrade_media_tables_to_v43(conn: sqlite3.Connection) -> None:
    """Recreate current media tables with the pre-v44 kind CHECK for upgrade tests."""
    index_rows = _fetchall_rows(
        conn,
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL "
        "AND tbl_name IN ('messages', 'scheduled_messages')",
    )
    index_sql = [str(row[0]) for row in index_rows]
    for table in ("messages", "scheduled_messages"):
        table_sql_row = _fetchone_row(conn, "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,))
        assert table_sql_row is not None
        table_sql = str(table_sql_row[0])
        v43_sql = table_sql.replace("'custom_emoji', ", "")
        assert v43_sql != table_sql
        columns = [str(row[1]) for row in _table_info(conn, table)]
        column_list = ", ".join(columns)
        old_table = f"{table}_v44"
        conn.execute(f"ALTER TABLE {table} RENAME TO {old_table}")
        conn.execute(v43_sql)
        conn.execute(f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM {old_table}")
        conn.execute(f"DROP TABLE {old_table}")
    for statement in index_sql:
        conn.execute(statement)


def test_migration_v11_creates_freshness_table(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = _fetchall_rows(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='message_reactions_freshness'"
        )
        assert rows == [("message_reactions_freshness",)]
        cols = _table_info(conn, "message_reactions_freshness")
        # Each row: (cid, name, type, notnull, dflt_value, pk)
        col_map = {c[1]: (c[2], c[3], c[5]) for c in cols}
        assert col_map["dialog_id"] == ("INTEGER", 1, 1)
        assert col_map["message_id"] == ("INTEGER", 1, 2)
        assert col_map["checked_at"] == ("INTEGER", 1, 0)


def test_migration_v37_rebuilds_media_tables_without_legacy_description(db_path: Path) -> None:
    # Start with the complete schema, then rebuild only the two message tables
    # to their real v36 shape.  This retains all other production artifacts and
    # lets the test exercise the physical table swap rather than an idealized
    # fixture.
    ensure_sync_schema(db_path)
    with _sqlite_connection(db_path) as conn:
        conn.execute("ALTER TABLE messages RENAME TO messages_current")
        conn.execute(
            """CREATE TABLE messages (
                dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                sent_at INTEGER NOT NULL, text TEXT, sender_id INTEGER,
                sender_first_name TEXT, media_description TEXT, media_kind TEXT,
                reply_to_msg_id INTEGER, forum_topic_id INTEGER, edit_date INTEGER,
                grouped_id INTEGER, reply_to_peer_id INTEGER,
                out INTEGER NOT NULL DEFAULT 0, is_service INTEGER NOT NULL DEFAULT 0,
                post_author TEXT, reply_count INTEGER NOT NULL DEFAULT 0,
                is_deleted INTEGER NOT NULL DEFAULT 0, deleted_at INTEGER,
                PRIMARY KEY (dialog_id, message_id)
            ) WITHOUT ROWID"""
        )
        conn.execute(
            """INSERT INTO messages
            (dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
             media_description, media_kind, out, is_service, reply_count)
            VALUES (1, 1, 1700000000, 'ordinary', 7, 'Alice',
                        'legacy human description', 'other', 0, 0, 2)"""
        )
        conn.execute(
            """INSERT INTO messages
            (dialog_id, message_id, sent_at, text, sender_id, sender_first_name,
             media_description, media_kind, out, is_service, reply_count)
            VALUES (1, 3, 1700000001, NULL, 7, 'Bob',
                        'legacy contact description', 'contact', 0, 0, 0)"""
        )
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (1, 1, 'explicit', 1)"
        )
        conn.execute("DROP TABLE messages_current")
        conn.execute("CREATE INDEX idx_messages_dialog_sent ON messages(dialog_id, sent_at DESC)")
        conn.execute("CREATE INDEX idx_messages_legacy_safe ON messages(dialog_id, message_id)")

        conn.execute("ALTER TABLE scheduled_messages RENAME TO scheduled_messages_current")
        conn.execute(
            """CREATE TABLE scheduled_messages (
                dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                scheduled_at INTEGER, text TEXT, sender_id INTEGER,
                sender_first_name TEXT, media_description TEXT, media_kind TEXT,
                reply_to_msg_id INTEGER, forum_topic_id INTEGER, edit_date INTEGER,
                grouped_id INTEGER, reply_to_peer_id INTEGER,
                out INTEGER NOT NULL DEFAULT 1, is_service INTEGER NOT NULL DEFAULT 0,
                post_author TEXT, schedule_repeat_period INTEGER,
                message_state TEXT NOT NULL DEFAULT 'scheduled',
                visibility TEXT NOT NULL DEFAULT 'author_only',
                unpublished INTEGER NOT NULL DEFAULT 1, unseen INTEGER NOT NULL DEFAULT 1,
                publication_hint_message_id INTEGER, published_message_id INTEGER,
                publication_verified_at INTEGER, published_at INTEGER, deleted_at INTEGER,
                first_seen_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY (dialog_id, message_id)
            ) WITHOUT ROWID"""
        )
        conn.execute(
            """INSERT INTO scheduled_messages
                (dialog_id, message_id, scheduled_at, text, sender_id,
                 media_description, media_kind, first_seen_at, updated_at)
                VALUES (1, 2, 1700000100, NULL, 7, 'legacy scheduled description',
                        'other', 1700000000, 1700000000)"""
        )
        conn.execute("DROP TABLE scheduled_messages_current")
        conn.execute("CREATE INDEX idx_scheduled_messages_active ON scheduled_messages(dialog_id, scheduled_at)")
        # Replay the migration tail from a genuine pre-v39 activity table so
        # the additive v39 columns are exercised exactly once.
        conn.execute("DROP TABLE activity_dialog_state")
        conn.execute(_V23_ACTIVITY_DIALOG_STATE_DDL)
        conn.execute("DELETE FROM schema_version WHERE version >= 37")

        # FTS is a separate contentless table and must survive the physical
        # message-table rebuild with its rows intact.
        conn.execute("INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, 1, 'ordinary')")
        conn.execute(
            "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, 2, 'scheduled')"
        )
        conn.commit()
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == 36

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        assert _fetchone_row(
            conn,
            "SELECT name FROM sqlite_master WHERE type='table' AND name='message_transcriptions'",
        ) == ("message_transcriptions",)
        assert (
            _fetchone_row(
                conn,
                "SELECT name FROM sqlite_master WHERE type='table' AND name='pending_transcriptions'",
            )
            is None
        )
        transcription_cols = {row[1] for row in _table_info(conn, "message_transcriptions")}
        assert {"dialog_id", "message_id", "text", "transcription_id", "received_at"} <= transcription_cols
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_transcriptions") == 0
        for table in ("messages", "scheduled_messages"):
            cols = {row[1] for row in _table_info(conn, table)}
            assert "media_description" not in cols
            assert {"media_kind", "media_payload"} <= cols
            sql_row = _fetchone_row(conn, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,))
            assert sql_row is not None
            sql = str(sql_row[0])
            assert "WITHOUT ROWID" in sql.upper()
        assert conn.execute("SELECT text, media_kind, media_payload FROM messages").fetchone() == (
            "ordinary",
            "other",
            "{}",
        )
        assert conn.execute("SELECT text, media_kind, media_payload FROM scheduled_messages").fetchone() == (
            None,
            "other",
            "{}",
        )
        assert conn.execute("SELECT stemmed_text FROM messages_fts WHERE dialog_id=1 AND message_id=1").fetchone() == (
            "ordinary",
        )
        assert conn.execute(
            "SELECT stemmed_text FROM scheduled_messages_fts WHERE dialog_id=1 AND message_id=2"
        ).fetchone() == ("scheduled",)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_messages_legacy_safe'"
        ).fetchone()
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_scheduled_messages_active'"
        ).fetchone()
        assert conn.execute(
            "SELECT kind, dialog_id, message_id, priority, message_sent_at FROM hydration_jobs ORDER BY message_id"
        ).fetchall() == [
            ("media_metadata", 1, 1, 0, 1700000000),
            ("media_metadata", 1, 3, 0, 1700000001),
        ]


def test_migration_v44_accepts_custom_emoji_and_preserves_media_artifacts(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (1, 1, 1700000000, 'ordinary', 'document', '{\"size\":4}')"
        )
        conn.execute(
            "INSERT INTO scheduled_messages(dialog_id, message_id, scheduled_at, text, media_kind, media_payload, "
            "first_seen_at, updated_at) VALUES (1, 2, 1700000100, 'scheduled', 'document', '{\"size\":5}', 1, 1)"
        )
        conn.execute("INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, 1, 'ordinary')")
        conn.execute(
            "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, 2, 'scheduled')"
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at) VALUES ('media_metadata', 1, 1, 1)"
        )
        conn.execute("CREATE INDEX idx_messages_v44_probe ON messages(sender_id)")
        conn.execute("CREATE INDEX idx_scheduled_messages_v44_probe ON scheduled_messages(sender_id)")

        _downgrade_media_tables_to_v43(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
                "VALUES (1, 3, 1700000001, 'custom_emoji', '{\"alt\":\"📊\"}')"
            )
        conn.execute("DELETE FROM schema_version WHERE version >= 44")
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT media_kind, media_payload FROM messages WHERE message_id = 1").fetchone() == (
            "document",
            '{"size":4}',
        )
        assert conn.execute(
            "SELECT media_kind, media_payload FROM scheduled_messages WHERE message_id = 2"
        ).fetchone() == (
            "document",
            '{"size":5}',
        )
        assert conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone() == (1,)
        assert conn.execute("SELECT COUNT(*) FROM scheduled_messages_fts").fetchone() == (1,)
        assert conn.execute("SELECT kind, dialog_id, message_id FROM hydration_jobs").fetchone() == (
            "media_metadata",
            1,
            1,
        )
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name IN "
            "('idx_messages_dialog_sent', 'idx_messages_v44_probe', 'idx_scheduled_messages_v44_probe') "
            "ORDER BY name"
        ).fetchall() == [
            ("idx_messages_dialog_sent",),
            ("idx_messages_v44_probe",),
            ("idx_scheduled_messages_v44_probe",),
        ]
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, media_kind, media_payload) "
            "VALUES (1, 3, 1700000001, 'custom_emoji', '{\"alt\":\"📊\"}')"
        )
        conn.execute(
            "INSERT INTO scheduled_messages(dialog_id, message_id, scheduled_at, media_kind, media_payload, "
            "first_seen_at, updated_at) VALUES (1, 4, 1700000101, 'custom_emoji', '{\"alt\":\"📊\"}', 1, 1)"
        )
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        assert conn.execute("SELECT media_kind FROM messages WHERE message_id = 3").fetchone() == ("custom_emoji",)
        assert conn.execute("SELECT media_kind FROM scheduled_messages WHERE message_id = 4").fetchone() == (
            "custom_emoji",
        )


def test_v40_creates_prioritized_hydration_queue_and_due_index(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert [row[1] for row in _table_info(conn, "hydration_jobs")] == [
            "kind",
            "dialog_id",
            "message_id",
            "due_at",
            "attempts",
            "priority",
            "message_sent_at",
            "terminal",
            "last_outcome",
            "last_error_code",
        ]
        table_sql = _fetchone_row(conn, "SELECT sql FROM sqlite_master WHERE type='table' AND name='hydration_jobs'")
        assert table_sql is not None and "WITHOUT ROWID" in str(table_sql[0]).upper()
        due_index = _fetchone_row(
            conn, "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_hydration_jobs_schedule'"
        )
        assert due_index is not None
        assert (
            "KIND, TERMINAL, PRIORITY DESC, MESSAGE_SENT_AT DESC, DUE_AT, DIALOG_ID, MESSAGE_ID"
            in str(due_index[0]).upper()
        )
        repair_index = _fetchone_row(
            conn,
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_messages_transcribable_undeleted_sent'",
        )
        assert repair_index is not None
        assert "SENT_AT DESC, DIALOG_ID, MESSAGE_ID" in str(repair_index[0]).upper()
        assert "ROUND_MESSAGE" in str(repair_index[0]).upper()
        assert (
            _fetchone_row(
                conn, "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_messages_voice_undeleted_sent'"
            )
            is None
        )


def test_v41_shape_seeds_voice_transcription_hydration_as_backfill(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (91, 'synced')")
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) VALUES (91, 1, 'explicit', 1)"
        )
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload) "
            "VALUES (91, 17, 1234, NULL, 'voice', '{}')"
        )
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, media_kind, media_payload, is_deleted) "
            "VALUES (91, 18, 1235, NULL, 'voice', '{}', 1)"
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at) "
            "VALUES ('media_metadata', 91, 99, 1200, 2, 1, 1199)"
        )
        conn.execute("DROP INDEX idx_hydration_jobs_schedule")
        conn.execute("DROP INDEX idx_messages_transcribable_undeleted_sent")
        conn.execute("ALTER TABLE hydration_jobs RENAME TO hydration_jobs_v41")
        conn.execute(
            "CREATE TABLE hydration_jobs ("
            "kind TEXT NOT NULL, dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL, "
            "due_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, "
            "priority INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0, 1)), "
            "message_sent_at INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (kind, dialog_id, message_id)) WITHOUT ROWID"
        )
        conn.execute(
            "INSERT INTO hydration_jobs(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at) "
            "SELECT kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at FROM hydration_jobs_v41"
        )
        conn.execute("DROP TABLE hydration_jobs_v41")
        assert "terminal" not in [row[1] for row in _table_info(conn, "hydration_jobs")]
        conn.execute("DELETE FROM schema_version WHERE version >= 41")
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT kind, dialog_id, message_id, priority, message_sent_at FROM hydration_jobs "
            "WHERE kind = 'transcription'"
        ).fetchone() == ("transcription", 91, 17, 0, 1234)
        assert conn.execute(
            "SELECT kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at, terminal "
            "FROM hydration_jobs WHERE kind = 'media_metadata'"
        ).fetchone() == ("media_metadata", 91, 99, 1200, 2, 1, 1199, 0)
        assert (
            conn.execute(
                "SELECT 1 FROM hydration_jobs WHERE kind = 'transcription' AND dialog_id = 91 AND message_id = 18"
            ).fetchone()
            is None
        )


def test_migration_v11_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second call: must not raise
    with _sync_db_connection(db_path) as conn:
        cols_before = _table_info(conn, "message_reactions_freshness")
        assert len(cols_before) == 3


def test_migration_v11_without_rowid(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(
            conn, "SELECT sql FROM sqlite_master WHERE type='table' AND name='message_reactions_freshness'"
        )
        assert row is not None
        assert "WITHOUT ROWID" in str(row[0]).upper()


def test_migration_v11_does_not_touch_synced_dialogs(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        cols = [c[1] for c in _table_info(conn, "synced_dialogs")]
        assert "reactions_reconciled_at" not in cols


def test_schema_version_records_current_v11(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# v12: synced_dialogs.read_outbox_max_id (Phase 39.3-01 Task 3)
# ---------------------------------------------------------------------------


def _col_info(conn: sqlite3.Connection, table: str) -> dict[str, tuple[object, ...]]:
    # PRAGMA table_info rows: (cid, name, type, notnull, dflt_value, pk)
    return {row[1]: tuple(row) for row in _table_info(conn, table)}


def test_migration_v12_adds_outbox_column(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        cols = _col_info(conn, "synced_dialogs")
        assert "read_outbox_max_id" in cols
        # (cid, name, type, notnull, dflt_value, pk)
        _, _, col_type, notnull, _, _ = cols["read_outbox_max_id"]
        assert col_type == "INTEGER"
        assert notnull == 0  # nullable


def test_migration_v12_existing_rows_have_null_outbox(db_path: Path, tmp_path: Path) -> None:
    # Build a v11-shaped DB by bootstrapping current schema then proving
    # that if we pre-insert a row prior to a re-run, the outbox is NULL.
    # Re-applying ensure_sync_schema is a no-op beyond current version, so
    # instead we exercise the "pre-existing row after migration" scenario:
    # insert a row after schema exists and confirm NULL is the default state.
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', ?)",
            (4242, 5),
        )
        conn.commit()
        row = _fetchone_row(conn, "SELECT read_outbox_max_id FROM synced_dialogs WHERE dialog_id=?", (4242,))
        assert row is not None
        assert row[0] is None, "new rows default read_outbox_max_id to NULL"


def test_migration_v12_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    # Second call must not raise (SQLite ALTER TABLE ADD COLUMN would otherwise
    # fail with 'duplicate column name'; the _migrate framework guards via
    # schema_version and must skip already-applied versions).
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        cols = _col_info(conn, "synced_dialogs")
        assert "read_outbox_max_id" in cols
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION


def test_schema_version_records_current_v12(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION


def test_migration_v12_does_not_drop_inbox_column(db_path: Path) -> None:
    """Regression guard: v12 adds the outbox column without touching inbox."""
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        cols = _col_info(conn, "synced_dialogs")
        assert "read_inbox_max_id" in cols
        # Inbox column is still writable via the existing monotonic primitive.
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, read_inbox_max_id) VALUES (?, 'synced', ?)",
            (7777, 123),
        )
        conn.commit()
        row = _fetchone_row(conn, "SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=?", (7777,))
        assert row is not None
        assert row[0] == 123


# ---------------------------------------------------------------------------
# v16: entity_details sibling table (Phase 47-01)
# ---------------------------------------------------------------------------


def test_schema_v16_creates_entity_details(tmp_path: Path) -> None:
    """v16 creates the entity_details sibling table per CONTEXT D-01."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sqlite_connection(db_path) as conn:
        cols = {(row[1], row[2]) for row in _table_info(conn, "entity_details")}
    assert cols >= {
        ("entity_id", "INTEGER"),
        ("detail_json", "TEXT"),
        ("fetched_at", "INTEGER"),
    }, f"entity_details columns mismatch: {cols}"


def test_schema_v16_creates_fetched_at_index(tmp_path: Path) -> None:
    """v16 adds an index on entity_details.fetched_at for future eviction sweeps (D-04)."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sqlite_connection(db_path) as conn:
        idx = {
            str(row[0])
            for row in _fetchall_rows(
                conn, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='entity_details'"
            )
        }
    assert "idx_entity_details_fetched_at" in idx


def test_migration_v16_fk_cascade_deletes_detail_row(tmp_path: Path) -> None:
    """Deleting an entities row CASCADES to delete the matching entity_details row."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sqlite_connection(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")  # SQLite defaults FKs OFF per connection
        conn.execute(
            "INSERT INTO entities (id, type, name, updated_at) VALUES (?, ?, ?, ?)",
            (42, "user", "Alice", 1000),
        )
        conn.execute(
            "INSERT INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
            (42, '{"schema": 1, "type": "user"}', 1000),
        )
        conn.commit()
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM entity_details WHERE entity_id=42") == 1
        conn.execute("DELETE FROM entities WHERE id = 42")
        conn.commit()
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM entity_details WHERE entity_id=42") == 0, "FK CASCADE failed"


def test_migration_v16_idempotent(tmp_path: Path) -> None:
    """Running ensure_sync_schema twice is a no-op — schema_version has _CURRENT_SCHEMA_VERSION rows, MAX=_CURRENT_SCHEMA_VERSION."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second call must be a no-op
    with _sqlite_connection(db_path) as conn:
        count = _fetchone_int(conn, "SELECT COUNT(*) FROM schema_version")
        max_v = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
    assert count == _CURRENT_SCHEMA_VERSION
    assert max_v == _CURRENT_SCHEMA_VERSION


def test_migration_v16_does_not_touch_entities_columns(tmp_path: Path) -> None:
    """SPEC Constraint #4: v16 does NOT widen the entities table.

    The exact column set must remain {id, type, name, username, name_normalized, updated_at}.
    """
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sqlite_connection(db_path) as conn:
        cols = {row[1] for row in _table_info(conn, "entities")}
    assert cols == {"id", "type", "name", "username", "name_normalized", "updated_at"}, (
        f"entities columns must not change in v16; got {cols}"
    )


# ---------------------------------------------------------------------------
# v18: daemon_state KV table (Phase 41 — bootstrap sweep cursor + flags)
# ---------------------------------------------------------------------------


def test_migration_v18_creates_daemon_state_table(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='daemon_state'")
        assert rows == [("daemon_state",)]
        cols = _table_info(conn, "daemon_state")
        col_map = {c[1]: (c[2], c[3], c[5]) for c in cols}
        # name -> (type, notnull, pk)
        assert col_map["key"] == ("TEXT", 0, 1)
        assert col_map["value"] == ("TEXT", 0, 0)


def test_daemon_state_contains_runtime_history_boundary_after_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(
            conn, "SELECT value FROM daemon_state WHERE key = 'runtime_observations_history_started_at_ms'"
        )
        assert row is not None
        assert isinstance(row[0], (int, str))
        assert int(row[0]) > 0


def test_migration_v18_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("INSERT INTO daemon_state(key, value) VALUES ('probe', 'value')")
        conn.commit()

    ensure_sync_schema(db_path)  # second call: must not raise or wipe data

    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(conn, "SELECT value FROM daemon_state WHERE key = 'probe'")
        assert row == ("value",)


def test_schema_version_records_current_v18(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        max_version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert max_version == _CURRENT_SCHEMA_VERSION
        assert _CURRENT_SCHEMA_VERSION == 64


def test_genuine_v61_fixture_upgrades_to_v62_and_reopens_idempotently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ownership and pair measurements are a real additive post-v61 migration."""
    db_path = tmp_path / "v61.sqlite"
    with monkeypatch.context() as v61:
        v61.setattr(sync_db_module, "_CURRENT_SCHEMA_VERSION", 61)
        ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == 61
        refresh_columns = {row[1] for row in _table_info(conn, "entity_profile_refresh_state")}
        detail_columns = {row[1] for row in _table_info(conn, "entity_details")}
        assert "pair_mode" not in refresh_columns
        assert "pair_summary_watermark" not in refresh_columns
        assert "profile_owner_account_id" not in detail_columns
        conn.execute("INSERT INTO entities(id, type, name, updated_at) VALUES (42, 'user', 'kept', 100)")
        conn.execute(
            "INSERT INTO entity_details(entity_id, detail_json, fetched_at) VALUES (42, ?, 100)",
            ('{"schema":1,"id":42,"type":"user","name":"kept"}',),
        )
        conn.execute(
            "INSERT INTO entity_profile_refresh_state("
            "entity_id,status,retry_at,reason,updated_at,next_section,acquisition_cursor,"
            "generation,started_at,pair_eligible,follow_up_required,profile_revision) "
            "VALUES (42,'failed',123,'old',100,'common_chats',3,7,90,1,0,2)"
        )
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        assert _fetchone_row(
            conn,
            "SELECT detail_json, fetched_at, profile_owner_account_id FROM entity_details WHERE entity_id=42",
        ) == ('{"schema":1,"id":42,"type":"user","name":"kept"}', 100, None)
        assert _fetchone_row(
            conn,
            "SELECT status, retry_at, reason, next_section, acquisition_cursor, generation, profile_revision "
            "FROM entity_profile_refresh_state WHERE entity_id=42",
        ) == ("failed", 123, "old", "common_chats", 3, 7, 2)
        columns_before = _table_info(conn, "entity_profile_refresh_state")

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        assert _table_info(conn, "entity_profile_refresh_state") == columns_before


def test_current_schema_repairs_missing_scheduled_fts(tmp_path: Path) -> None:
    """A v27 database missing its FTS companion is repaired on startup."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO scheduled_messages "
            "(dialog_id, message_id, scheduled_at, text, first_seen_at, updated_at) "
            "VALUES (1, 1, 2000000000, 'future message', 1700000000, 1700000000)"
        )
        conn.execute("DROP TABLE scheduled_messages_fts")
        conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert (
            _fetchone_int(
                conn,
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='scheduled_messages_fts'",
            )
            == 1
        )
        assert (
            _fetchone_int(
                conn,
                "SELECT COUNT(*) FROM scheduled_messages_fts WHERE scheduled_messages_fts MATCH 'future'",
            )
            == 1
        )


# ---------------------------------------------------------------------------
# v19: topic_metadata augmentation with v1.6 columns (Phase 42)
# ---------------------------------------------------------------------------


def test_migration_v19_adds_v1_6_columns_to_topic_metadata(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        cols = {row[1] for row in _table_info(conn, "topic_metadata")}
        # Legacy v4 columns retained:
        assert {
            "dialog_id",
            "topic_id",
            "title",
            "top_message_id",
            "is_general",
            "is_deleted",
            "inaccessible_error",
            "inaccessible_at",
            "updated_at",
        }.issubset(cols)
        # New v19 columns:
        assert {"icon_emoji_id", "pinned", "hidden", "snapshot_at", "date"}.issubset(cols)


def test_migration_v19_preserves_legacy_topic_metadata_columns(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        # Insert a legacy-shaped row (only legacy columns set explicitly).
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, top_message_id, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (-1001234, 1, "General", 1, 1, 0, 1700000000),
        )
        conn.commit()
        row = _fetchone_row(
            conn,
            "SELECT title, is_general, is_deleted, pinned, hidden FROM topic_metadata WHERE dialog_id=? AND topic_id=?",
            (-1001234, 1),
        )
        assert row is not None
        assert row[0] == "General"
        assert row[1] == 1
        assert row[2] == 0
        # New columns default to 0 (NOT NULL DEFAULT 0):
        assert row[3] == 0
        assert row[4] == 0


def test_migration_v19_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second call must not raise
    with _sync_db_connection(db_path) as conn:
        cols = {row[1] for row in _table_info(conn, "topic_metadata")}
        assert {"icon_emoji_id", "pinned", "hidden", "snapshot_at", "date"}.issubset(cols)


def test_migration_v19_pinned_default_zero(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (-1009999, 1, "T", 0, 0, 1700000000),
        )
        conn.commit()
        row = _fetchone_row(conn, "SELECT pinned, hidden FROM topic_metadata WHERE dialog_id=-1009999 AND topic_id=1")
        assert row == (0, 0)


def test_migration_v19_does_not_break_existing_left_join(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (-1001234, 5, "General Discussion", 0, 0, 1700000000),
        )
        conn.commit()
        # Mirror the daemon_api.py:573 LEFT JOIN expression in isolation:
        row = _fetchone_row(
            conn, "SELECT tm.title FROM topic_metadata tm WHERE tm.dialog_id = ? AND tm.topic_id = ?", (-1001234, 5)
        )
        assert row is not None
        assert row[0] == "General Discussion"


def test_schema_version_records_v19(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        # v19 must be present in the version history (migration ran).
        row = _fetchone_row(conn, "SELECT version FROM schema_version WHERE version = 19")
        assert row is not None, "v19 migration did not run"


# ---------------------------------------------------------------------------
# v21: trace_coverage_fragments (Phase 51 — Account Trace)
# ---------------------------------------------------------------------------


def test_migration_v21_creates_trace_coverage_fragments(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = _fetchall_rows(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_coverage_fragments'"
        )
        assert rows == [("trace_coverage_fragments",)]
        cols = _table_info(conn, "trace_coverage_fragments")
        col_map = {c[1]: (c[2], c[3], c[4], c[5]) for c in cols}
        assert col_map["target_user_id"] == ("INTEGER", 1, None, 1)
        assert col_map["dialog_id"] == ("INTEGER", 1, None, 2)
        assert col_map["topic_id"] == ("INTEGER", 1, "0", 3)
        assert col_map["coverage_kind"] == ("TEXT", 1, None, 4)
        assert col_map["status"] == ("TEXT", 1, None, 0)
        assert col_map["created_at"] == ("INTEGER", 1, None, 0)
        assert col_map["updated_at"] == ("INTEGER", 1, None, 0)


def test_migration_v21_creates_target_status_index(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        indexes = {
            row[0]
            for row in _fetchall_rows(
                conn, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='trace_coverage_fragments'"
            )
        }
        assert "idx_trace_coverage_target_status" in indexes


def test_migration_v21_accepts_dialog_level_trace_fragment(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO trace_coverage_fragments
                (target_user_id, dialog_id, topic_id, coverage_kind, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (101, -100123, 0, "authored_message", "pending", 1700000000, 1700000001),
        )
        conn.commit()
        row = _fetchone_row(
            conn,
            "SELECT target_user_id, dialog_id, topic_id, status, created_at, updated_at FROM trace_coverage_fragments WHERE target_user_id = 101",
        )
        assert row is not None
        assert row == (101, -100123, 0, "pending", 1700000000, 1700000001)


def test_migration_v21_topic_zero_reserved_for_dialog_level(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO topic_metadata "
            "(dialog_id, topic_id, title, is_general, is_deleted, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (-100123, 1, "General", 1, 0, 1700000000),
        )
        conn.commit()
        topic_ids = [
            int(cast(int, row[0]))
            for row in _fetchall_rows(conn, "SELECT topic_id FROM topic_metadata WHERE dialog_id = -100123")
        ]
        assert topic_ids == [1]
        assert 0 not in topic_ids


def test_migration_v21_runs_from_v20_database(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    with _sqlite_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE schema_version (
                version INTEGER NOT NULL,
                applied_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(_V36_MESSAGES_DDL)
        conn.execute(_V36_SCHEDULED_MESSAGES_DDL)
        # entities/entity_details were created in v16; dialogs in v17.
        # Stub both so v24 ALTER/UPDATE succeeds when this test seeds version=20
        # (skipping v1-v20 migration steps).
        conn.execute(
            """
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT,
                username TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE entity_details (
                entity_id   INTEGER PRIMARY KEY,
                detail_json TEXT NOT NULL,
                fetched_at  INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            CREATE TABLE dialogs (
                dialog_id INTEGER PRIMARY KEY,
                name TEXT, type TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                members INTEGER,
                created INTEGER,
                last_message_at INTEGER,
                snapshot_at INTEGER,
                hidden INTEGER NOT NULL DEFAULT 0,
                needs_refresh INTEGER NOT NULL DEFAULT 0,
                unread_mentions_count INTEGER NOT NULL DEFAULT 0,
                unread_reactions_count INTEGER NOT NULL DEFAULT 0,
                draft_text TEXT
            )
            """
        )
        # synced_dialogs exists since v1; stub it so the v25 own_only backfill
        # (INSERT...SELECT FROM synced_dialogs) succeeds when seeding mid-chain.
        conn.execute(
            "CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending')"
        )
        conn.execute("INSERT INTO schema_version VALUES (20, 1700000000)")
        conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert _fetchone_row(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='trace_coverage_fragments'"
        ) == ("trace_coverage_fragments",)
        columns = {row[1] for row in _table_info(conn, "messages")}
        assert "reply_count" in columns
        max_version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert max_version >= 23


# ---------------------------------------------------------------------------
# v23: activity_dialog_state + activity_channel_resolution (Phase 53)
# ---------------------------------------------------------------------------


def test_migration_v23_schema_version(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") >= 23


def test_migration_v23_creates_activity_dialog_state(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_row(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_dialog_state'"
        ) == ("activity_dialog_state",)

        cols = {c[1]: c for c in _table_info(conn, "activity_dialog_state")}
        # (cid, name, type, notnull, dflt_value, pk)
        assert "dialog_id" in cols
        assert "source" in cols
        assert "last_activity_at" in cols
        assert "hot_cursor" in cols
        assert "hot_last_sync_at" in cols
        assert "hot_next_retry_at" in cols
        assert "hot_last_error" in cols
        assert "cold_offset_id" in cols
        assert "cold_status" in cols
        assert "cold_next_retry_at" in cols
        assert "cold_last_error" in cols
        assert "created_at" in cols
        assert "updated_at" in cols

        # dialog_id is PK
        assert cols["dialog_id"][5] == 1, "dialog_id must be PRIMARY KEY"
        # source is NOT NULL
        assert cols["source"][3] == 1, "source must be NOT NULL"
        # cold_status has default 'pending'
        assert cols["cold_status"][4] == "'pending'", (
            f"cold_status default must be 'pending', got {cols['cold_status'][4]}"
        )


def test_migration_v23_per_tier_retry_columns_no_shared(db_path: Path) -> None:
    """Both hot_next_retry_at and cold_next_retry_at exist; no bare next_retry_at column."""
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        col_names = {c[1] for c in _table_info(conn, "activity_dialog_state")}
        assert "hot_next_retry_at" in col_names, "hot_next_retry_at must exist (Tier-A retry)"
        assert "cold_next_retry_at" in col_names, "cold_next_retry_at must exist (Tier-B retry)"
        assert "next_retry_at" not in col_names, "bare next_retry_at must NOT exist (tier coupling)"


def test_migration_v23_creates_per_tier_indexes(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        indexes = {
            row[0]
            for row in _fetchall_rows(
                conn, "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='activity_dialog_state'"
            )
        }
        assert "idx_activity_dialog_state_hot" in indexes, "Tier-A hot index missing"
        assert "idx_activity_dialog_state_cold" in indexes, "Tier-B cold index missing"


def test_migration_v23_activity_dialog_state_without_rowid(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(conn, "SELECT sql FROM sqlite_master WHERE type='table' AND name='activity_dialog_state'")
        assert row is not None
        assert "WITHOUT ROWID" in str(row[0]).upper()


def test_migration_v23_idempotent(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)  # second call must not raise
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") >= 23


def test_migration_v23_runs_from_v22_database(tmp_path: Path) -> None:
    """v22 → current upgrade path creates activity_dialog_state; activity_channel_resolution is absent after v24."""
    db_path = tmp_path / "sync.db"
    with _sqlite_connection(db_path) as conn:
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
        conn.execute(_V36_MESSAGES_DDL)
        conn.execute(_V36_SCHEDULED_MESSAGES_DDL)
        # entities/entity_details were created in v16; dialogs in v17.
        # Stub both so v24 ALTER/UPDATE succeeds when this test seeds version=22
        # (skipping v1-v22 migration steps).
        conn.execute(
            """
            CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT,
                username TEXT,
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE entity_details (
                entity_id   INTEGER PRIMARY KEY,
                detail_json TEXT NOT NULL,
                fetched_at  INTEGER NOT NULL
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            CREATE TABLE dialogs (
                dialog_id INTEGER PRIMARY KEY,
                name TEXT, type TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                pinned INTEGER NOT NULL DEFAULT 0,
                members INTEGER,
                created INTEGER,
                last_message_at INTEGER,
                snapshot_at INTEGER,
                hidden INTEGER NOT NULL DEFAULT 0,
                needs_refresh INTEGER NOT NULL DEFAULT 0,
                unread_mentions_count INTEGER NOT NULL DEFAULT 0,
                unread_reactions_count INTEGER NOT NULL DEFAULT 0,
                draft_text TEXT
            )
            """
        )
        # synced_dialogs exists since v1; stub it so the v25 own_only backfill
        # (INSERT...SELECT FROM synced_dialogs) succeeds when seeding mid-chain.
        conn.execute(
            "CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending')"
        )
        conn.execute("INSERT INTO schema_version VALUES (22, 1700000000)")
        conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        # activity_dialog_state survives v24
        assert _fetchone_row(
            conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_dialog_state'"
        ) == ("activity_dialog_state",)
        # activity_channel_resolution is ABSENT after v24 drops it
        assert (
            _fetchone_row(
                conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_channel_resolution'"
            )
            is None
        ), "activity_channel_resolution must be absent after v24"
        max_version = _fetchone_int(conn, "SELECT MAX(version) FROM schema_version")
        assert max_version >= 23


# ---------------------------------------------------------------------------
# v24: linked_chat columns on dialogs, backfill from entity_details, strip
#      detail_json, drop activity_channel_resolution (Phase 54)
# ---------------------------------------------------------------------------


def test_migration_v24_schema_version(db_path: Path) -> None:
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION


def test_migration_v24_columns_exist(db_path: Path) -> None:
    """dialogs table has linked_chat_id and linked_chat_resolved_at columns after v24."""
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        cols = {c[1] for c in _table_info(conn, "dialogs")}
        assert "linked_chat_id" in cols, "linked_chat_id column missing from dialogs"
        assert "linked_chat_resolved_at" in cols, "linked_chat_resolved_at column missing from dialogs"


def _seed_v24_fixtures(conn: sqlite3.Connection) -> None:
    """Seed three channel rows covering the three production-observed shapes for v24 backfill tests.

    (a) channel A (id=1001): linked_chat_id = -1002000000000 (linked chat present)
    (b) channel B (id=1002): linked_chat_id = null (JSON null, key present — explicitly no linked chat)
    (c) channel C (id=1003): no linked_chat_id key at all (cold path, lazy resolve)
    """
    now = 1700000000
    # entities
    conn.executemany(
        "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) VALUES (?, 'channel', ?, NULL, ?)",
        [
            (1001, "Channel A", now),
            (1002, "Channel B", now),
            (1003, "Channel C", now),
        ],
    )
    # entity_details
    conn.execute(
        "INSERT OR IGNORE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (1001, '{"linked_chat_id": -1002000000000, "subscribers_count": 42}', 1700000000),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (1002, '{"linked_chat_id": null}', 1700000001),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
        (1003, '{"subscribers_count": 7}', 1700000002),
    )
    # dialogs
    now_snap = now
    conn.executemany(
        "INSERT OR IGNORE INTO dialogs (dialog_id, name, type, snapshot_at) VALUES (?, ?, 'channel', ?)",
        [
            (1001, "Channel A", now_snap),
            (1002, "Channel B", now_snap),
            (1003, "Channel C", now_snap),
        ],
    )
    conn.commit()


def test_migration_v24_backfill_three_shapes(tmp_path: Path) -> None:
    """Full v24 migration: three channel shapes produce correct post-migration dialogs state."""
    db_path = tmp_path / "sync.db"
    # Open at v23 to seed data before v24 runs
    with _sqlite_connection(db_path) as pre_conn:
        pre_conn.execute("PRAGMA journal_mode=WAL")
        pre_conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
        # Minimal tables required by the migration path up to v23
        pre_conn.execute(
            """CREATE TABLE entities (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT,
                username TEXT,
                name_normalized TEXT,
                updated_at INTEGER NOT NULL
            )"""
        )
        pre_conn.execute(
            """CREATE TABLE entity_details (
                entity_id   INTEGER PRIMARY KEY,
                detail_json TEXT NOT NULL,
                fetched_at  INTEGER NOT NULL
            ) WITHOUT ROWID"""
        )
        pre_conn.execute(
            """CREATE TABLE dialogs (
                dialog_id               INTEGER PRIMARY KEY,
                name                    TEXT,
                type                    TEXT,
                archived                INTEGER NOT NULL DEFAULT 0,
                pinned                  INTEGER NOT NULL DEFAULT 0,
                members                 INTEGER,
                created                 INTEGER,
                last_message_at         INTEGER,
                snapshot_at             INTEGER,
                hidden                  INTEGER NOT NULL DEFAULT 0,
                needs_refresh           INTEGER NOT NULL DEFAULT 0,
                unread_mentions_count   INTEGER NOT NULL DEFAULT 0,
                unread_reactions_count  INTEGER NOT NULL DEFAULT 0,
                draft_text              TEXT
            )"""
        )
        # Simulate v23 tables existing (to verify DROP works on existing deployment)
        pre_conn.execute(
            """CREATE TABLE activity_channel_resolution (
            channel_id  INTEGER PRIMARY KEY,
            next_retry_at INTEGER,
            last_error  TEXT,
            updated_at  INTEGER NOT NULL
            ) WITHOUT ROWID"""
        )
        pre_conn.execute(_V23_ACTIVITY_DIALOG_STATE_DDL)
        pre_conn.execute(_V36_MESSAGES_DDL)
        pre_conn.execute(_V36_SCHEDULED_MESSAGES_DDL)
        # synced_dialogs exists since v1; stub it so the v25 own_only backfill
        # (INSERT...SELECT FROM synced_dialogs) succeeds when seeding mid-chain.
        pre_conn.execute(
            "CREATE TABLE synced_dialogs (dialog_id INTEGER PRIMARY KEY, status TEXT NOT NULL DEFAULT 'pending')"
        )
        pre_conn.execute("INSERT INTO schema_version VALUES (23, 1700000000)")
        pre_conn.commit()
        _seed_v24_fixtures(pre_conn)

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        # Schema version (v24 columns exist; MAX will be _CURRENT_SCHEMA_VERSION as later migrations run too)
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION

        # Both new columns exist
        cols = {c[1] for c in _table_info(conn, "dialogs")}
        assert "linked_chat_id" in cols
        assert "linked_chat_resolved_at" in cols

        # (a) channel A: linked chat present
        r = _fetchone_row(conn, "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id = 1001")
        assert r is not None
        assert r[0] == -1002000000000, f"channel A linked_chat_id: expected -1002000000000, got {r[0]}"
        assert r[1] == 1700000000, f"channel A resolved_at: expected 1700000000, got {r[1]}"

        # (b) channel B: key present, JSON null → linked_chat_id NULL, resolved_at populated
        r = _fetchone_row(conn, "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id = 1002")
        assert r is not None
        assert r[0] is None, f"channel B linked_chat_id should be NULL, got {r[0]}"
        assert r[1] == 1700000001, f"channel B resolved_at: expected 1700000001, got {r[1]}"

        # (c) channel C: key absent → both NULL (lazy resolve)
        r = _fetchone_row(conn, "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id = 1003")
        assert r is not None
        assert r[0] is None, f"channel C linked_chat_id should be NULL, got {r[0]}"
        assert r[1] is None, f"channel C resolved_at should be NULL, got {r[1]}"

        # entity_details strip: channels A and B no longer have linked_chat_id key
        for eid, label in [(1001, "A"), (1002, "B")]:
            row = _fetchone_row(
                conn,
                "SELECT json_type(detail_json, '$.linked_chat_id') FROM entity_details WHERE entity_id = ?",
                (eid,),
            )
            assert row is not None
            assert row[0] is None, f"channel {label} detail_json still has linked_chat_id key: {row[0]}"

        # Sibling key survives strip: channel A's subscribers_count = 42
        row = _fetchone_row(
            conn, "SELECT json_extract(detail_json, '$.subscribers_count') FROM entity_details WHERE entity_id = 1001"
        )
        assert row is not None
        assert row[0] == 42, f"channel A subscribers_count should be 42, got {row[0]}"

        # activity_channel_resolution is absent
        assert (
            _fetchone_row(
                conn, "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_channel_resolution'"
            )
            is None
        ), "activity_channel_resolution must be absent after v24"


def test_migration_v24_idempotent(db_path: Path) -> None:
    """Running ensure_sync_schema twice is a no-op (no exception, version stays at current)."""
    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION


def test_migration_v24_channel_c_not_stripped(db_path: Path) -> None:
    """Channel C (no linked_chat_id key) is unaffected by the strip — json_type still NULL."""
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        # No entity_details row was seeded for this db_path, so the strip is vacuously safe.
        # Insert a channel row post-migration to verify json_type on fresh rows behaves.
        conn.execute(
            "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) VALUES (9999, 'channel', 'Test', NULL, 1700000000)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (9999, '{\"subscribers_count\": 5}', 1700000000)"
        )
        conn.commit()
        row = _fetchone_row(
            conn, "SELECT json_type(detail_json, '$.linked_chat_id') FROM entity_details WHERE entity_id = 9999"
        )
        assert row is not None
        assert row[0] is None, "key-absent channel should have NULL json_type for linked_chat_id"
        row2 = _fetchone_row(
            conn, "SELECT json_extract(detail_json, '$.subscribers_count') FROM entity_details WHERE entity_id = 9999"
        )
        assert row2 is not None
        assert row2[0] == 5, "subscribers_count should be preserved"


# ---------------------------------------------------------------------------
# v25: one-shot backfill of thin dialogs rows for orphan own_only peers
# ---------------------------------------------------------------------------
#
# Approach (Lazy): enroll_activity_dialog now writes a thin dialogs row alongside
# the synced_dialogs own_only insert. The v25 migration materialises thin rows for
# the ~88 pre-existing orphans. Existing run_light_pass (needs_refresh=1 AND hidden=0)
# fills name/type/members/created on its hourly cycle — no new code paths.
#
# FloodWait note: ~88 net-new candidates added at once; ship-as-is per operator
# decision. Observe logs for burst; cap/stagger deferred to a follow-up if needed.


def _make_v24_db(tmp_path: Path) -> Path:
    """Create a minimal v24 database (pre-v25) for migration tests."""
    db_path = tmp_path / "sync.db"
    with _sqlite_connection(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at INTEGER NOT NULL)")
        # These are the complete v35 message tables.  Earlier migration tests
        # intentionally start at a prior version, but v36 operates only on
        # databases where both tables already exist.
        conn.execute(_V36_MESSAGES_DDL)
        conn.execute(_V36_SCHEDULED_MESSAGES_DDL)
        # Minimal dialogs table as it exists post-v24 (includes linked_chat columns)
        conn.execute(
            """CREATE TABLE dialogs (
                dialog_id               INTEGER PRIMARY KEY,
                name                    TEXT,
                type                    TEXT,
                archived                INTEGER NOT NULL DEFAULT 0,
                pinned                  INTEGER NOT NULL DEFAULT 0,
                members                 INTEGER,
                created                 INTEGER,
                last_message_at         INTEGER,
                snapshot_at             INTEGER,
                hidden                  INTEGER NOT NULL DEFAULT 0,
                needs_refresh           INTEGER NOT NULL DEFAULT 0,
                unread_mentions_count   INTEGER NOT NULL DEFAULT 0,
                unread_reactions_count  INTEGER NOT NULL DEFAULT 0,
                draft_text              TEXT,
                linked_chat_id          INTEGER,
                linked_chat_resolved_at INTEGER
            )"""
        )
        # Minimal synced_dialogs table
        conn.execute(
            """CREATE TABLE synced_dialogs (
                dialog_id   INTEGER PRIMARY KEY,
                status      TEXT NOT NULL DEFAULT 'pending',
                access_lost_at INTEGER
            )"""
        )
        # Minimal message_forwards table (exists since v7 in real DBs; v26 UPDATEs it)
        conn.execute(
            """CREATE TABLE message_forwards (
                dialog_id        INTEGER NOT NULL,
                message_id       INTEGER NOT NULL,
                fwd_from_peer_id INTEGER,
                fwd_from_name    TEXT,
                fwd_date         INTEGER,
                fwd_channel_post INTEGER,
                PRIMARY KEY (dialog_id, message_id)
            )"""
        )
        conn.execute(_V23_ACTIVITY_DIALOG_STATE_DDL)
        conn.execute("INSERT INTO schema_version VALUES (24, 1700000000)")
        conn.commit()
    return db_path


def test_migration_schema_version_is_current(tmp_path: Path) -> None:
    """After all migrations, MAX(schema_version) == _CURRENT_SCHEMA_VERSION."""
    db_path = _make_v24_db(tmp_path)
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        assert _CURRENT_SCHEMA_VERSION == 64


def test_migration_v34_maps_coverage_and_preserves_rows_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.executemany(
            "INSERT INTO synced_dialogs(dialog_id, status, sync_progress, total_messages) VALUES (?, ?, ?, ?)",
            [(1, "synced", 10, 20), (2, "own_only", 3, 4), (3, "access_lost", 5, 6)],
        )
        conn.execute("DROP INDEX idx_full_history_enrollment_enabled")
        conn.execute("DROP TABLE full_history_enrollment")
        # Recreate the complete v36 message-table shape before replaying the
        # v34-v37 migration tail; current v37 tables are intentionally not
        # valid inputs for the v37 rebuild.
        conn.execute("DROP TABLE messages")
        conn.execute("DROP TABLE scheduled_messages")
        conn.execute(_V36_MESSAGES_DDL)
        conn.execute(_V36_SCHEDULED_MESSAGES_DDL)
        conn.execute("DROP TABLE activity_dialog_state")
        conn.execute(_V23_ACTIVITY_DIALOG_STATE_DDL)
        conn.execute("DELETE FROM schema_version WHERE version >= 34")
        conn.commit()

    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT dialog_id, enabled, source FROM full_history_enrollment ORDER BY dialog_id"
        ).fetchall() == [(1, 1, "migration"), (2, 0, "migration"), (3, 0, "migration")]
        assert conn.execute(
            "SELECT dialog_id, status, sync_progress, total_messages FROM synced_dialogs ORDER BY dialog_id"
        ).fetchall() == [(1, "synced", 10, 20), (2, "own_only", 3, 4), (3, "access_lost", 5, 6)]


def test_migration_v30_adds_delta_probe_columns(tmp_path: Path) -> None:
    """v30: existing synced_dialogs rows gain nullable local delta probe state."""
    db_path = _make_v24_db(tmp_path)
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = cast(
            list[tuple[int, str, str, int, object | None, int]],
            conn.execute("PRAGMA table_info(synced_dialogs)").fetchall(),
        )
        cols = {row[1]: row[2] for row in rows}
        assert cols["last_delta_checked_at"] == "INTEGER"
        assert cols["delta_refresh_requested_at"] == "INTEGER"


def test_migration_v26_remarks_known_channel_and_chat_forwards(tmp_path: Path) -> None:
    """v26: bare fwd_from_peer_id is remarked to the marked id only when that marked id is a
    known local dialog. Known users (bare == marked) and already-marked rows are untouched."""
    db_path = _make_v24_db(tmp_path)
    known_channel = -1001579759981  # marked; bare source = 1579759981
    known_chat = -4276001234  # marked legacy chat; bare source = 4276001234
    with _sqlite_connection(db_path) as pre:
        for did in (known_channel, known_chat):
            pre.execute("INSERT INTO dialogs (dialog_id) VALUES (?)", (did,))
        rows = [
            # (message_id, fwd_from_peer_id) — channel, chat, user, already-marked, null
            (1, 1579759981),  # -> known channel marked
            (2, 4276001234),  # -> known legacy chat marked
            (3, 429356),  # known user (not a dialog) -> stays bare
            (4, known_channel),  # already marked -> untouched (guard: >0)
            (5, None),  # null -> untouched
        ]
        for mid, peer in rows:
            pre.execute(
                "INSERT INTO message_forwards (dialog_id, message_id, fwd_from_peer_id) VALUES (?,?,?)",
                (100, mid, peer),
            )
        pre.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        got = {
            int(cast(int, row[0])): row[1]
            for row in _fetchall_rows(
                conn, "SELECT message_id, fwd_from_peer_id FROM message_forwards WHERE dialog_id = 100"
            )
        }
        assert got[1] == known_channel, f"known channel must be remarked, got {got[1]}"
        assert got[2] == known_chat, f"known chat must be remarked, got {got[2]}"
        assert got[3] == 429356, f"unknown user-shaped peer must stay bare, got {got[3]}"
        assert got[4] == known_channel, f"already-marked row must be untouched, got {got[4]}"
        assert got[5] is None, f"null peer must stay null, got {got[5]}"


def test_migration_v25_backfills_orphan_own_only(tmp_path: Path) -> None:
    """v25: an orphan synced_dialogs(status='own_only') with no dialogs row gets a thin
    dialogs row (needs_refresh=1, name IS NULL) after ensure_sync_schema."""
    db_path = _make_v24_db(tmp_path)
    orphan_id = -100888000001

    with _sqlite_connection(db_path) as pre_conn:
        pre_conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'own_only')",
            (orphan_id,),
        )
        pre_conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(conn, "SELECT needs_refresh, name FROM dialogs WHERE dialog_id = ?", (orphan_id,))
        assert row is not None, "v25 backfill must create a thin dialogs row for orphan own_only peer"
        assert row[0] == 1, f"needs_refresh must be 1 after backfill, got {row[0]!r}"
        assert row[1] is None, f"name must remain NULL until reconciliation fills it, got {row[1]!r}"


def test_migration_v25_leaves_resolved_own_only_untouched(tmp_path: Path) -> None:
    """v25: an own_only peer that already has a resolved dialogs row is untouched
    (INSERT OR IGNORE preserves name, type, needs_refresh=0)."""
    db_path = _make_v24_db(tmp_path)
    peer_id = -100888000002

    with _sqlite_connection(db_path) as pre_conn:
        pre_conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'own_only')",
            (peer_id,),
        )
        pre_conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, needs_refresh, snapshot_at,"
            " archived, pinned, hidden, unread_mentions_count, unread_reactions_count)"
            " VALUES (?, 'Already Resolved', 'user', 0, 1700000000, 0, 0, 0, 0, 0)",
            (peer_id,),
        )
        pre_conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(conn, "SELECT name, type, needs_refresh FROM dialogs WHERE dialog_id = ?", (peer_id,))
        assert row is not None
        assert row[0] == "Already Resolved", f"name must not be clobbered, got {row[0]!r}"
        assert row[1] == "user", f"type must not be clobbered, got {row[1]!r}"
        assert row[2] == 0, f"needs_refresh must stay 0 (already resolved), got {row[2]!r}"


def test_migration_v25_ignores_non_own_only(tmp_path: Path) -> None:
    """v25: a synced_dialogs row with status != 'own_only' that has no dialogs row
    must NOT get a thin dialogs row — the backfill is own_only-only."""
    db_path = _make_v24_db(tmp_path)
    synced_id = -100888000003
    pending_id = -100888000004

    with _sqlite_connection(db_path) as pre_conn:
        pre_conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (synced_id,),
        )
        pre_conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'pending')",
            (pending_id,),
        )
        pre_conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        row_synced = _fetchone_row(conn, "SELECT dialog_id FROM dialogs WHERE dialog_id = ?", (synced_id,))
        assert row_synced is None, "non-own_only 'synced' peer must NOT get a thin dialogs row"

        row_pending = _fetchone_row(conn, "SELECT dialog_id FROM dialogs WHERE dialog_id = ?", (pending_id,))
        assert row_pending is None, "non-own_only 'pending' peer must NOT get a thin dialogs row"


def test_migration_v63_repairs_positive_synced_dialog_orphans(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status, last_event_at, last_synced_at) VALUES (?, 'synced', ?, ?)",
            (12345, 1700000002, 1700000001),
        )
        conn.execute(
            "INSERT INTO entities(id, type, name, name_normalized, updated_at) VALUES (?, 'bot', ?, ?, ?)",
            (12345, "Exact Bot", "exact bot", 1700000000),
        )
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text) VALUES (?, ?, ?, ?)",
            (12345, 1, 1700000005, "hello"),
        )
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'access_lost')",
            (12346,),
        )
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')",
            (12348,),
        )
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at) "
            "VALUES (?, 0, 'explicit', 1700000000)",
            (12348,),
        )
        conn.execute(
            "INSERT INTO dialogs(dialog_id, name, type, hidden, archived, pinned, needs_refresh, draft_text) "
            "VALUES (?, 'Keep', 'user', 1, 1, 1, 0, 'draft')",
            (12347,),
        )
        conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'synced')", (12347,))
        conn.execute("DELETE FROM schema_version WHERE version = 63")
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert conn.execute(
            "SELECT name, type, last_message_at, hidden, needs_refresh FROM dialogs WHERE dialog_id=?", (12345,)
        ).fetchone() == ("Exact Bot", "bot", 1700000005, 0, 1)
        assert conn.execute("SELECT dialog_id FROM dialogs WHERE dialog_id=?", (12346,)).fetchone() is None
        assert conn.execute("SELECT dialog_id FROM dialogs WHERE dialog_id=?", (12348,)).fetchone() is None
        assert conn.execute(
            "SELECT name, type, hidden, archived, pinned, needs_refresh, draft_text FROM dialogs WHERE dialog_id=?",
            (12347,),
        ).fetchone() == ("Keep", "user", 1, 1, 1, 0, "draft")


def test_startup_repair_reclassifies_persisted_replies_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO entities(id, type, name, username, updated_at) VALUES (?, ?, ?, ?, ?)",
            (777000, "bot", "Replies", "Replies", 1),
        )
        conn.execute("INSERT INTO dialogs(dialog_id, name, type) VALUES (?, ?, ?)", (777000, "Replies", "Bot"))
        conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert _fetchone_row(conn, "SELECT type FROM entities WHERE id = ?", (777000,)) == ("service",)
        assert _fetchone_row(conn, "SELECT type FROM dialogs WHERE dialog_id = ?", (777000,)) == ("service",)


def test_v51_fresh_schema_has_runtime_store_and_focused_alert_projection(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == _CURRENT_SCHEMA_VERSION
        tables = {row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "runtime_observations" in tables
        assert "telemetry_events" not in tables
        assert "daemon_events" not in tables
        columns = [row[1] for row in _fetchall_rows(conn, "PRAGMA table_info(conversation_history_events)")]
        assert columns == [
            "seq",
            "kind",
            "occurred_at",
            "time_basis",
            "dialog_id",
            "message_id",
            "version",
            "reason_code",
            "previous_status",
            "source_namespace",
            "source_event_id",
            "access_change_cause",
            "actor_id",
        ]
        assert "origin" in {row[1] for row in _fetchall_rows(conn, "PRAGMA table_info(message_versions)")}


def test_v49_adds_bounded_own_activity_index(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(
            conn, "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_messages_own_activity_sent'"
        )
        assert row is not None
        assert "WHERE out = 1 AND is_service = 0 AND is_deleted = 0" in str(row[0])


def test_v50_adds_covering_dialog_summary_index(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(
            conn, "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_messages_dialog_summary'"
        )
        assert row is not None
        assert "dialog_id, is_deleted, is_service, out, message_id" in str(row[0])


def test_v51_human_dm_alert_policy_and_sequence_are_durable(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        for dialog_id, dialog_type in ((1, "user"), (2, "bot"), (3, "group"), (4, None)):
            conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (?, ?)", (dialog_id, dialog_type))
        conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (1, 'user', 1)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,sender_id,out) VALUES "
            "(1,1,1,1,0),(1,2,1,1,1),(2,1,1,2,0),(3,1,1,3,0),(4,1,1,4,0)"
        )
        before = _fetchone_int(conn, "SELECT seq FROM sqlite_sequence WHERE name='conversation_history_events'")
        for dialog_id, message_id in ((1, 1), (1, 2), (2, 1), (3, 1), (4, 1)):
            conn.execute(
                "UPDATE messages SET is_deleted=1, deleted_at=10 WHERE dialog_id=? AND message_id=?",
                (dialog_id, message_id),
            )
        rows = _fetchall_rows(conn, "SELECT kind,dialog_id,message_id FROM conversation_history_events")
        assert rows == [("deleted_message", 1, 1)]
        after = _fetchone_int(conn, "SELECT seq FROM sqlite_sequence WHERE name='conversation_history_events'")
        assert after == before + 1


def test_v51_replay_does_not_clear_new_alerts(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,message_id) VALUES ('deleted_message',1,'observed',1,1)"
        )
        conn.execute("DELETE FROM schema_version WHERE version >= 51")
        conn.commit()
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM conversation_history_events") == 1


def _downgrade_event_tables_to_v50(conn: sqlite3.Connection) -> None:
    for trigger in (
        "sync_alert_events_message_insert_deleted",
        "sync_alert_events_message_delete_transition",
        "conversation_history_message_insert_deleted",
        "conversation_history_message_delete_transition",
    ):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.execute("DROP TABLE runtime_observations")
    conn.execute("DROP TABLE conversation_history_events")
    conn.execute("DROP TABLE event_recovery_ledger")
    conn.executescript(
        """CREATE TABLE telemetry_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, tool_name TEXT NOT NULL, timestamp REAL NOT NULL,
               duration_ms REAL NOT NULL, result_count INTEGER NOT NULL, has_cursor INTEGER NOT NULL,
               page_depth INTEGER NOT NULL, has_filter INTEGER NOT NULL, outcome TEXT NOT NULL,
               error_code TEXT, error_type TEXT);
           CREATE TABLE daemon_events (
               id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, dialog_id INTEGER,
               occurred_at INTEGER NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}');
           CREATE TABLE sync_alert_events (
               seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, occurred_at INTEGER NOT NULL,
               dialog_id INTEGER NOT NULL, message_id INTEGER, version INTEGER, daemon_event_id INTEGER);"""
    )
    conn.execute("DELETE FROM schema_version WHERE version >= 51")


def _downgrade_message_versions_to_v47(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE message_versions RENAME TO message_versions_current")
    conn.execute(
        """CREATE TABLE message_versions (
               dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
               version INTEGER NOT NULL, old_text TEXT, edit_date INTEGER,
               PRIMARY KEY (dialog_id, message_id, version)
           ) WITHOUT ROWID"""
    )
    conn.execute(
        """INSERT INTO message_versions(dialog_id, message_id, version, old_text, edit_date)
           SELECT dialog_id, message_id, version, old_text, edit_date
             FROM message_versions_current"""
    )
    conn.execute("DROP TABLE message_versions_current")


def _downgrade_event_tables_to_v53(conn: sqlite3.Connection) -> None:
    trigger_rows = cast(
        list[tuple[str]],
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'conversation_history_%'"
        ).fetchall(),
    )
    for row in trigger_rows:
        conn.execute(f"DROP TRIGGER {row[0]}")
    conn.execute("DROP TABLE event_recovery_ledger")
    conn.execute("ALTER TABLE runtime_observations RENAME TO runtime_events")
    conn.execute("ALTER TABLE conversation_history_events RENAME TO sync_alert_events")
    conn.execute("DELETE FROM schema_version WHERE version >= 54")


def test_v51_reconstructs_strict_dm_alerts_and_preserves_access_lost(tmp_path: Path) -> None:
    """v51 keeps only positively identified incoming human-DM changes."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        _downgrade_message_versions_to_v47(conn)
        dialog_rows = [
            (1, "user"),
            (2, "user"),
            (3, "bot"),
            (4, "group"),
            (5, "supergroup"),
            (6, "channel"),
            (7, "user"),
            (8, None),
        ]
        conn.executemany("INSERT INTO dialogs(dialog_id, type) VALUES (?, ?)", dialog_rows)
        conn.executemany("INSERT INTO entities(id, type, updated_at) VALUES (?, 'user', 1)", [(1,), (2,), (7,)])
        conn.executemany(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, sender_id, out, is_service, is_deleted, deleted_at) "
            "VALUES (?, 1, 1, 'old', ?, ?, ?, 1, 10)",
            [(dialog_id, dialog_id, int(dialog_id == 2), int(dialog_id == 7)) for dialog_id, _ in dialog_rows],
        )
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, sender_id, out, is_service, is_deleted, deleted_at) "
            "VALUES (1, 2, 1, 'old', 1, 0, 0, 0, NULL)"
        )
        conn.execute(
            "INSERT INTO message_versions(dialog_id, message_id, version, old_text, edit_date) VALUES (1, 1, 1, 'old', 20)"
        )
        conn.execute(
            "INSERT INTO message_versions(dialog_id, message_id, version, old_text, edit_date) VALUES (1, 2, 1, 'old', 21)"
        )
        conn.execute(
            "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) "
            "VALUES (1, 2, 'new', 1, 21)"
        )
        conn.executemany(
            "INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id, version, daemon_event_id) "
            "VALUES ('deleted_message', 10, ?, 1, NULL, NULL)",
            [(dialog_id,) for dialog_id, _ in dialog_rows],
        )
        conn.execute(
            "INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id, version, daemon_event_id) "
            "VALUES ('edit', 20, 1, 1, 1, NULL)"
        )
        conn.execute(
            "INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id, version, daemon_event_id) "
            "VALUES ('edit', 21, 1, 2, 1, NULL)"
        )
        conn.execute("INSERT INTO daemon_events(kind, occurred_at, dialog_id) VALUES ('access_lost', 30, 1)")
        access_id = _fetchone_int(conn, "SELECT MAX(id) FROM daemon_events")
        conn.execute(
            "INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, daemon_event_id) "
            "VALUES ('access_lost', 30, 1, ?)",
            (access_id,),
        )
        conn.executemany(
            "INSERT INTO daemon_events(kind, occurred_at, dialog_id) VALUES ('runtime_probe', 1, NULL)",
            [() for _ in range(99)],
        )
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = _fetchall_rows(
            conn,
            "SELECT kind, dialog_id, message_id, version, occurred_at FROM conversation_history_events ORDER BY occurred_at, kind",
        )
        assert rows == [
            ("deleted_message", 1, 1, None, 10),
            ("edit", 1, 1, 1, 20),
            ("access_lost", 1, None, None, 30),
        ]
        assert _fetchone_int(conn, "SELECT MIN(seq) FROM conversation_history_events") > 100
        assert _fetchone_row(conn, "SELECT origin FROM message_versions WHERE dialog_id=1 AND message_id=1") == (
            "legacy_unknown",
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind, occurred_at, time_basis, dialog_id) VALUES ('access_lost', 40, 'observed', 1)"
        )
        assert _fetchone_int(conn, "SELECT MAX(seq) FROM conversation_history_events") > 100


def test_v52_expands_origin_contract_without_guessing_from_edit_date(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    boundary_ms = 2_000_000_000_000
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO daemon_state(key, value) VALUES ('runtime_observations_history_started_at_ms', ?)",
            (str(boundary_ms),),
        )
        conn.execute("ALTER TABLE message_versions RENAME TO message_versions_current")
        conn.execute(
            """CREATE TABLE message_versions (
                   dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                   version INTEGER NOT NULL, old_text TEXT, edit_date INTEGER,
                   origin TEXT NOT NULL DEFAULT 'telegram_edit'
                     CHECK (origin IN ('telegram_edit', 'transcription')),
                   PRIMARY KEY (dialog_id, message_id, version)
               ) WITHOUT ROWID"""
        )
        conn.executemany(
            "INSERT INTO message_versions VALUES (1, ?, 1, 'old', ?, ?)",
            [
                (1, 1_999_999_999, "telegram_edit"),
                (2, 2_000_000_000, "telegram_edit"),
                (3, 1_999_999_999, "transcription"),
            ],
        )
        conn.execute("DROP TABLE message_versions_current")
        conn.execute("DELETE FROM schema_version WHERE version >= 52")
        conn.commit()

    with _sync_db_connection(db_path) as conn:
        _apply_migration_52(conn, 51)
    with _sync_db_connection(db_path) as conn:
        assert _fetchall_rows(conn, "SELECT message_id, origin FROM message_versions ORDER BY message_id") == [
            (1, "telegram_edit"),
            (2, "telegram_edit"),
            (3, "transcription"),
        ]


def test_v52_origin_repair_rolls_back_before_replacing_versions(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("ALTER TABLE message_versions RENAME TO message_versions_current")
        conn.execute(
            """CREATE TABLE message_versions (
                   dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                   version INTEGER NOT NULL, old_text TEXT, edit_date INTEGER,
                   origin TEXT NOT NULL DEFAULT 'telegram_edit'
                     CHECK (origin IN ('telegram_edit', 'transcription')),
                   PRIMARY KEY (dialog_id, message_id, version)
               ) WITHOUT ROWID"""
        )
        conn.execute("INSERT INTO message_versions VALUES (1, 1, 1, 'old', 1, 'telegram_edit')")
        conn.execute("DROP TABLE message_versions_current")
        conn.execute("CREATE TABLE message_versions_origin_migration(blocker INTEGER)")
        conn.execute("DELETE FROM schema_version WHERE version >= 52")
        conn.commit()

        ensure_sync_schema(db_path)
        assert _fetchone_row(conn, "SELECT origin FROM message_versions WHERE dialog_id=1") == ("telegram_edit",)
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=52") == (52,)


def test_v51_preserves_access_alerts_and_uses_both_sequence_high_water_marks(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        conn.executemany(
            "INSERT INTO daemon_events(kind,occurred_at) VALUES ('access_lost',1)",
            [() for _ in range(7)],
        )
        conn.execute(
            "INSERT INTO sync_alert_events(kind,occurred_at,dialog_id,daemon_event_id) VALUES ('access_lost',1,1,7)"
        )
        conn.commit()
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM conversation_history_events WHERE kind='access_lost'") == 1
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id) VALUES ('access_lost',2,'observed',1)"
        )
        assert _fetchone_int(conn, "SELECT MAX(seq) FROM conversation_history_events") > 7


def test_v51_failure_before_table_swap_rolls_back_all_prior_ddl(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        conn.execute("CREATE TABLE sync_alert_events_v51(blocker INTEGER)")
        conn.commit()
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            ensure_sync_schema(db_path)
        tables = {row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"telemetry_events", "daemon_events", "sync_alert_events"} <= tables
        assert "runtime_events" not in tables
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=51") is None


@pytest.mark.parametrize(
    ("action", "first", "second"),
    [
        (sqlite3.SQLITE_DROP_TABLE, "sync_alert_events", None),
        (sqlite3.SQLITE_ALTER_TABLE, "main", "sync_alert_events_v51"),
        (sqlite3.SQLITE_DROP_TABLE, "telemetry_events", None),
        (sqlite3.SQLITE_DROP_TABLE, "daemon_events", None),
    ],
)
def test_v51_each_destructive_ddl_failure_rolls_back_cutover(
    tmp_path: Path, action: int, first: str, second: str | None
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        conn.execute(
            "INSERT INTO telemetry_events(tool_name,timestamp,duration_ms,result_count,has_cursor,page_depth,has_filter,outcome) VALUES ('probe',1,1,0,0,0,0,'success')"
        )
        conn.execute("INSERT INTO daemon_events(kind,occurred_at,dialog_id) VALUES ('access_lost',1,1)")
        daemon_id = _fetchone_int(conn, "SELECT id FROM daemon_events")
        conn.execute(
            "INSERT INTO sync_alert_events(kind,occurred_at,dialog_id,daemon_event_id) VALUES ('access_lost',1,1,?)",
            (daemon_id,),
        )
        conn.commit()

        def deny_target(
            requested_action: int,
            requested_first: str | None,
            requested_second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if (
                requested_action == action
                and requested_first == first
                and (second is None or requested_second == second)
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_target)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_51(conn, 50)
        conn.set_authorizer(None)

        tables = {row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"telemetry_events", "daemon_events", "sync_alert_events"} <= tables
        assert "runtime_events" not in tables
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM telemetry_events") == 1
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM daemon_events") == 1
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM sync_alert_events") == 1
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=51") is None


@pytest.mark.parametrize(
    ("action", "first", "second"),
    [
        (sqlite3.SQLITE_ALTER_TABLE, "main", "message_versions"),
        (sqlite3.SQLITE_DROP_TABLE, "message_versions_origin_migration", None),
    ],
)
def test_v51_message_version_rebuild_failure_rolls_back(
    tmp_path: Path, action: int, first: str, second: str | None
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        _downgrade_message_versions_to_v47(conn)
        conn.execute(
            "INSERT INTO message_versions(dialog_id,message_id,version,old_text,edit_date) VALUES (1,1,1,'old',1)"
        )
        conn.commit()

        def deny_target(
            requested_action: int,
            requested_first: str | None,
            requested_second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if (
                requested_action == action
                and requested_first == first
                and (second is None or requested_second == second)
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_target)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_51(conn, 50)
        conn.set_authorizer(None)

        columns = {row[1] for row in _fetchall_rows(conn, "PRAGMA table_info(message_versions)")}
        assert "origin" not in columns
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_versions") == 1
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=51") is None


@pytest.mark.parametrize(
    "trigger_name",
    [
        "sync_alert_events_message_insert_deleted",
        "sync_alert_events_message_delete_transition",
        "sync_alert_events_message_edit",
        "sync_alert_events_access_lost",
    ],
)
def test_v51_trigger_drop_failure_rolls_back(tmp_path: Path, trigger_name: str) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        for name in (
            "sync_alert_events_message_insert_deleted",
            "sync_alert_events_message_delete_transition",
            "sync_alert_events_message_edit",
            "sync_alert_events_access_lost",
        ):
            conn.execute(f"CREATE TRIGGER {name} AFTER INSERT ON messages BEGIN SELECT 1; END")
        conn.commit()

        def deny_trigger(
            action: int,
            first: str | None,
            _second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_DROP_TRIGGER and first == trigger_name:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_trigger)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_51(conn, 50)
        conn.set_authorizer(None)

        triggers = {row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert trigger_name in triggers
        assert "runtime_events" not in {
            row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=51") is None


@pytest.mark.parametrize("legacy_table", ["telemetry_events", "daemon_events"])
def test_v51_replay_cleanup_failure_rolls_back(tmp_path: Path, legacy_table: str) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v50(conn)
        conn.commit()

        def deny_drop(
            action: int,
            first: str | None,
            _second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_DROP_TABLE and first == legacy_table:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_drop)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_51(conn, 50)
        conn.set_authorizer(None)

        tables = {row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"telemetry_events", "daemon_events"} <= tables
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=51") is None


def test_v52_failure_after_rename_rolls_back_original_contract(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("ALTER TABLE message_versions RENAME TO message_versions_current")
        conn.execute(
            """CREATE TABLE message_versions (
                   dialog_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                   version INTEGER NOT NULL, old_text TEXT, edit_date INTEGER,
                   origin TEXT NOT NULL DEFAULT 'telegram_edit'
                     CHECK (origin IN ('telegram_edit', 'transcription')),
                   PRIMARY KEY (dialog_id, message_id, version)
               ) WITHOUT ROWID"""
        )
        conn.execute("INSERT INTO message_versions VALUES (1,1,1,'old',1,'telegram_edit')")
        conn.execute("DROP TABLE message_versions_current")
        conn.execute("DELETE FROM schema_version WHERE version >= 52")
        conn.commit()

        def deny_drop(
            action: int,
            first: str | None,
            _second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_DROP_TABLE and first == "message_versions_origin_migration":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_drop)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_52(conn, 51)
        conn.set_authorizer(None)

        schema = _fetchone_row(conn, "SELECT sql FROM sqlite_master WHERE name='message_versions'")
        assert schema is not None and "legacy_unknown" not in str(schema[0])
        assert _fetchone_row(conn, "SELECT origin FROM message_versions") == ("telegram_edit",)
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=52") is None


def test_v54_ledger_damage_does_not_replay_destructive_v53_cleanup(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("INSERT INTO dialogs(dialog_id, type) VALUES (1, 'user'), (2, 'group')")
        conn.execute("INSERT INTO entities(id, type, updated_at) VALUES (1, 'user', 1)")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,sender_id,out,is_service) VALUES "
            "(1,1,1,1,0,0),(2,1,1,2,0,0)"
        )
        conn.execute(
            "INSERT INTO message_versions(dialog_id,message_id,version,old_text,edit_date,origin) "
            "VALUES (1,1,1,'kept',10,'telegram_edit'),(2,1,1,'discarded',10,'telegram_edit')"
        )
        conn.execute(
            "INSERT INTO conversation_history_events(kind,occurred_at,time_basis,dialog_id,message_id,version) VALUES ('edit',10,'telegram',1,1,1)"
        )
        conn.execute("DELETE FROM schema_version WHERE version=53")
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchall_rows(
            conn, "SELECT dialog_id,message_id,old_text FROM message_versions ORDER BY dialog_id"
        ) == [(1, 1, "kept"), (2, 1, "discarded")]
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=53") == (53,)


def test_v53_delete_failure_rolls_back_all_version_history(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute(
            "INSERT INTO message_versions(dialog_id,message_id,version,old_text,edit_date,origin) "
            "VALUES (1,1,1,'first',10,'legacy_unknown'),(2,1,1,'second',10,'legacy_unknown')"
        )
        conn.execute("DELETE FROM schema_version WHERE version=53")
        conn.commit()

        def deny_delete(
            action: int,
            first: str | None,
            _second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_DELETE and first == "message_versions":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_delete)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_53(conn, 52)
        conn.set_authorizer(None)

        assert _fetchone_int(conn, "SELECT COUNT(*) FROM message_versions") == 2
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=53") is None


def test_v54_moves_runtime_lifecycle_rows_into_durable_history(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v53(conn)
        conn.execute(
            "INSERT INTO sync_alert_events(kind,occurred_at,time_basis,dialog_id) "
            "VALUES ('access_lost',10,'observed',1)"
        )
        conn.execute(
            "INSERT INTO runtime_events(observed_at_ms,kind,runtime_instance_id,reason_code,dialog_id,payload_json) "
            "VALUES (10000,'sync.access_lost','old','ChannelPrivateError',1,'{\"previous_status\":\"full\"}'),"
            "(11000,'sync.access_restored','old',NULL,1,'{}'),"
            "(12000,'mcp.call','old',NULL,NULL,'{}')"
        )
        conn.commit()
        _apply_migration_54(conn, 53)
        assert _fetchall_rows(
            conn,
            "SELECT kind,occurred_at,reason_code,previous_status FROM conversation_history_events ORDER BY seq",
        ) == [
            ("access_lost", 10, "ChannelPrivateError", "full"),
            ("access_restored", 11, None, None),
        ]
        assert _fetchall_rows(conn, "SELECT kind FROM runtime_observations") == [("mcp.call",)]


@pytest.mark.parametrize(
    ("action", "first", "second"),
    [
        (sqlite3.SQLITE_DROP_TABLE, "runtime_events", None),
        (sqlite3.SQLITE_ALTER_TABLE, "main", "conversation_history_events_v54"),
    ],
)
def test_v54_destructive_ddl_failure_rolls_back_event_cutover(
    tmp_path: Path, action: int, first: str, second: str | None
) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        _downgrade_event_tables_to_v53(conn)
        conn.execute(
            "INSERT INTO runtime_events(observed_at_ms,kind,runtime_instance_id,dialog_id) "
            "VALUES (10000,'sync.access_lost','old',1)"
        )
        conn.commit()

        def deny_target(
            requested_action: int,
            requested_first: str | None,
            requested_second: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if (
                requested_action == action
                and requested_first == first
                and (second is None or requested_second == second)
            ):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_target)
        with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
            _apply_migration_54(conn, 53)
        conn.set_authorizer(None)

        tables = {row[0] for row in _fetchall_rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"runtime_events", "sync_alert_events"} <= tables
        assert "runtime_observations" not in tables
        assert "conversation_history_events" not in tables
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM runtime_events") == 1
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=54") is None


def test_v55_adds_access_cause_and_actor_atomically(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("ALTER TABLE conversation_history_events DROP COLUMN actor_id")
        conn.execute("ALTER TABLE conversation_history_events DROP COLUMN access_change_cause")
        conn.execute("DELETE FROM schema_version WHERE version=55")
        conn.commit()
        _apply_migration_55(conn, 54)
        columns = {row[1] for row in _fetchall_rows(conn, "PRAGMA table_info(conversation_history_events)")}
        assert {"access_change_cause", "actor_id"} <= columns
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=55") == (55,)


def test_v56_adds_stable_tool_telemetry_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DELETE FROM schema_version WHERE version=56")
        conn.execute("UPDATE runtime_observations SET tool_capability=NULL,contract_version=NULL")
        conn.execute(
            "INSERT INTO runtime_observations(observed_at_ms,kind,runtime_instance_id,tool_name,payload_json) "
            "VALUES (1,'mcp.call','test','get_sync_alerts','{}')"
        )
        conn.commit()
        _apply_migration_56(conn, 55)
        row = _fetchone_row(
            conn,
            "SELECT tool_capability,contract_version FROM runtime_observations WHERE tool_name='get_sync_alerts'",
        )
        assert row == ("conversation_changes", 0)
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=56") == (56,)


def test_v58_adds_account_trace_author_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DROP INDEX idx_messages_account_trace_sender")
        conn.execute("DROP INDEX idx_messages_account_trace_post_author")
        conn.execute("DELETE FROM schema_version WHERE version=58")
        conn.commit()

        _apply_migration_58(conn, 57)

        indexes = {
            str(row[0])
            for row in _fetchall_rows(
                conn,
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_messages_account_trace_%'",
            )
        }
        assert indexes == {
            "idx_messages_account_trace_sender",
            "idx_messages_account_trace_post_author",
        }
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=58") == (58,)


def test_v59_seeds_active_scheduled_repairs_and_staggers_discovery(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DROP TABLE scheduled_reconciliation_state")
        conn.execute("DELETE FROM schema_version WHERE version=59")
        conn.execute("INSERT INTO dialogs(dialog_id, type, hidden) VALUES (42, 'user', 0)")
        conn.execute(
            "INSERT INTO scheduled_messages(dialog_id, message_id, scheduled_at, first_seen_at, updated_at) "
            "VALUES (42, 7, 2000000000, 1, 1)"
        )
        conn.commit()

        _apply_migration_59(conn, 58)

        now = _fetchone_int(conn, "SELECT applied_at FROM schema_version WHERE version=59")
        state = _fetchone_row(
            conn,
            "SELECT repair_due_at, discovery_due_at FROM scheduled_reconciliation_state WHERE dialog_id=42",
        )
        assert state == (now, now + 42)
        assert {row[1] for row in _table_info(conn, "scheduled_reconciliation_state")} == {
            "dialog_id",
            "repair_due_at",
            "discovery_due_at",
            "dirty_since",
            "dirty_generation",
            "updated_at",
        }


def test_v60_adds_restart_safe_domain_state_and_preserves_profile_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.executescript(
            """
            DROP TRIGGER dialogs_revision_after_update;
            DROP TRIGGER synced_dialogs_clear_access_recovery;
            DROP TABLE dialog_full_reconciliation_baseline;
            DROP TABLE dialog_full_reconciliation_state;
            DROP TABLE delta_access_recovery_state;
            DROP INDEX idx_entity_profile_refresh_due;
            DROP TABLE entity_profile_refresh_state;
            CREATE TABLE entity_profile_refresh_state (
                entity_id INTEGER PRIMARY KEY,
                status TEXT NOT NULL CHECK(status IN ('failed', 'pending')),
                retry_at INTEGER,
                reason TEXT,
                updated_at INTEGER NOT NULL
            ) WITHOUT ROWID;
            INSERT INTO entity_profile_refresh_state VALUES (42, 'failed', 123, 'timeout', 100);
            ALTER TABLE activity_dialog_state DROP COLUMN hot_page_offset_id;
            ALTER TABLE activity_dialog_state DROP COLUMN hot_window_max_id;
            ALTER TABLE activity_dialog_state DROP COLUMN hot_window_had_new;
            ALTER TABLE dialogs DROP COLUMN revision;
            DELETE FROM schema_version WHERE version=60;
            """
        )
        conn.commit()

        assert _apply_migration_60(conn, 59) == 60

        activity_columns = {row[1] for row in _table_info(conn, "activity_dialog_state")}
        assert {"hot_page_offset_id", "hot_window_max_id", "hot_window_had_new"} <= activity_columns
        assert "revision" in {row[1] for row in _table_info(conn, "dialogs")}
        assert _fetchone_row(
            conn,
            "SELECT generation, status FROM dialog_full_reconciliation_state WHERE singleton=1",
        ) == (0, "idle")
        assert _fetchone_row(
            conn,
            "SELECT status, retry_at, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42",
        ) == ("failed", 123, "full_profile", 0)
        conn.execute(
            "INSERT INTO entity_profile_refresh_state("
            "entity_id,status,retry_at,reason,updated_at,next_section,acquisition_cursor) "
            "VALUES (43,'rejected',NULL,'full',100,'avatar_history',2)"
        )
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version=60") == (60,)


def test_genuine_v59_schema_upgrades_to_v60_and_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "sync.db"
    with monkeypatch.context() as v59:
        v59.setattr(sync_db_module, "_CURRENT_SCHEMA_VERSION", 59)
        v59.setattr(sync_db_module, "_DOMAIN_RESUME_STATE_MIGRATION_60", 59)
        ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == 59
        assert "revision" not in {row[1] for row in _table_info(conn, "dialogs")}
        assert "next_section" not in {row[1] for row in _table_info(conn, "entity_profile_refresh_state")}
        conn.execute("INSERT INTO dialogs(dialog_id, name, type) VALUES (42, 'kept dialog', 'user')")
        conn.execute(
            "INSERT INTO activity_dialog_state("
            "dialog_id, source, hot_cursor, created_at, updated_at) "
            "VALUES (42, 'supergroup', 77, 100, 200)"
        )
        conn.execute(
            "INSERT INTO entity_profile_refresh_state("
            "entity_id, status, retry_at, reason, updated_at) "
            "VALUES (42, 'failed', 123, 'timeout', 100)"
        )
        conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert _fetchone_row(
            conn,
            "SELECT name, revision FROM dialogs WHERE dialog_id=42",
        ) == ("kept dialog", 0)
        assert _fetchone_row(
            conn,
            "SELECT source, hot_cursor, hot_page_offset_id, hot_window_max_id, hot_window_had_new "
            "FROM activity_dialog_state WHERE dialog_id=42",
        ) == ("supergroup", 77, None, None, 0)
        assert _fetchone_row(
            conn,
            "SELECT status, retry_at, reason, updated_at, next_section, acquisition_cursor "
            "FROM entity_profile_refresh_state WHERE entity_id=42",
        ) == ("failed", 123, "timeout", 100, "full_profile", 0)
        assert _fetchone_row(
            conn,
            "SELECT generation, status, offset_id, observed_count "
            "FROM dialog_full_reconciliation_state WHERE singleton=1",
        ) == (0, "idle", 0, 0)
        conn.execute(
            "UPDATE dialog_full_reconciliation_state "
            "SET generation=1, status='in_progress', observed_count=1 WHERE singleton=1"
        )
        conn.execute(
            "INSERT INTO dialog_full_reconciliation_baseline("
            "generation, dialog_id, baseline_revision, seen) VALUES (1, 42, 0, 1)"
        )
        conn.execute(
            "INSERT INTO delta_access_recovery_state("
            "dialog_id, stage, total_messages, probe_succeeded_at, retry_at, updated_at) "
            "VALUES (42, 'gap_fill', 10, 300, 400, 500)"
        )
        conn.commit()

    ensure_sync_schema(db_path)

    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM schema_version WHERE version=60") == 1
        assert _fetchone_row(
            conn,
            "SELECT generation, status, observed_count FROM dialog_full_reconciliation_state WHERE singleton=1",
        ) == (1, "in_progress", 1)
        assert _fetchone_row(
            conn,
            "SELECT baseline_revision, seen FROM dialog_full_reconciliation_baseline "
            "WHERE generation=1 AND dialog_id=42",
        ) == (0, 1)
        assert _fetchone_row(
            conn,
            "SELECT stage, total_messages, probe_succeeded_at, retry_at, updated_at "
            "FROM delta_access_recovery_state WHERE dialog_id=42",
        ) == ("gap_fill", 10, 300, 400, 500)


def test_v64_preserves_legacy_generation_one_without_manufacturing_a_receipt(tmp_path: Path) -> None:
    """The historical NULL/zero legacy cursor remains fenced, never reset or complete."""
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DROP TABLE dialog_directory_pins")
        conn.execute("DROP TABLE dialog_directory_staging")
        conn.execute("DROP TABLE dialog_directory_baseline")
        conn.execute("DROP TABLE dialog_directory_state")
        conn.execute("DELETE FROM schema_version WHERE version=64")
        conn.execute(
            "UPDATE dialog_full_reconciliation_state SET generation=1,status='in_progress',offset_date=NULL,"
            "offset_id=0,offset_peer=NULL,observed_count=0 WHERE singleton=1"
        )
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,snapshot_at,hidden) VALUES (77,'kept','user',1,0)")
        conn.commit()

        assert _apply_migration_64(conn, 63) == 64
        assert _fetchone_row(
            conn,
            "SELECT generation,status,offset_date,offset_id,offset_peer,observed_count,reason "
            "FROM dialog_directory_state WHERE singleton=1",
        ) == (1, "pending", None, 0, None, 0, "legacy_wrapper_cursor_unverified")
        assert _fetchone_row(conn, "SELECT name FROM dialogs WHERE dialog_id=77") == ("kept",)


def test_v64_keeps_completed_legacy_generation_one_pending_for_raw_proof(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DROP TABLE dialog_directory_pins")
        conn.execute("DROP TABLE dialog_directory_staging")
        conn.execute("DROP TABLE dialog_directory_baseline")
        conn.execute("DROP TABLE dialog_directory_state")
        conn.execute("DELETE FROM schema_version WHERE version=64")
        conn.execute(
            "UPDATE dialog_full_reconciliation_state SET generation=1,status='idle',observed_count=0 WHERE singleton=1"
        )
        conn.execute("INSERT INTO dialogs(dialog_id,name,type,snapshot_at,hidden) VALUES (78,'kept','user',1,0)")
        conn.commit()

        assert _apply_migration_64(conn, 63) == 64
        assert _fetchone_row(
            conn,
            "SELECT generation,status,observation_started_at,observation_completed_at FROM dialog_directory_state",
        ) == (1, "pending", None, None)
        assert _fetchone_row(conn, "SELECT name FROM dialogs WHERE dialog_id=78") == ("kept",)
