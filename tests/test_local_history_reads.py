"""Daemon history reads project retained facts without triggering acquisition."""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
from jsonschema import validate

from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.pagination import HistoryDirection, encode_history_navigation
from mcp_telegram.temporal import normalize_temporal_output_schema
from mcp_telegram.tools.reading import LIST_MESSAGES_OUTPUT_SCHEMA, ListMessages, list_messages
from tests.test_daemon_api import (
    _insert_message,
    _insert_synced_dialog,
    _make_db,
    _response_data,
    _response_messages,
    _TestClient,
    make_server,
)

type LocalReader = tuple[sqlite3.Connection, DaemonAPIServer]


@pytest.fixture
async def local_reader() -> AsyncIterator[LocalReader]:
    conn = _make_db()
    client = _TestClient()
    client.iter_messages = MagicMock(side_effect=AssertionError("history RPC forbidden"))
    client.get_messages = AsyncMock(side_effect=AssertionError("context RPC forbidden"))
    client.get_input_entity = AsyncMock(side_effect=AssertionError("entity RPC forbidden"))
    client.get_entity = AsyncMock(side_effect=AssertionError("entity RPC forbidden"))
    client.iter_dialogs = MagicMock(side_effect=AssertionError("dialog RPC forbidden"))
    hydration = MagicMock(side_effect=AssertionError("read reconciliation forbidden"))
    server = make_server(conn, client, hydration_requester=hydration)
    offer = MagicMock(side_effect=AssertionError("read demand forbidden"))
    sink = MagicMock(offer=offer)
    server.bind_demand_sink(sink)
    conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (42,'Local dialog','user')")
    conn.commit()
    try:
        yield conn, server
        client.iter_messages.assert_not_called()
        client.get_messages.assert_not_called()
        client.get_input_entity.assert_not_called()
        client.get_entity.assert_not_called()
        client.iter_dialogs.assert_not_called()
        hydration.assert_not_called()
        offer.assert_not_called()
    finally:
        await server.shutdown()
        conn.close()


async def _read(reader: LocalReader, **request: object) -> dict[str, object]:
    conn, server = reader
    writes: list[tuple[int, str | None]] = []

    def authorize(action: int, table: str | None, column: str | None, database: str | None, trigger: str | None) -> int:
        del column, database, trigger
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            writes.append((action, table))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    before = conn.total_changes
    conn.set_authorizer(authorize)
    try:
        result = await server._list_messages({"dialog_id": 42, "message_state": "sent", **request})
    finally:
        conn.set_authorizer(None)
    assert writes == []
    assert conn.total_changes == before
    return result


@pytest.mark.parametrize("status", (None, "not_synced", "own_only", "fragment", "syncing", "synced", "access_lost"))
@pytest.mark.parametrize("message_state", ("sent", "all"))
async def test_retained_history_is_visible_in_every_coverage_state(
    local_reader: LocalReader, status: str | None, message_state: str
) -> None:
    conn, _ = local_reader
    if status is not None:
        _insert_synced_dialog(conn, 42, status=status, enrollment_enabled=False, last_synced_at=200)
    _insert_message(conn, 42, 7, text="Retained", sent_at=100)

    result = await _read(local_reader, message_state=message_state)

    assert result["ok"] is True
    data = _response_data(result)
    assert data["source"] == ("sync_db" if message_state == "sent" else "sync_db+scheduled_messages+draft_current")
    assert data["selection_state"] == "present"
    expected_coverage = "unknown" if status in (None, "not_synced") else "partial"
    assert data["coverage"] == ("complete_as_of" if status == "synced" else expected_coverage)
    assert data["dialog_access"] == ("archived" if status == "access_lost" else "unknown")
    assert [message["message_id"] for message in _response_messages(result)] == [7]
    assert data["observation_horizon_at"] == (200 if status == "synced" else None)


@pytest.mark.parametrize("status", (None, "not_synced", "own_only", "fragment", "syncing", "synced", "access_lost"))
async def test_empty_history_requires_a_completed_observation(local_reader: LocalReader, status: str | None) -> None:
    conn, _ = local_reader
    if status is not None:
        _insert_synced_dialog(conn, 42, status=status, enrollment_enabled=False, last_synced_at=200)

    result = await _read(local_reader)

    assert result["ok"] is True
    data = _response_data(result)
    assert data["messages"] == []
    assert data["selection_state"] == ("known_empty" if status == "synced" else "unknown")
    assert data["observation_horizon_at"] == (200 if status == "synced" else None)


@pytest.mark.parametrize("filters", ({}, {"topic_id": 5}, {"sender_name": "Unknown"}))
async def test_synced_marker_without_timestamp_is_not_complete(
    local_reader: LocalReader, filters: dict[str, object]
) -> None:
    conn, _ = local_reader
    _insert_synced_dialog(conn, 42, status="synced")

    result = await _read(local_reader, **filters)

    data = _response_data(result)
    assert data["selection_state"] == "unknown"
    assert data["coverage"] != "complete_as_of"
    assert data["observation_horizon_at"] is None


@pytest.mark.parametrize("filters", ({"topic_id": 5}, {"sender_name": "Unknown"}))
async def test_incomplete_filter_attribution_cannot_establish_absence(
    local_reader: LocalReader, filters: dict[str, object]
) -> None:
    conn, _ = local_reader
    _insert_synced_dialog(conn, 42, status="synced", last_synced_at=200)
    _insert_message(conn, 42, 1, text="Unattributed")
    conn.execute("UPDATE messages SET sender_first_name=NULL WHERE dialog_id=42")

    result = await _read(local_reader, **filters)

    assert _response_data(result)["selection_state"] == "unknown"


@pytest.mark.parametrize("message_state", ("sent", "all"))
async def test_numeric_sender_filter_cannot_prove_absence_with_unattributed_group_rows(
    local_reader: LocalReader, message_state: str
) -> None:
    conn, _ = local_reader
    conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (-42,'Local group','group')")
    _insert_synced_dialog(conn, -42, status="synced", last_synced_at=200)
    _insert_message(conn, -42, 1, text="Unknown author", sent_at=100)

    result = await _read(local_reader, dialog_id=-42, sender_id=7, message_state=message_state)

    assert result["ok"] is True
    assert _response_messages(result) == []
    assert _response_data(result)["selection_state"] == "unknown"


@pytest.mark.parametrize("status", (None, "not_synced", "own_only", "fragment", "syncing", "synced", "access_lost"))
async def test_context_uses_retained_chronological_neighbors(local_reader: LocalReader, status: str | None) -> None:
    conn, _ = local_reader
    if status is not None:
        _insert_synced_dialog(conn, 42, status=status, enrollment_enabled=False)
    for message_id, timestamp in ((90, 100), (1, 200), (7, 200), (2, 300), (3, 400)):
        _insert_message(conn, 42, message_id, sent_at=timestamp)

    result = await _read(local_reader, context_message_id=7, context_size=4)

    assert result["ok"] is True
    assert [message["message_id"] for message in _response_messages(result)] == [90, 1, 7, 2]
    assert _response_data(result)["anchor_message_id"] == 7
    assert _response_data(result)["source"] == "sync_db"


@pytest.mark.parametrize("anchor_state", ("missing", "deleted", "outside_time_range"))
async def test_context_unavailable_anchor_has_a_distinct_recoverable_reason(
    local_reader: LocalReader, anchor_state: str
) -> None:
    conn, _ = local_reader
    if anchor_state != "missing":
        _insert_message(conn, 42, 7, sent_at=100)
    if anchor_state == "deleted":
        conn.execute("UPDATE messages SET is_deleted=1 WHERE dialog_id=42 AND message_id=7")
    _insert_message(conn, 42, 8, sent_at=200)

    result = await _read(local_reader, context_message_id=7, since_utc="1970-01-01T00:03:00Z")

    assert result["ok"] is False
    assert result["error"] == "local_context_unavailable"
    assert result["context_availability"] == f"anchor_{anchor_state}"
    assert result["required_action"]


@pytest.mark.parametrize(
    "filters", ({"sender_id": 7}, {"sender_name": "Alice"}, {"topic_id": 5}, {"unread": True}, {"unread_after_id": 0})
)
async def test_context_rejects_unsupported_filters_before_dialog_lookup(
    local_reader: LocalReader, filters: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, server = local_reader
    resolver = AsyncMock(side_effect=AssertionError("invalid request must not resolve a dialog"))
    monkeypatch.setattr(server, "_resolve_dialog_id_local", resolver)

    result = await _read(local_reader, context_message_id=7, **filters)

    assert result["ok"] is False
    assert result["error"] == "invalid_context_filters"
    resolver.assert_not_called()


@pytest.mark.parametrize("cursor", (None, 0))
@pytest.mark.parametrize("message_state", ("sent", "all"))
async def test_unknown_unread_stays_pending_but_zero_cursor_is_known(
    local_reader: LocalReader, cursor: int | None, message_state: str
) -> None:
    conn, _ = local_reader
    _insert_synced_dialog(conn, 42, status="fragment")
    conn.execute("UPDATE synced_dialogs SET read_inbox_max_id=? WHERE dialog_id=42", (cursor,))
    _insert_message(conn, 42, 1, sent_at=100)

    result = await _read(local_reader, unread=True, message_state=message_state)

    if cursor is None:
        assert result["ok"] is False
        assert result["error"] == "read_position_pending"
    else:
        assert result["ok"] is True
        assert [message["message_id"] for message in _response_messages(result)] == [1]


@pytest.mark.parametrize("direction", ("newest", "oldest"))
async def test_composite_local_pagination_survives_deleted_anchor_and_exhausts(
    local_reader: LocalReader, direction: str
) -> None:
    conn, _ = local_reader
    for message_id, timestamp in ((4, 100), (1, 200), (3, 200), (2, 300)):
        _insert_message(conn, 42, message_id, sent_at=timestamp)
    first = await _read(local_reader, limit=2, direction=direction)
    first_ids = [message["message_id"] for message in _response_messages(first)]
    token = _response_data(first)["next_navigation"]
    assert isinstance(token, str)
    anchor_id = first_ids[-1]
    conn.execute("DELETE FROM messages WHERE dialog_id=42 AND message_id=?", (anchor_id,))

    second = await _read(local_reader, limit=2, navigation=token, direction=direction)

    second_ids = [message["message_id"] for message in _response_messages(second)]
    assert first_ids == ([4, 1] if direction == "oldest" else [2, 3])
    assert second_ids == ([3, 2] if direction == "oldest" else [1, 4])
    final_token = _response_data(second)["next_navigation"]
    assert isinstance(final_token, str)
    exhausted = await _read(local_reader, limit=2, navigation=final_token, direction=direction)
    assert _response_messages(exhausted) == []
    assert _response_data(exhausted)["next_navigation"] is None
    assert _response_data(exhausted)["selection_state"] == "unknown"


async def test_local_history_time_bounds_are_inclusive_exclusive(local_reader: LocalReader) -> None:
    conn, _ = local_reader
    for message_id, timestamp in ((1, 99), (2, 100), (3, 199), (4, 200)):
        _insert_message(conn, 42, message_id, sent_at=timestamp)
    result = await _read(
        local_reader, direction="oldest", since_utc="1970-01-01T00:01:40Z", until_utc="1970-01-01T00:03:20Z"
    )
    assert [message["message_id"] for message in _response_messages(result)] == [2, 3]


async def test_navigation_does_not_cross_local_time_scope(local_reader: LocalReader) -> None:
    token = encode_history_navigation(7, 42, direction=HistoryDirection.NEWEST, message_state="sent", since_utc=100)
    result = await _read(local_reader, navigation=token, since_utc="1970-01-01T00:01:41Z")
    assert result["ok"] is False
    assert result["error"] == "invalid_navigation"


async def test_empty_time_range_after_observation_horizon_is_unknown(local_reader: LocalReader) -> None:
    conn, _ = local_reader
    _insert_synced_dialog(conn, 42, status="synced", last_synced_at=100)
    result = await _read(local_reader, since_utc="1970-01-01T00:01:41Z")
    assert _response_data(result)["selection_state"] == "unknown"


async def test_context_respects_valid_utc_bounds(local_reader: LocalReader) -> None:
    conn, _ = local_reader
    for message_id, timestamp in ((1, 100), (2, 150), (3, 199), (4, 200)):
        _insert_message(conn, 42, message_id, sent_at=timestamp)
    result = await _read(
        local_reader,
        context_message_id=2,
        context_size=4,
        since_utc="1970-01-01T00:02:30Z",
        until_utc="1970-01-01T00:03:20Z",
    )
    assert [message["message_id"] for message in _response_messages(result)] == [2, 3]


@pytest.mark.parametrize("anchor_present", (False, True))
async def test_tool_preserves_real_local_context_availability_and_schema(
    local_reader: LocalReader, monkeypatch: pytest.MonkeyPatch, anchor_present: bool
) -> None:
    conn, _ = local_reader
    _insert_synced_dialog(conn, 42, status="access_lost")
    if anchor_present:
        _insert_message(conn, 42, 7, text="Archived context", sent_at=100)

    class LocalConnection:
        async def list_messages(self, **request: object) -> dict[str, object]:
            return await _read(local_reader, **request)

    @asynccontextmanager
    async def connection() -> AsyncIterator[LocalConnection]:
        yield LocalConnection()

    monkeypatch.setattr("mcp_telegram.tools.reading.daemon_connection", connection)
    result = await list_messages(ListMessages(exact_dialog_id=42, message_state="sent", anchor_message_id=7))
    payload = result.structured_content
    assert payload is not None
    if anchor_present:
        assert not result.is_error
        assert not result.content
        assert payload["count"] == 1
        schema = normalize_temporal_output_schema(LIST_MESSAGES_OUTPUT_SCHEMA)
        assert schema is not None
        validate(instance=payload, schema=schema)
    else:
        assert result.is_error
        assert payload["error"] == "local_context_unavailable"
        assert payload["context_availability"] == "anchor_missing"
        assert payload["required_action"]
        assert any("Action:" in str(getattr(item, "text", "")) for item in result.content)
