from __future__ import annotations

import pytest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from mcp_telegram.cache import EntityCache


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
def mock_cache(tmp_db_path: Path) -> EntityCache:
    """Return EntityCache seeded with entity 101 (Иван Петров)."""
    cache = EntityCache(tmp_db_path)
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
        msg.sender_id = sender_id
        msg.sender = MagicMock(first_name=sender_name, last_name=None, username=None)
        msg.date = date or datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc)
        msg.reply_to = None
        msg.reactions = None
        msg.media = None
        return msg

    return _make


@pytest.fixture()
def mock_client() -> AsyncMock:
    """Return a mock Telethon TelegramClient configured as an async context manager."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.iter_dialogs = MagicMock(return_value=async_iter([]))
    client.iter_messages = MagicMock(return_value=async_iter([]))
    client.__call__ = AsyncMock(return_value=MagicMock())
    return client
