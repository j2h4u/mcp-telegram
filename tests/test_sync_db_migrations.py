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

from mcp_telegram.sync_db import (
    _CURRENT_SCHEMA_VERSION,
    _apply_migration_48,
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
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == 49
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
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == 49
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
    assert cols == {
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


def test_migration_v18_daemon_state_empty_after_migration(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        count = _fetchone_int(conn, "SELECT COUNT(*) FROM daemon_state")
        assert count == 0


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
        assert _CURRENT_SCHEMA_VERSION == 49


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
        assert _CURRENT_SCHEMA_VERSION == 49


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


def test_v48_fresh_schema_has_sync_alert_projection(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_int(conn, "SELECT MAX(version) FROM schema_version") == 49
        columns = _fetchall_rows(conn, "PRAGMA table_info(sync_alert_events)")
        assert [row[1] for row in columns] == [
            "seq",
            "kind",
            "occurred_at",
            "dialog_id",
            "message_id",
            "version",
            "daemon_event_id",
        ]


def test_v49_adds_bounded_own_activity_index(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        row = _fetchone_row(
            conn,
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'idx_messages_own_activity_sent'",
        )
        assert row is not None
        assert "WHERE out = 1 AND is_service = 0 AND is_deleted = 0" in str(row[0])
        assert _fetchone_row(conn, "SELECT name FROM sqlite_master WHERE name = 'sync_alert_events_message_edit'") == (
            "sync_alert_events_message_edit",
        )


def test_v48_backfills_current_access_loss_and_replays_safely(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DELETE FROM schema_version WHERE version >= 48")
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status, access_lost_at) VALUES (?, 'access_lost', ?)",
            (902, 1700000100),
        )
        conn.execute(
            "INSERT INTO daemon_events(kind, dialog_id, occurred_at, payload_json) VALUES (?, ?, ?, ?)",
            ("access_lost", 902, 1700000100, '{"reason":"preserve"}'),
        )
        conn.commit()

    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = _fetchall_rows(
            conn,
            "SELECT kind, dialog_id, occurred_at, payload_json FROM daemon_events WHERE dialog_id = ?",
            (902,),
        )
        assert rows == [("access_lost", 902, 1700000100, '{"reason":"preserve"}')]


def test_v48_backfill_preserves_historical_fixture_shape(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DELETE FROM schema_version WHERE version >= 48")
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status, access_lost_at) VALUES (?, 'access_lost', ?)",
            (903, 1700000200),
        )
        conn.execute(
            "INSERT INTO daemon_events(kind, dialog_id, occurred_at, payload_json) VALUES (?, ?, ?, ?)",
            ("access_lost", 904, 1700000000, '{"legacy":true}'),
        )
        conn.commit()

    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        assert _fetchone_row(
            conn,
            "SELECT payload_json FROM daemon_events WHERE dialog_id = ? AND occurred_at = ?",
            (904, 1700000000),
        ) == ('{"legacy":true}',)
        assert _fetchone_row(
            conn,
            "SELECT payload_json FROM daemon_events WHERE dialog_id = ? AND occurred_at = ?",
            (903, 1700000200),
        ) == ("{}",)


def test_v48_backfills_all_sources_deterministically_and_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        for trigger in (
            "sync_alert_events_message_insert_deleted",
            "sync_alert_events_message_delete_transition",
            "sync_alert_events_message_edit",
            "sync_alert_events_access_lost",
        ):
            conn.execute(f"DROP TRIGGER {trigger}")
        conn.execute("DELETE FROM sync_alert_events")
        conn.execute("DELETE FROM schema_version WHERE version >= 48")
        conn.execute(
            "INSERT INTO messages(dialog_id, message_id, sent_at, text, is_deleted, deleted_at) VALUES (?, ?, ?, ?, 1, ?)",
            (901, 1, 1, "deleted", 100),
        )
        conn.execute(
            "INSERT INTO message_versions(dialog_id, message_id, version, old_text, edit_date) VALUES (?, ?, ?, ?, ?)",
            (902, 2, 1, "old", 200),
        )
        conn.execute(
            "INSERT INTO daemon_events(kind, dialog_id, occurred_at, payload_json) VALUES ('access_lost', ?, ?, '{}')",
            (903, 300),
        )
        conn.execute(
            "INSERT INTO synced_dialogs(dialog_id, status, access_lost_at) VALUES (?, 'access_lost', ?)",
            (904, 400),
        )
        conn.commit()

    ensure_sync_schema(db_path)
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        rows = _fetchall_rows(
            conn,
            "SELECT kind, occurred_at, dialog_id, message_id, version, daemon_event_id "
            "FROM sync_alert_events ORDER BY seq",
        )
        assert [row[:5] for row in rows] == [
            ("deleted_message", 100, 901, 1, None),
            ("edit", 200, 902, 2, 1),
            ("access_lost", 300, 903, None, None),
            ("access_lost", 400, 904, None, None),
        ]
        assert len(rows) == 4
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM daemon_events WHERE dialog_id = 904") == 1


def test_v48_triggers_are_interleaved_strict_and_access_restore_is_new_event(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("INSERT INTO messages(dialog_id, message_id, sent_at, text) VALUES (1, 1, 1, 'raw')")
        conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 10 WHERE dialog_id = 1 AND message_id = 1")
        conn.execute(
            "INSERT INTO message_versions(dialog_id, message_id, version, old_text, edit_date) VALUES (1, 1, 1, 'old', 20)"
        )
        conn.execute(
            "INSERT INTO daemon_events(kind, dialog_id, occurred_at, payload_json) VALUES ('access_lost', 1, 30, '{}')"
        )
        conn.execute("UPDATE synced_dialogs SET status = 'access_lost', access_lost_at = 40 WHERE dialog_id = 1")
        conn.commit()
        seqs = _fetchall_rows(conn, "SELECT kind, seq FROM sync_alert_events ORDER BY seq")
        assert [row[0] for row in seqs] == ["deleted_message", "edit", "access_lost"]
        conn.execute("UPDATE messages SET is_deleted = 0 WHERE dialog_id = 1 AND message_id = 1")
        conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 11 WHERE dialog_id = 1 AND message_id = 1")
        conn.commit()
        assert _fetchone_row(
            conn, "SELECT is_deleted, deleted_at FROM messages WHERE dialog_id = 1 AND message_id = 1"
        ) == (1, 11)
        conn.execute("UPDATE synced_dialogs SET status = 'synced' WHERE dialog_id = 1")
        conn.execute(
            "INSERT INTO daemon_events(kind, dialog_id, occurred_at, payload_json) VALUES ('access_lost', 1, 50, '{}')"
        )
        conn.commit()
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM sync_alert_events WHERE kind = 'access_lost'") == 2


def test_v48_migration_rolls_back_atomically_on_projection_invariant_conflict(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("DROP INDEX idx_sync_alert_events_deleted_source")
        conn.execute("DELETE FROM sync_alert_events")
        conn.execute(
            "INSERT INTO sync_alert_events(kind, occurred_at, dialog_id, message_id) VALUES ('deleted_message', 1, 1, 1), ('deleted_message', 2, 1, 1)"
        )
        conn.execute("DELETE FROM schema_version WHERE version >= 48")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            _apply_migration_48(conn, 47)
        assert _fetchone_int(conn, "SELECT COUNT(*) FROM sync_alert_events") == 2
        assert _fetchone_row(conn, "SELECT version FROM schema_version WHERE version = 48") is None


def test_v48_delete_resync_delete_is_idempotent_for_message_source(tmp_path: Path) -> None:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    with _sync_db_connection(db_path) as conn:
        conn.execute("INSERT INTO messages(dialog_id, message_id, sent_at, text) VALUES (1, 1, 1, 'first')")
        conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 10 WHERE dialog_id = 1 AND message_id = 1")
        conn.commit()
        conn.execute(
            "INSERT OR REPLACE INTO messages(dialog_id, message_id, sent_at, text, is_deleted, deleted_at) "
            "VALUES (1, 1, 1, 'resynced', 0, NULL)"
        )
        conn.commit()
        conn.execute("UPDATE messages SET is_deleted = 1, deleted_at = 20 WHERE dialog_id = 1 AND message_id = 1")
        conn.commit()
        assert _fetchone_row(
            conn,
            "SELECT is_deleted, deleted_at FROM messages WHERE dialog_id = 1 AND message_id = 1",
        ) == (1, 20)
        assert (
            _fetchone_int(
                conn,
                "SELECT COUNT(*) FROM sync_alert_events WHERE kind = 'deleted_message' AND dialog_id = 1 AND message_id = 1",
            )
            == 1
        )
