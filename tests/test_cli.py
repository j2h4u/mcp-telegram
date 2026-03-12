from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from typer.testing import CliRunner

import cli as cli_module
from mcp_telegram.cache import EntityCache


runner = CliRunner()


def _make_raw_topic(
    *,
    topic_id: int,
    title: str | None,
    top_message_id: int | None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=topic_id,
        title=title,
        top_message=top_message_id,
        date=datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc),
    )


def test_cli_debug_topic_catalog(tmp_db_path, monkeypatch) -> None:
    cache = EntityCache(tmp_db_path)
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=False)
    mock_client.connect = AsyncMock(return_value=None)
    mock_client.disconnect = AsyncMock(return_value=None)
    mock_client.get_entity = AsyncMock(return_value=SimpleNamespace(id=701))

    first_page = [
        _make_raw_topic(topic_id=1, title="General", top_message_id=None),
        _make_raw_topic(topic_id=11, title="Release Notes", top_message_id=5011),
    ]
    second_page = [
        _make_raw_topic(topic_id=12, title=None, top_message_id=6012),
    ]

    monkeypatch.setattr(cli_module, "create_client", lambda: mock_client)
    monkeypatch.setattr(cli_module, "get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        cli_module,
        "_fetch_forum_topics_page",
        AsyncMock(side_effect=[
            (first_page, 3),
            (second_page, 3),
            ([], 3),
        ]),
    )
    monkeypatch.setattr(
        cli_module,
        "_load_dialog_topics",
        AsyncMock(
            return_value={
                "choices": {1: "General", 11: "Release Notes"},
                "metadata_by_id": {
                    1: {
                        "topic_id": 1,
                        "title": "General",
                        "top_message_id": None,
                        "is_general": True,
                        "is_deleted": False,
                    },
                    11: {
                        "topic_id": 11,
                        "title": "Release Notes",
                        "top_message_id": 5011,
                        "is_general": False,
                        "is_deleted": False,
                    },
                    12: {
                        "topic_id": 12,
                        "title": "Topic 12",
                        "top_message_id": 6012,
                        "is_general": False,
                        "is_deleted": True,
                    },
                },
                "deleted_topics": {
                    12: {
                        "topic_id": 12,
                        "title": "Topic 12",
                        "top_message_id": 6012,
                        "is_general": False,
                        "is_deleted": True,
                    }
                },
            }
        ),
    )

    result = runner.invoke(
        cli_module.app,
        ["debug-topic-catalog", "--dialog", "Backend Forum", "--page-size", "2"],
    )

    assert result.exit_code == 0
    assert "dialog_id=701" in result.stdout
    assert "page=1 offset_topic=0 offset_id=0 fetched=2 total_count=3" in result.stdout
    assert 'topic_id=1 title="General" top_message_id=None is_general=True is_deleted=False' in result.stdout
    assert 'topic_id=11 title="Release Notes" top_message_id=5011 is_general=False is_deleted=False' in result.stdout
    assert "page=2 offset_topic=11 offset_id=5011 fetched=1 total_count=3" in result.stdout
    assert 'topic_id=12 title="Topic 12" top_message_id=6012 is_general=False is_deleted=True' in result.stdout
    assert "normalized_catalog_count=3 active_count=2 deleted_count=1" in result.stdout


def test_cli_debug_topic_by_id(tmp_db_path, monkeypatch) -> None:
    cache = EntityCache(tmp_db_path)
    mock_client = AsyncMock()
    mock_client.is_connected = MagicMock(return_value=False)
    mock_client.connect = AsyncMock(return_value=None)
    mock_client.disconnect = AsyncMock(return_value=None)
    mock_client.get_entity = AsyncMock(return_value=SimpleNamespace(id=701))

    cached_topic = {
        "topic_id": 11,
        "title": "Release Notes",
        "top_message_id": 5011,
        "is_general": False,
        "is_deleted": False,
    }
    refreshed_topic = {
        "topic_id": 11,
        "title": "Release Notes",
        "top_message_id": 6011,
        "is_general": False,
        "is_deleted": False,
    }
    refresh_topic = AsyncMock(return_value=refreshed_topic)

    monkeypatch.setattr(cli_module, "create_client", lambda: mock_client)
    monkeypatch.setattr(cli_module, "get_entity_cache", lambda: cache)
    monkeypatch.setattr(
        cli_module,
        "_load_dialog_topics",
        AsyncMock(
            return_value={
                "choices": {11: "Release Notes"},
                "metadata_by_id": {11: cached_topic},
                "deleted_topics": {},
            }
        ),
    )
    monkeypatch.setattr(cli_module, "_refresh_topic_by_id", refresh_topic)

    result = runner.invoke(
        cli_module.app,
        ["debug-topic-by-id", "--dialog", "Backend Forum", "--topic-id", "11"],
    )

    assert result.exit_code == 0
    assert "dialog_id=701 topic_id=11" in result.stdout
    assert "cached=" in result.stdout
    assert "refreshed=" in result.stdout
    assert '"top_message_id": 5011' in result.stdout
    assert '"top_message_id": 6011' in result.stdout
    assert '"title": "Release Notes"' in result.stdout
    refresh_topic.assert_awaited_once()
