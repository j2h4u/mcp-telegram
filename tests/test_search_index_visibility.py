"""Retained local search reports real SQL failures independently of scheduled search."""

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Literal, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import validate
from mcp.types import TextContent

from mcp_telegram.reading import ReadingDeps, ReadingService
from mcp_telegram.tools import omit_none_mapping_values, reading
from mcp_telegram.tools._base import TOOL_REGISTRY


@pytest.fixture
def local_reading(make_synced_db: Callable[[], sqlite3.Connection]) -> ReadingService:
    conn = make_synced_db()
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (1,'Retained chat','user')")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status,last_synced_at) VALUES (1,'synced',100)")
    conn.execute(
        "INSERT INTO messages(dialog_id,message_id,sent_at,text,out,is_service,is_deleted) "
        "VALUES (1,7,100,'needle',0,0,0)"
    )
    conn.execute("INSERT INTO messages_fts(dialog_id,message_id,stemmed_text) VALUES (1,7,'needle')")
    conn.execute("UPDATE scheduled_sync_state SET account_id=2 WHERE key='account'")
    return ReadingService(
        ReadingDeps(
            conn=conn,
            sync_db_path=None,
            self_id=2,
            resolve_dialog_id=AsyncMock(return_value=1),
            logger=MagicMock(),
            rid=lambda: "",
            deleted_message_visibility_seconds=86_400,
            draft_response_budget_bytes=1024,
        )
    )


def _connect_tool(monkeypatch: pytest.MonkeyPatch, service: ReadingService) -> None:
    async def search_messages(**kwargs: object) -> dict:
        return await service.search_messages(kwargs)

    @asynccontextmanager
    async def connection() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(search_messages=search_messages)

    monkeypatch.setattr(reading, "daemon_connection", connection)


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("message_state", ["sent", "all"])
@pytest.mark.parametrize("query", ["needle", "absent"])
async def test_local_search_preserves_hits_and_coverage(
    local_reading: ReadingService,
    monkeypatch: pytest.MonkeyPatch,
    scoped: bool,
    message_state: Literal["sent", "all"],
    query: str,
) -> None:
    _connect_tool(monkeypatch, local_reading)
    args = reading.SearchMessages(query=query, dialog="1" if scoped else None, message_state=message_state)
    result = await reading.search_messages(args)
    payload = result.structured_content
    assert not result.is_error
    assert not result.content
    assert payload is not None
    assert payload["count"] == (1 if query == "needle" else 0)
    output_schema = TOOL_REGISTRY["search_messages"].output_schema
    assert output_schema is not None
    validate(omit_none_mapping_values(payload), output_schema)


@pytest.mark.parametrize("scoped", [False, True])
async def test_scheduled_search_does_not_depend_on_sent_index(
    local_reading: ReadingService, monkeypatch: pytest.MonkeyPatch, scoped: bool
) -> None:
    local_reading._conn.execute("DROP TABLE messages_fts")
    _connect_tool(monkeypatch, local_reading)
    result = await reading.search_messages(
        reading.SearchMessages(query="needle", dialog="1" if scoped else None, message_state="scheduled")
    )
    assert not result.is_error
    assert result.structured_content is not None
    assert "search_index_state" not in result.structured_content


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("message_state", ["sent", "all"])
async def test_actual_fts_query_failure_returns_specific_error_and_preserves_list_reads(
    local_reading: ReadingService, monkeypatch: pytest.MonkeyPatch, scoped: bool, message_state: Literal["sent", "all"]
) -> None:
    local_reading._conn.execute("DROP TABLE messages_fts")
    _connect_tool(monkeypatch, local_reading)
    result = await reading.search_messages(
        reading.SearchMessages(query="needle", dialog="1" if scoped else None, message_state=message_state)
    )
    assert result.is_error
    assert result.error_code == "local_search_index_unavailable"
    error_content = result.content[0]
    assert isinstance(error_content, TextContent)
    assert "list_messages" in error_content.text
    assert "list_dialogs" not in error_content.text
    listed = await local_reading.list_messages({"dialog_id": 1, "message_state": "sent"})
    assert listed["ok"]
    assert listed["data"]["messages"][0]["message_id"] == 7
    assert "search_index_state" not in listed["data"]


async def test_non_fts_directory_errors_are_not_misclassified(local_reading: ReadingService) -> None:
    local_reading._conn.execute("DROP TABLE dialogs")
    with pytest.raises(sqlite3.Error):
        await local_reading.search_messages({"query": "needle", "message_state": "sent"})


async def test_sent_search_wire_omits_unavailable_scheduled_metadata(
    local_reading: ReadingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduled_metadata = {"repeat_period", "expected_version", "scheduled_coverage", "dialog_type"}
    response = await local_reading.search_messages({"query": "needle", "message_state": "sent"})
    assert response["ok"]
    row = response["data"]["messages"][0]
    assert scheduled_metadata.isdisjoint(row)
    _connect_tool(monkeypatch, local_reading)
    result = await reading.search_messages(reading.SearchMessages(query="needle", message_state="sent"))
    assert not result.is_error
    assert result.structured_content is not None
    payload = cast(dict[str, object], omit_none_mapping_values(result.structured_content))
    hit = cast(list[dict[str, object]], payload["results"])[0]
    assert scheduled_metadata.isdisjoint(hit)
    assert cast(dict[str, object], hit["content"])["content_kind"] == "snippet"
    assert hit["anchor_call"] == {
        "tool": "list_messages",
        "arguments": {"exact_dialog_id": 1, "anchor_message_id": 7, "message_state": "sent"},
    }
    output_schema = TOOL_REGISTRY["search_messages"].output_schema
    assert output_schema is not None
    validate(payload, output_schema)
