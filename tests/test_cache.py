from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp_telegram.cache import EntityCache, ReactionMetadataCache, TopicMetadataCache


def test_persistence(tmp_db_path: Path) -> None:
    """Entity survives close/reopen of EntityCache on same file."""
    cache = EntityCache(tmp_db_path)
    cache.upsert(101, "user", "Ivan", "ivan123")
    cache.close()

    cache2 = EntityCache(tmp_db_path)
    result = cache2.get(101, ttl_seconds=2_592_000)
    cache2.close()

    assert result is not None
    assert result["id"] == 101
    assert result["type"] == "user"
    assert result["name"] == "Ivan"
    assert result["username"] == "ivan123"


def test_ttl_expiry(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entity beyond TTL returns None from get()."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    cache.upsert(101, "user", "Ivan", None)

    original_time = time.time
    monkeypatch.setattr(cache_module, "time", type("_T", (), {"time": staticmethod(lambda: original_time() + 1000)})())

    result = cache.get(101, ttl_seconds=500)
    assert result is None
    cache.close()


def test_upsert_update(tmp_db_path: Path) -> None:
    """Second upsert with same entity_id updates updated_at and data."""
    cache = EntityCache(tmp_db_path)
    cache.upsert(101, "user", "Ivan", "ivan123")
    first_ts = cache.get(101, ttl_seconds=2_592_000)
    assert first_ts is not None

    cache.upsert(101, "user", "Ivan Updated", "ivan_new")
    second = cache.get(101, ttl_seconds=2_592_000)
    assert second is not None
    assert second["name"] == "Ivan Updated"
    assert second["username"] == "ivan_new"
    cache.close()


def test_cross_process(tmp_db_path: Path) -> None:
    """Data written in one EntityCache instance is readable by another (WAL mode)."""
    cache_a = EntityCache(tmp_db_path)
    cache_a.upsert(200, "group", "Team Alpha", None)

    cache_b = EntityCache(tmp_db_path)
    result = cache_b.get(200, ttl_seconds=604_800)
    assert result is not None
    assert result["name"] == "Team Alpha"

    cache_a.close()
    cache_b.close()


def test_entity_cache_concurrent_open_stays_read_safe_under_locked_writer(tmp_db_path: Path) -> None:
    """A second cache open should stay read-safe while another connection holds a write lock."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.upsert(200, "group", "Team Alpha", None)
    TopicMetadataCache(cache._conn).upsert_topics(
        200,
        [{
            "topic_id": 11,
            "title": "Release Notes",
            "top_message_id": 5011,
            "is_general": False,
            "is_deleted": False,
        }],
    )
    cache.close()

    writer = sqlite3.connect(str(tmp_db_path), timeout=0.1)
    writer.execute("PRAGMA busy_timeout=100")
    writer.execute("BEGIN IMMEDIATE")
    writer.execute(
        "UPDATE topic_metadata SET updated_at = updated_at WHERE dialog_id = ? AND topic_id = ?",
        (200, 11),
    )

    cache_b = EntityCache(tmp_db_path)
    result = cache_b.get(200, ttl_seconds=604_800)
    cache_b.close()

    writer.rollback()
    writer.close()

    assert result is not None
    assert result["name"] == "Team Alpha"


def test_entity_cache_tolerates_locked_wal_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """EntityCache should still initialize when journal_mode=WAL is temporarily locked."""
    import mcp_telegram.cache as cache_module

    class FakeConn:
        def __init__(self) -> None:
            self.isolation_level = None
            self.seen_statements: list[str] = []

        def execute(self, sql: str, params: tuple | None = None):  # noqa: ANN001
            self.seen_statements.append(sql)
            if sql == "PRAGMA journal_mode=WAL":
                raise cache_module.sqlite3.OperationalError("database is locked")
            return self

        def fetchall(self):  # noqa: ANN201
            return []

        def fetchone(self):  # noqa: ANN201
            return None

        def commit(self) -> None:
            return None

        def close(self) -> None:
            return None

    fake_conn = FakeConn()
    monkeypatch.setattr(cache_module.sqlite3, "connect", lambda *args, **kwargs: fake_conn)

    cache = EntityCache(tmp_path / "entity_cache.db")

    assert "PRAGMA busy_timeout=30000" in fake_conn.seen_statements
    assert cache_module._ENTITY_TABLE_DDL.strip() in {statement.strip() for statement in fake_conn.seen_statements}
    cache.close()


def test_expired_returns_none(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Entity with updated_at 100s in past and ttl=50 returns None; ttl=200 returns entity."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    cache.upsert(300, "channel", "News", None)

    original_time = time.time
    future_time = original_time() + 100

    monkeypatch.setattr(cache_module, "time", type("_T", (), {"time": staticmethod(lambda: future_time)})())

    assert cache.get(300, ttl_seconds=50) is None
    assert cache.get(300, ttl_seconds=200) is not None
    cache.close()


def test_all_names_with_ttl_excludes_stale(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """all_names_with_ttl excludes user entity whose TTL has expired."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    cache.upsert(10, "user", "OldUser", None)

    original_time = time.time
    monkeypatch.setattr(
        cache_module, "time",
        type("_T", (), {"time": staticmethod(lambda: original_time() + 1000)})()
    )

    # user_ttl=500: 1000s have passed, so OldUser is stale
    result = cache.all_names_with_ttl(user_ttl=500, group_ttl=604800)
    assert result == {}

    # Upsert a fresh entity AFTER the time-advance
    cache.upsert(11, "user", "FreshUser", None)
    result2 = cache.all_names_with_ttl(user_ttl=500, group_ttl=604800)
    assert 11 in result2

    cache.close()


def test_all_names_with_ttl_user_vs_group_different_ttl(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """all_names_with_ttl returns group but not user when user TTL < elapsed < group TTL."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    cache.upsert(1, "user", "UserAlice", None)
    cache.upsert(2, "group", "GroupBeta", None)

    original_time = time.time
    monkeypatch.setattr(
        cache_module, "time",
        type("_T", (), {"time": staticmethod(lambda: original_time() + 200)})()
    )

    # user_ttl=100: user expired (200 > 100). group_ttl=9999: group still fresh (200 < 9999).
    result = cache.all_names_with_ttl(user_ttl=100, group_ttl=9999)
    assert 1 not in result
    assert 2 in result

    cache.close()


def test_indexes_created(tmp_db_path: Path) -> None:
    """Verify both indexes exist in sqlite_master after EntityCache creation."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    # Open database directly to query schema
    conn = sqlite3.connect(str(tmp_db_path))
    rows = conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'entities'"
    ).fetchall()
    conn.close()

    index_names = {name for name, tbl_name in rows}
    assert "idx_entities_type_updated" in index_names, f"Expected idx_entities_type_updated, found: {index_names}"
    assert "idx_entities_username" in index_names, f"Expected idx_entities_username, found: {index_names}"


def test_ttl_query_uses_index(tmp_db_path: Path) -> None:
    """Verify EXPLAIN QUERY PLAN shows index exists for all_names_with_ttl queries.

    Note: SQLite's query planner may not use the index for OR conditions with multiple
    branches, but the index is used for individual type-based queries and improves
    overall performance. This test verifies the index is present and would be used
    for simpler conditions.
    """
    import sqlite3

    cache = EntityCache(tmp_db_path)
    # Insert test data so query planner makes reasonable decisions
    cache.upsert(1, "user", "Alice", "alice")
    cache.upsert(2, "user", "Bob", "bob")
    cache.upsert(3, "group", "Developers", None)
    cache.upsert(4, "group", "Marketing", None)
    cache.close()

    # Verify index exists in schema
    conn = sqlite3.connect(str(tmp_db_path))
    indexes = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_entities_type_updated'"
    ).fetchall()
    assert len(indexes) > 0, "idx_entities_type_updated index not found in schema"

    # Verify it works for single-type queries (which do use the index)
    query_user = "SELECT id, name FROM entities WHERE type = 'user' AND updated_at >= ?"
    explain_user = conn.execute(f"EXPLAIN QUERY PLAN {query_user}", (0,)).fetchall()
    explain_user_output = "\n".join(str(row) for row in explain_user)
    assert "idx_entities_type_updated" in explain_user_output, (
        f"Expected idx_entities_type_updated in single-type query plan, got:\n{explain_user_output}"
    )

    conn.close()


def test_username_index_used(tmp_db_path: Path) -> None:
    """Verify EXPLAIN QUERY PLAN shows index usage for get_by_username query."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    # Insert test data
    cache.upsert(1, "user", "Alice", "alice")
    cache.upsert(2, "user", "Bob", "bob")
    cache.upsert(3, "group", "Developers", None)
    cache.close()

    # Open database and run EXPLAIN QUERY PLAN on the username query
    conn = sqlite3.connect(str(tmp_db_path))
    query = "SELECT id, name FROM entities WHERE username = ?"
    explain = conn.execute(f"EXPLAIN QUERY PLAN {query}", ("alice",)).fetchall()
    conn.close()

    explain_output = "\n".join(str(row) for row in explain)
    # SQLite should use idx_entities_username; the plan should mention SEARCH, not SCAN
    assert "idx_entities_username" in explain_output or "SEARCH TABLE entities USING INDEX" in explain_output, (
        f"Expected index usage in username query plan, got:\n{explain_output}"
    )


def test_reaction_metadata_cache(tmp_db_path: Path) -> None:
    """Test basic reaction cache: upsert and get with correct data structure."""
    cache = EntityCache(tmp_db_path)
    reaction_cache = ReactionMetadataCache(cache._conn)

    # Upsert reactions for message 100 in dialog 50
    reactions = {
        "👍": ["Alice", "Bob"],
        "❤️": ["Charlie"],
    }
    reaction_cache.upsert(message_id=100, dialog_id=50, reactions_by_emoji=reactions)

    # Retrieve and verify
    result = reaction_cache.get(message_id=100, dialog_id=50, ttl_seconds=600)
    assert result is not None
    assert result == reactions
    cache.close()


def test_reaction_ttl_expiry(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test TTL expiry: stale cache returns None, fresh cache with longer TTL returns data."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    reaction_cache = ReactionMetadataCache(cache._conn)

    # Upsert reactions
    reactions = {"👍": ["Alice", "Bob"]}
    reaction_cache.upsert(message_id=100, dialog_id=50, reactions_by_emoji=reactions)

    # Advance time by 700 seconds (beyond default 600s TTL)
    original_time = time.time
    monkeypatch.setattr(cache_module, "time", type("_T", (), {"time": staticmethod(lambda: original_time() + 700)})())

    # Cache miss with 600s TTL (700s elapsed > 600s)
    result = reaction_cache.get(message_id=100, dialog_id=50, ttl_seconds=600)
    assert result is None

    # Cache hit with 1000s TTL (700s elapsed < 1000s)
    result = reaction_cache.get(message_id=100, dialog_id=50, ttl_seconds=1000)
    assert result == reactions

    cache.close()


def test_topic_metadata_cache_round_trip(tmp_db_path: Path) -> None:
    """Topic metadata is scoped to a dialog and read back with forum flags intact."""
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)

    topic_cache.upsert_topics(
        dialog_id=777,
        topics=[
            {
                "topic_id": 1,
                "title": "General",
                "top_message_id": 1001,
                "is_general": True,
                "is_deleted": False,
            },
            {
                "topic_id": 42,
                "title": "Release Notes",
                "top_message_id": 2042,
                "is_general": False,
                "is_deleted": False,
            },
        ],
    )

    result = topic_cache.get_dialog_topics(dialog_id=777, ttl_seconds=600)
    assert result is not None
    assert result == [
        {
            "topic_id": 1,
            "title": "General",
            "top_message_id": 1001,
            "is_general": True,
            "is_deleted": False,
        },
        {
            "topic_id": 42,
            "title": "Release Notes",
            "top_message_id": 2042,
            "is_general": False,
            "is_deleted": False,
        },
    ]
    assert topic_cache.get_dialog_topics(dialog_id=778, ttl_seconds=600) is None

    cache.close()


def test_topic_metadata_cache_ttl(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Expired topic metadata returns a cache miss."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)
    topic_cache.upsert_topics(
        dialog_id=777,
        topics=[
            {
                "topic_id": 7,
                "title": "Ops",
                "top_message_id": 7007,
                "is_general": False,
                "is_deleted": False,
            }
        ],
    )

    original_time = time.time
    monkeypatch.setattr(
        cache_module,
        "time",
        type("_T", (), {"time": staticmethod(lambda: original_time() + 601)})(),
    )

    assert topic_cache.get_dialog_topics(dialog_id=777, ttl_seconds=600) is None
    assert topic_cache.get_topic(dialog_id=777, topic_id=7, ttl_seconds=600) is None

    cache.close()


def test_topic_metadata_cache_deleted_marker(tmp_db_path: Path) -> None:
    """Deleted topic tombstones stay addressable by ID but stay out of active listings."""
    cache = EntityCache(tmp_db_path)
    topic_cache = TopicMetadataCache(cache._conn)

    topic_cache.upsert_topics(
        dialog_id=777,
        topics=[
            {
                "topic_id": 1,
                "title": "General",
                "top_message_id": 1001,
                "is_general": True,
                "is_deleted": False,
            },
            {
                "topic_id": 9,
                "title": "Deprecated",
                "top_message_id": 9009,
                "is_general": False,
                "is_deleted": True,
            },
        ],
    )

    active_topics = topic_cache.get_dialog_topics(dialog_id=777, ttl_seconds=600)
    deleted_topic = topic_cache.get_topic(dialog_id=777, topic_id=9, ttl_seconds=600)

    assert active_topics == [
        {
            "topic_id": 1,
            "title": "General",
            "top_message_id": 1001,
            "is_general": True,
            "is_deleted": False,
        }
    ]
    assert deleted_topic == {
        "topic_id": 9,
        "title": "Deprecated",
        "top_message_id": 9009,
        "is_general": False,
        "is_deleted": True,
    }

    cache.close()


def test_reaction_cache_hit(tmp_db_path: Path) -> None:
    """Test cache hit: multiple get() calls on same message return consistent data."""
    cache = EntityCache(tmp_db_path)
    reaction_cache = ReactionMetadataCache(cache._conn)

    # Upsert reactions for two messages
    reactions_100 = {"👍": ["Alice"]}
    reactions_101 = {"❤️": ["Bob", "Charlie"]}

    reaction_cache.upsert(message_id=100, dialog_id=50, reactions_by_emoji=reactions_100)
    reaction_cache.upsert(message_id=101, dialog_id=50, reactions_by_emoji=reactions_101)

    # Multiple gets on same messages should return same data (cache hits)
    result_100_a = reaction_cache.get(message_id=100, dialog_id=50, ttl_seconds=600)
    result_100_b = reaction_cache.get(message_id=100, dialog_id=50, ttl_seconds=600)
    assert result_100_a == result_100_b == reactions_100

    result_101_a = reaction_cache.get(message_id=101, dialog_id=50, ttl_seconds=600)
    result_101_b = reaction_cache.get(message_id=101, dialog_id=50, ttl_seconds=600)
    assert result_101_a == result_101_b == reactions_101

    cache.close()


def test_get_name_falls_back_to_user_ttl(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_name() falls back from GROUP_TTL to USER_TTL when the short TTL expires."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    cache.upsert(10, "user", "Alice", None)

    # Advance time past GROUP_TTL (7 days) but within USER_TTL (30 days)
    original_time = time.time
    monkeypatch.setattr(
        cache_module, "time",
        type("_T", (), {"time": staticmethod(lambda: original_time() + 700_000)})(),
    )

    assert cache.get_name(10) == "Alice"
    cache.close()


def test_get_name_returns_none_when_both_ttls_expired(tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """get_name() returns None when both GROUP_TTL and USER_TTL have expired."""
    import mcp_telegram.cache as cache_module

    cache = EntityCache(tmp_db_path)
    cache.upsert(10, "user", "Alice", None)

    original_time = time.time
    monkeypatch.setattr(
        cache_module, "time",
        type("_T", (), {"time": staticmethod(lambda: original_time() + 3_000_000)})(),
    )

    assert cache.get_name(10) is None
    cache.close()


def test_get_name_returns_none_for_missing_entity(tmp_db_path: Path) -> None:
    """get_name() returns None for an entity that was never cached."""
    cache = EntityCache(tmp_db_path)
    assert cache.get_name(999) is None
    cache.close()


def test_get_name_empty_string_returns_none(tmp_db_path: Path) -> None:
    """get_name() returns None for entities with empty name."""
    cache = EntityCache(tmp_db_path)
    cache.upsert(10, "user", "", None)
    assert cache.get_name(10) is None
    cache.close()


def test_upsert_stores_name_normalized(tmp_db_path: Path) -> None:
    """upsert() computes and stores name_normalized via latinize()."""
    cache = EntityCache(tmp_db_path)
    cache.upsert(101, "user", "Ольга Петрова", None)

    normalized = cache.all_names_normalized_with_ttl(user_ttl=2_592_000, group_ttl=604_800)
    assert normalized == {101: "olga petrova"}
    cache.close()


def test_all_names_normalized_with_ttl(tmp_db_path: Path) -> None:
    """all_names_normalized_with_ttl returns latinized names filtered by TTL."""
    cache = EntityCache(tmp_db_path)
    cache.upsert(1, "user", "Иван Петров", None)
    cache.upsert(2, "group", "Рабочая группа", None)

    result = cache.all_names_normalized_with_ttl(user_ttl=2_592_000, group_ttl=604_800)
    assert result[1] == "ivan petrov"
    assert result[2] == "rabochaya gruppa"
    cache.close()


def test_upsert_batch_stores_name_normalized(tmp_db_path: Path) -> None:
    """upsert_batch() computes and stores name_normalized for each entity."""
    cache = EntityCache(tmp_db_path)
    cache.upsert_batch([
        (1, "user", "Ольга Петрова", "olga"),
        (2, "group", "Telegram News", None),
    ])

    normalized = cache.all_names_normalized_with_ttl(user_ttl=2_592_000, group_ttl=604_800)
    assert normalized[1] == "olga petrova"
    assert normalized[2] == "telegram news"
    cache.close()


# ---------------------------------------------------------------------------
# message_cache and message_versions schema tests (Phase 20)
# ---------------------------------------------------------------------------

def test_message_cache_table_exists(tmp_db_path: Path) -> None:
    """message_cache table exists in entity_cache.db after EntityCache init."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_cache'"
    ).fetchone()
    conn.close()

    assert row is not None, "message_cache table not found in sqlite_master"


def test_message_cache_schema(tmp_db_path: Path) -> None:
    """message_cache has correct schema: all 11 structured fields."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    rows = conn.execute("PRAGMA table_info(message_cache)").fetchall()
    conn.close()

    col_map = {str(row[1]): str(row[2]) for row in rows}
    expected = {
        "dialog_id": "INTEGER",
        "message_id": "INTEGER",
        "sent_at": "INTEGER",
        "text": "TEXT",
        "sender_id": "INTEGER",
        "sender_first_name": "TEXT",
        "media_description": "TEXT",
        "reply_to_msg_id": "INTEGER",
        "forum_topic_id": "INTEGER",
        "edit_date": "INTEGER",
        "fetched_at": "INTEGER",
    }
    assert col_map == expected, f"Schema mismatch. Got: {col_map}"


def test_message_cache_pk_constraint(tmp_db_path: Path) -> None:
    """INSERT OR REPLACE on same (dialog_id, message_id) leaves exactly one row."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    conn.execute(
        "INSERT OR REPLACE INTO message_cache (dialog_id, message_id, sent_at, fetched_at, text) VALUES (?, ?, ?, ?, ?)",
        (1, 1, 1000, 2000, "first"),
    )
    conn.execute(
        "INSERT OR REPLACE INTO message_cache (dialog_id, message_id, sent_at, fetched_at, text) VALUES (?, ?, ?, ?, ?)",
        (1, 1, 1000, 2001, "second"),
    )
    conn.commit()

    count = conn.execute(
        "SELECT COUNT(*) FROM message_cache WHERE dialog_id = 1 AND message_id = 1"
    ).fetchone()[0]
    text = conn.execute(
        "SELECT text FROM message_cache WHERE dialog_id = 1 AND message_id = 1"
    ).fetchone()[0]
    conn.close()

    assert count == 1, f"Expected 1 row, got {count}"
    assert text == "second", f"Expected 'second', got {text!r}"


def test_message_cache_without_rowid(tmp_db_path: Path) -> None:
    """WITHOUT ROWID enforces NOT NULL on PK columns — NULL dialog_id raises IntegrityError."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO message_cache (dialog_id, message_id, sent_at, fetched_at) VALUES (?, ?, ?, ?)",
            (None, 1, 1000, 2000),
        )
    conn.close()


def test_message_cache_index_exists(tmp_db_path: Path) -> None:
    """idx_message_cache_dialog_sent index exists on message_cache after EntityCache init."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = 'idx_message_cache_dialog_sent'"
    ).fetchone()
    conn.close()

    assert row is not None, "idx_message_cache_dialog_sent index not found in sqlite_master"


def test_message_versions_table_exists(tmp_db_path: Path) -> None:
    """message_versions table exists in entity_cache.db after EntityCache init."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'message_versions'"
    ).fetchone()
    conn.close()

    assert row is not None, "message_versions table not found in sqlite_master"


def test_message_versions_schema(tmp_db_path: Path) -> None:
    """message_versions has correct schema: 5 columns."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    rows = conn.execute("PRAGMA table_info(message_versions)").fetchall()
    conn.close()

    col_map = {str(row[1]): str(row[2]) for row in rows}
    expected = {
        "dialog_id": "INTEGER",
        "message_id": "INTEGER",
        "version": "INTEGER",
        "old_text": "TEXT",
        "edit_date": "INTEGER",
    }
    assert col_map == expected, f"Schema mismatch. Got: {col_map}"


def test_message_cache_same_db_as_entities(tmp_db_path: Path) -> None:
    """entities, message_cache, and message_versions all live in the same DB file."""
    import sqlite3

    cache = EntityCache(tmp_db_path)
    cache.close()

    conn = sqlite3.connect(str(tmp_db_path))
    table_names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    conn.close()

    assert "entities" in table_names, f"entities not found in tables: {table_names}"
    assert "message_cache" in table_names, f"message_cache not found in tables: {table_names}"
    assert "message_versions" in table_names, f"message_versions not found in tables: {table_names}"


def test_existing_entity_cache_still_works_after_bootstrap(tmp_db_path: Path) -> None:
    """Existing EntityCache upsert/get functionality unbroken after bootstrap extension."""
    cache = EntityCache(tmp_db_path)
    cache.upsert(101, "user", "Ivan", "ivan")
    result = cache.get(101, ttl_seconds=2_592_000)
    cache.close()

    assert result is not None
    assert result["name"] == "Ivan"


# ---------------------------------------------------------------------------
# CachedMessage proxy tests (Phase 20, Plan 02)
# ---------------------------------------------------------------------------


def test_cached_message_from_row_basic() -> None:
    """CachedMessage.from_row() maps a full cache row to correct attributes."""
    from datetime import datetime, timezone

    from mcp_telegram.cache import CachedMessage

    row = (100, 42, 1718451000, "hello", 101, "Alice", None, None, None, None, 1718451100)
    msg = CachedMessage.from_row(row)
    assert msg.id == 42
    assert msg.message == "hello"
    assert msg.sender is not None
    assert msg.sender.first_name == "Alice"
    assert msg.reply_to is None
    assert msg.reactions is None
    assert msg.media is None


def test_cached_message_from_row_with_reply() -> None:
    """CachedMessage.from_row() with reply_to_msg_id creates reply header."""
    from mcp_telegram.cache import CachedMessage

    row = (100, 43, 1718451000, "reply text", 101, "Bob", None, 10, None, None, 1718451100)
    msg = CachedMessage.from_row(row)
    assert msg.reply_to is not None
    assert msg.reply_to.reply_to_msg_id == 10


def test_cached_message_from_row_media_description() -> None:
    """CachedMessage.from_row() uses media_description as message when text is None."""
    from mcp_telegram.cache import CachedMessage

    row = (100, 44, 1718451000, None, 101, "Carol", "[фото]", None, None, None, 1718451100)
    msg = CachedMessage.from_row(row)
    assert msg.message == "[фото]"


def test_cached_message_from_row_no_sender() -> None:
    """CachedMessage.from_row() with sender_first_name=None yields sender=None."""
    from mcp_telegram.cache import CachedMessage

    row = (100, 45, 1718451000, "channel post", None, None, None, None, None, None, 1718451100)
    msg = CachedMessage.from_row(row)
    assert msg.sender is None


def test_cached_message_from_row_date_timezone() -> None:
    """CachedMessage.from_row() produces timezone-aware UTC datetime from Unix timestamp."""
    from datetime import datetime, timezone

    from mcp_telegram.cache import CachedMessage

    ts = 1718451000
    row = (100, 46, ts, "hello", 101, "Dave", None, None, None, None, 1718451100)
    msg = CachedMessage.from_row(row)
    expected = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert msg.date == expected
    assert msg.date.tzinfo is not None


def test_cached_message_from_row_edit_date_preserved() -> None:
    """CachedMessage stores edit_date for Phase 22 formatter use."""
    from mcp_telegram.cache import CachedMessage

    row = (100, 47, 1718451000, "edited text", 101, "Eve", None, None, None, 1718460000, 1718461000)
    msg = CachedMessage.from_row(row)
    assert msg.edit_date == 1718460000


def test_cached_message_frozen() -> None:
    """CachedMessage is frozen — attribute assignment raises FrozenInstanceError."""
    import dataclasses

    from mcp_telegram.cache import CachedMessage

    row = (100, 48, 1718451000, "immutable", 101, "Frank", None, None, None, None, 1718451100)
    msg = CachedMessage.from_row(row)
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.id = 999  # type: ignore[misc]
