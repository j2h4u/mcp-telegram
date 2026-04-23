from __future__ import annotations

import resource
import sqlite3
import sys

# Hard virtual memory limit: 512 MB per test process.
# Prevents runaway tests (e.g., infinite loops with MagicMock) from
# consuming all RAM and pushing the system into swap.
if sys.platform != "win32":
    _MAX_AS_BYTES = 512 * 1024 * 1024
    _soft, _hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (_MAX_AS_BYTES, _hard))

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.errors import RPCError


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
        row = self._conn.execute("SELECT type, name, username FROM entities WHERE id = ?", (entity_id,)).fetchone()
        if row is None:
            return None
        return {"type": row[0], "name": row[1], "username": row[2]}

    def get_by_username(self, username: str) -> tuple[int, str] | None:
        row = self._conn.execute(
            "SELECT id, name FROM entities WHERE username = ? COLLATE NOCASE",
            (username,),
        ).fetchone()
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
        return {row[0]: row[1] for row in rows}


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


async def async_iter(items):
    """Async generator that yields items from a list."""
    for item in items:
        yield item


@pytest.fixture()
def mock_cache(tmp_db_path: Path) -> _MockEntityCache:
    """Return _MockEntityCache seeded with entity 101 (Иван Петров)."""
    cache = _MockEntityCache(tmp_db_path)
    cache.upsert(101, "user", "Иван Петров", "ivan")
    return cache


@pytest.fixture()
def make_mock_message():
    """Return a factory function for creating mock Telethon messages."""

    def _make(
        id: int,
        text: str,
        sender_id: int = 101,
        sender_name: str = "Иван",
        date: datetime | None = None,
    ) -> MagicMock:
        msg = MagicMock()
        msg.id = id
        msg.text = text
        msg.message = text  # Telethon exposes message text via .message; formatter reads this
        msg.sender_id = sender_id
        msg.sender = MagicMock(first_name=sender_name, last_name=None, username=None)
        msg.date = date or datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        msg.reply_to = None
        msg.reactions = None
        msg.media = None
        return msg

    return _make


@pytest.fixture()
def make_mock_forum_reply():
    """Return a factory for lightweight forum reply headers."""

    def _make(
        *,
        reply_to_msg_id: int | None = None,
        reply_to_top_id: int | None = None,
        forum_topic: bool = True,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            reply_to_msg_id=reply_to_msg_id,
            reply_to_top_id=reply_to_top_id,
            forum_topic=forum_topic,
        )

    return _make


@pytest.fixture()
def make_mock_topic():
    """Return a factory for dialog-scoped topic metadata rows."""

    def _make(
        *,
        topic_id: int,
        title: str,
        top_message_id: int | None,
        is_general: bool = False,
        is_deleted: bool = False,
    ) -> dict[str, int | str | bool | None]:
        return {
            "topic_id": topic_id,
            "title": title,
            "top_message_id": top_message_id,
            "is_general": is_general,
            "is_deleted": is_deleted,
        }

    return _make


@pytest.fixture()
def make_deleted_topic(make_mock_topic):
    """Return a factory for tombstoned topic metadata rows."""

    def _make(
        *,
        topic_id: int,
        title: str,
        top_message_id: int | None,
    ) -> dict[str, int | str | bool | None]:
        return make_mock_topic(
            topic_id=topic_id,
            title=title,
            top_message_id=top_message_id,
            is_deleted=True,
        )

    return _make


@pytest.fixture()
def make_general_topic_message(make_mock_message):
    """Return a factory for General topic messages without thread reply headers."""

    def _make(
        *,
        id: int,
        text: str,
        sender_id: int = 101,
        sender_name: str = "Иван",
        date: datetime | None = None,
    ) -> MagicMock:
        message = make_mock_message(
            id=id,
            text=text,
            sender_id=sender_id,
            sender_name=sender_name,
            date=date,
        )
        message.reply_to = None
        return message

    return _make


@pytest.fixture()
def make_private_topic_error():
    """Return a factory for Telethon RPC errors raised on inaccessible topics."""

    def _make(message: str = "TOPIC_PRIVATE", code: int = 400) -> RPCError:
        return RPCError(request=None, message=message, code=code)

    return _make


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
    from mcp_telegram.sync_db import _apply_migrations

    def _factory() -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        _apply_migrations(conn)
        return conn

    return _factory


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
