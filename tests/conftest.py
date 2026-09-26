from __future__ import annotations

import resource
import sqlite3
import sys
from importlib import import_module

# Hard virtual memory limit: 512 MB per test process.
# Prevents runaway tests (e.g., infinite loops with MagicMock) from
# consuming all RAM and pushing the system into swap.
if sys.platform != "win32":
    _MAX_AS_BYTES = 512 * 1024 * 1024
    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (_MAX_AS_BYTES, _hard))

# Load Hypothesis before collection so its native extension does not first map
# during terminal-summary hooks after coverage has accumulated.
import_module("hypothesis")

from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest


def pytest_exception_interact(node: pytest.Item, call: pytest.CallInfo[object], report: pytest.TestReport) -> None:
    """Expose SQLite extended error identity in CI failure logs."""
    del node, report
    if call.excinfo is None:
        return
    error: BaseException | None = call.excinfo.value
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, sqlite3.Error):
            print(
                "sqlite_diagnostic "
                f"sqlite_errorcode={getattr(error, 'sqlite_errorcode', None)} "
                f"sqlite_errorname={getattr(error, 'sqlite_errorname', None)}",
                flush=True,
            )
            return
        error = error.__cause__ or error.__context__


@pytest.fixture(autouse=True)
def _mcp_telegram_test_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Give tests an explicit state-dir config; production fails fast without one."""
    config_home = tmp_path / "config"
    state_dir = tmp_path / "state"
    config_dir = config_home / "mcp-telegram"
    config_dir.mkdir(parents=True)
    (config_dir / "config.toml").write_text(f'[state]\ndir = "{state_dir}"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))


class _MockEntityCache:
    """Minimal stand-in for deleted EntityCache — used by resolver tests."""

    def __init__(self, db_path: Path) -> None:
        import sqlite3

        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT NOT NULL,
                username TEXT,
                updated_at INTEGER NOT NULL
            )
        """)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_username ON entities(username)")
        self._conn.commit()

    def upsert(self, entity_id: int, entity_type: str, name: str, username: str | None = None) -> None:
        import time

        self._conn.execute(
            "INSERT OR REPLACE INTO entities (id, type, name, username, updated_at) VALUES (?, ?, ?, ?, ?)",
            (entity_id, entity_type, name, username, int(time.time())),
        )
        self._conn.commit()

    def get(self, entity_id: int, ttl_seconds: int = 300) -> dict | None:
        row = cast(
            tuple[str, str, str | None] | None,
            self._conn.execute("SELECT type, name, username FROM entities WHERE id = ?", (entity_id,)).fetchone(),
        )
        if row is None:
            return None
        return {"type": row[0], "name": row[1], "username": row[2]}

    def get_by_username(self, username: str) -> tuple[int, str] | None:
        row = cast(
            tuple[int, str] | None,
            self._conn.execute(
                "SELECT id, name FROM entities WHERE username = ? COLLATE NOCASE",
                (username,),
            ).fetchone(),
        )
        if row is None:
            return None
        return (row[0], row[1])

    def all_names_with_ttl(self, user_ttl: int, group_ttl: int) -> dict[int, str]:
        import time

        now = int(time.time())
        rows = self._conn.execute(
            "SELECT id, name FROM entities WHERE (type='user' AND updated_at > ?) OR (type!='user' AND updated_at > ?)",
            (now - user_ttl, now - group_ttl),
        ).fetchall()
        typed_rows = cast(list[tuple[int, str]], rows)
        return {row[0]: row[1] for row in typed_rows}

    def close(self) -> None:
        self._conn.close()


@pytest.fixture()
def tmp_db_path(tmp_path: Path) -> Path:
    """Return a path to a temporary SQLite file (not yet created)."""
    return tmp_path / "entity_cache.db"


@pytest.fixture()
def sample_entities() -> dict[int, str]:
    """Return {entity_id: display_name} mapping for resolver tests."""
    return {
        101: "Иван Петров",
        102: "Ivan's Team",
        103: "Анна Иванова",
        104: "Work Group",
    }


async def async_iter(items: Iterable[object]) -> AsyncIterator[object]:
    """Async generator that yields items from a list."""
    for item in items:
        yield item


@pytest.fixture()
def mock_cache(tmp_db_path: Path):
    """Return _MockEntityCache seeded with entity 101 (Иван Петров)."""
    cache = _MockEntityCache(tmp_db_path)
    cache.upsert(101, "user", "Иван Петров", "ivan")
    try:
        yield cache
    finally:
        cache.close()


@pytest.fixture
def make_synced_db():
    """Factory fixture: call make_synced_db() to get a fresh in-memory DB at current schema.

    Replaces per-file _make_db() helpers. Schema always matches production via
    _apply_migrations — no manual DDL, no drift.

    Usage::

        def test_something(make_synced_db):
            conn = make_synced_db()
            ...
    """
    connections = []
    from mcp_telegram.sync_db import _apply_migrations

    def _factory() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        _apply_migrations(conn)
        connections.append(conn)
        return conn

    try:
        yield _factory
    finally:
        for conn in connections:
            conn.close()
        connections.clear()


@pytest.fixture()
def mock_client() -> AsyncMock:
    """Return a mock Telethon TelegramClient configured as an async context manager."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.is_connected = MagicMock(return_value=False)
    client.connect = AsyncMock(return_value=None)
    client.disconnect = AsyncMock(return_value=None)
    client.iter_dialogs = MagicMock(return_value=async_iter([]))
    client.iter_messages = MagicMock(return_value=async_iter([]))
    client.__call__ = AsyncMock(return_value=MagicMock())
    return client


@pytest.fixture
def make_feedback_db(tmp_path: Path):
    """Factory: returns (conn, db_path) for a freshly-migrated feedback.db."""
    connections = []

    def _factory() -> tuple[sqlite3.Connection, Path]:
        from mcp_telegram.feedback_db import ensure_feedback_schema

        db_path = tmp_path / "feedback.db"
        conn = ensure_feedback_schema(db_path)
        connections.append(conn)
        return conn, db_path

    try:
        yield _factory
    finally:
        for conn in connections:
            conn.close()
        connections.clear()
