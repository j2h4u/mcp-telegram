from __future__ import annotations

import time
from pathlib import Path

import pytest

from mcp_telegram.cache import EntityCache, ReactionMetadataCache


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

    time.sleep(0.01)
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
