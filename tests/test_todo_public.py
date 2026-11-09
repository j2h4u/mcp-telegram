import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl import types as tl

from mcp_telegram import server
from mcp_telegram.chat_export_projection import omit_empty_fields, project_message
from mcp_telegram.daemon_message import project_read_message_content
from mcp_telegram.inbox_projection import _drop_largest_inbox_preview
from mcp_telegram.message_composition import normalize_telegram_fact
from mcp_telegram.message_view import project_message_view
from mcp_telegram.messages.sqlite_bundle import insert_messages_with_fts
from mcp_telegram.messages.telegram_adapter import extract_message_row
from mcp_telegram.reading import ReadingDeps, ReadingService
from mcp_telegram.reading.query_records import read_message_from_row
from mcp_telegram.reading.scheduled_projection import scheduled_row_to_wire
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.tools import reading
from mcp_telegram.tools._base import omit_none_mapping_values
from mcp_telegram.tools.search_hit import project_search_hit


def test_todo_payload_survives_public_projections() -> None:
    native = tl.MessageMediaToDo(
        tl.TodoList(
            tl.TextWithEntities("😀 Plan", [tl.MessageEntityBold(3, 4)]),
            [tl.TodoItem(23, tl.TextWithEntities("Find me", []))],
        ),
    )
    payload = normalize_telegram_fact(native.to_dict())
    row = {
        "message_id": 1,
        "dialog_id": 2,
        "sent_at": 3,
        "text": "",
        "media_kind": "todo",
        "media_payload": json.dumps(payload),
    }
    message = project_read_message_content(read_message_from_row(row))
    result = cast(dict[str, object], omit_none_mapping_values(project_message_view(message)))
    assert result["media"] == {"type": "todo", "todo": payload, "todo_status": "captured"}
    assert cast(dict[str, object], result["content"])["text"] == "😀 Plan\nFind me"
    scheduled = scheduled_row_to_wire({**row, "scheduled_at": 5})
    scheduled_message = project_message_view(read_message_from_row(scheduled))
    scheduled_result = cast(dict[str, object], omit_none_mapping_values(scheduled_message))
    assert cast(dict[str, object], scheduled_result["media"])["todo"] == payload
    search = project_search_hit(
        {**row, "text": message.text, "media_payload": payload},
        "Find",
        lifecycle={
            "message_state": "sent",
            "visibility": "visible",
            "unpublished": False,
            "published": True,
            "unseen": False,
            "scheduled_at": None,
            "published_at": None,
            "inclusion_basis": [],
        },
    )
    assert cast(dict[str, object], omit_none_mapping_values(search))["media"] == {
        "type": "todo",
        "todo_status": "captured",
    }
    export = project_message({"id": 1, "dialog_id": 2, "raw": {"media": payload}})
    export_result = cast(dict[str, object], omit_empty_fields(export))
    assert cast(dict[str, object], export_result["metadata"])["media"] == payload


def test_todo_unknown_and_unsupported_are_explicit() -> None:
    for payload, expected in (({}, "unknown"), ({"_": "FutureMedia"}, "unsupported")):
        row = {"message_id": 1, "dialog_id": 2, "sent_at": 3, "media_kind": "todo", "media_payload": payload}
        media = project_message_view(read_message_from_row(row))["media"]
        assert media == {"type": "todo", "todo": payload, "todo_status": expected}


def test_inbox_budget_omits_whole_checklist_without_clipping_tasks() -> None:
    payload = {"_": "MessageMediaToDo", "todo": {"list": [{"title": "x" * 10000}]}}
    messages = [{"media": {"type": "todo", "todo": payload, "todo_status": "captured"}}]
    budget = {"total_in_chat": 1}
    dialogs: list[object] = [{"messages": messages, "budget": budget, "category": "user", "unread_mentions_count": 0}]
    assert _drop_largest_inbox_preview(dialogs)
    assert messages == []
    assert budget["hidden_count"] == 1
    assert payload["todo"]["list"][0]["title"] == "x" * 10000


async def test_native_producer_sqlite_fts_and_registered_mcp(
    make_synced_db: Callable[[], sqlite3.Connection], monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = make_synced_db()
    conn.row_factory = sqlite3.Row
    conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (1,'Chat','user')")
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status,last_synced_at) VALUES (1,'synced',100)")
    conn.execute("UPDATE scheduled_sync_state SET account_id=2 WHERE key='account'")
    conn.commit()
    native = tl.MessageMediaToDo(
        tl.TodoList(tl.TextWithEntities("Title", []), [tl.TodoItem(23, tl.TextWithEntities("uniquefind", []))])
    )
    message = tl.Message(7, tl.PeerUser(1), date=datetime(2026, 1, 1, tzinfo=UTC), message="", media=native)
    with write_transaction(conn):
        insert_messages_with_fts(conn, [extract_message_row(1, message)])
    service = ReadingService(
        ReadingDeps(
            conn=conn,
            sync_db_path=None,
            self_id=2,
            resolve_dialog_id=AsyncMock(return_value=1),
            logger=MagicMock(),
            rid=lambda: "",
            deleted_message_visibility_seconds=86400,
            draft_response_budget_bytes=1024,
        )
    )

    async def search_messages(**kwargs: object) -> dict:
        return await service.search_messages(kwargs)

    async def list_messages(**kwargs: object) -> dict:
        return await service.list_messages(kwargs)

    @asynccontextmanager
    async def connection() -> AsyncIterator[SimpleNamespace]:
        yield SimpleNamespace(search_messages=search_messages, list_messages=list_messages)

    monkeypatch.setattr(reading, "daemon_connection", connection)
    monkeypatch.setattr(server, "_send_telemetry_event", AsyncMock())
    result = await server.call_tool("search_messages", {"query": "uniquefind", "message_state": "sent"})
    assert not result.is_error
    content = cast(dict[str, object] | None, result.structured_content)
    assert content is not None
    assert content["count"] == 1
    hit = cast(list[dict[str, object]], content["results"])[0]
    assert hit["media"] == {"type": "todo", "todo_status": "captured"}
    anchor = cast(dict[str, object], hit["anchor_call"])
    detail = await server.call_tool(cast(str, anchor["tool"]), cast(dict[str, object], anchor["arguments"]))
    assert not detail.is_error
    detail_content = cast(dict[str, object] | None, detail.structured_content)
    assert detail_content is not None
    detail_message = cast(list[dict[str, object]], detail_content["messages"])[0]
    assert cast(dict[str, object], detail_message["media"])["todo"] == normalize_telegram_fact(native.to_dict())
