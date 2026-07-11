from __future__ import annotations

import sqlite3
import time
from typing import Literal

import pytest

from mcp_telegram.daemon_reading import DaemonReadingService
from mcp_telegram.pagination import decode_navigation_token, encode_history_navigation, encode_search_navigation
from mcp_telegram.tools.reading import (
    SearchMessages,
    _list_messages_structured_messages,
    _search_messages_request_context,
    _search_result_structured_rows,
)
from tests.test_daemon_api import (
    _insert_message,
    _insert_synced_dialog,
    _make_db_with_dialogs,
    _seed_dialog_row,
    make_server,
)

FUTURE_BASE = int(time.time()) + 86_400


def _create_scheduled_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE scheduled_messages (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            scheduled_at INTEGER,
            text TEXT,
            sender_id INTEGER,
            sender_first_name TEXT,
            media_description TEXT,
            reply_to_msg_id INTEGER,
            forum_topic_id INTEGER,
            edit_date INTEGER,
            grouped_id INTEGER,
            reply_to_peer_id INTEGER,
            out INTEGER NOT NULL DEFAULT 1,
            is_service INTEGER NOT NULL DEFAULT 0,
            post_author TEXT,
            schedule_repeat_period INTEGER,
            message_state TEXT NOT NULL DEFAULT 'scheduled',
            visibility TEXT NOT NULL DEFAULT 'author_only',
            unpublished INTEGER NOT NULL DEFAULT 1,
            unseen INTEGER NOT NULL DEFAULT 1,
            publication_hint_message_id INTEGER,
            publication_verified_at INTEGER,
            published_at INTEGER,
            deleted_at INTEGER,
            first_seen_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        "CREATE VIRTUAL TABLE scheduled_messages_fts "
        "USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, stemmed_text, tokenize='unicode61')"
    )


def _insert_scheduled(  # noqa: PLR0913
    conn: sqlite3.Connection,
    message_id: int,
    at: int,
    text: str,
    state: str = "scheduled",
    dialog_id: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO scheduled_messages
        (dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
         message_state, visibility, unpublished, unseen, first_seen_at, updated_at)
        VALUES (?, ?, ?, ?, 99, 'Me', ?, 'author_only', 1, 1, 1700000000, 1700000000)
        """,
        (dialog_id, message_id, at, text, state),
    )
    conn.commit()
    conn.execute(
        "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)",
        (dialog_id, message_id, text),
    )
    conn.commit()


def _create_own_only_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE own_only_dialogs (dialog_id INTEGER PRIMARY KEY, inclusion_basis TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.commit()


@pytest.mark.asyncio
async def test_list_messages_scheduled_is_pending_only_and_local() -> None:
    server = make_server()
    conn = server._conn
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 11, FUTURE_BASE + 200, "later")
    _insert_scheduled(conn, 10, FUTURE_BASE + 100, "sooner")
    _insert_scheduled(conn, 12, FUTURE_BASE + 300, "cancelled", state="cancelled")

    result = await server._list_messages({"dialog_id": 1, "message_state": "scheduled", "direction": "oldest"})

    assert result["ok"] is True
    rows = result["data"]["messages"]
    assert [row["message_id"] for row in rows] == [10, 11]
    assert all(row["message_state"] == "scheduled" for row in rows)
    assert all(row["scheduled_at"] == row["sent_at"] for row in rows)
    assert result["data"]["source"] == "scheduled_messages"


@pytest.mark.asyncio
async def test_list_messages_all_uses_one_unified_envelope() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=1700000000, text="published")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, FUTURE_BASE + 100, "future")

    result = await server._list_messages({"dialog_id": 1, "message_state": "all", "direction": "oldest"})

    rows = result["data"]["messages"]
    assert [row["text"] for row in rows] == ["published", "future"]
    assert "message_state" not in rows[0]
    assert rows[1]["message_state"] == "scheduled"
    assert rows[1]["unpublished"] is True
    assert rows[1]["unseen"] is True


@pytest.mark.asyncio
async def test_list_messages_all_paginates_across_sent_and_scheduled_rows() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=FUTURE_BASE - 300, text="sent one")
    _insert_message(conn, 1, 3, sent_at=FUTURE_BASE - 100, text="sent three")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, FUTURE_BASE - 200, "scheduled two")
    _insert_scheduled(conn, 4, FUTURE_BASE + 100, "scheduled four")

    first = await server._list_messages({"dialog_id": 1, "message_state": "all", "direction": "oldest", "limit": 2})
    assert [row["text"] for row in first["data"]["messages"]] == ["sent one", "scheduled two"]
    assert first["data"]["next_navigation"] is not None

    second = await server._list_messages(
        {
            "dialog_id": 1,
            "message_state": "all",
            "direction": "oldest",
            "limit": 2,
            "navigation": first["data"]["next_navigation"],
        }
    )
    assert [row["text"] for row in second["data"]["messages"]] == ["sent three", "scheduled four"]


@pytest.mark.asyncio
async def test_scheduled_reads_hide_non_future_rows() -> None:
    server = make_server()
    conn = server._conn
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 10, FUTURE_BASE + 100, "future")
    _insert_scheduled(conn, 11, int(time.time()) - 100, "expired")

    result = await server._list_messages({"dialog_id": 1, "message_state": "scheduled"})
    assert [row["message_id"] for row in result["data"]["messages"]] == [10]


@pytest.mark.asyncio
async def test_search_messages_scheduled_is_local_and_explicit() -> None:
    conn = _make_db_with_dialogs()
    server = make_server(conn)
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 11, FUTURE_BASE + 200, "needle in future")

    result = await server._search_messages(
        {"dialog_id": 1, "query": "needle", "message_state": "scheduled", "limit": 20}
    )

    assert result["ok"] is True
    rows = result["data"]["messages"]
    assert len(rows) == 1
    assert rows[0]["message_state"] == "scheduled"
    assert rows[0]["scheduled_at"] == FUTURE_BASE + 200
    assert rows[0]["unpublished"] is True
    assert rows[0]["unseen"] is True


@pytest.mark.parametrize(
    ("token_dialog", "token_query", "token_state", "dialog", "query", "state"),
    [
        (123, "other", "sent", "123", "needle", "sent"),
        (123, "needle", "scheduled", "123", "needle", "sent"),
        (999, "needle", "sent", "123", "needle", "sent"),
    ],
    ids=["query", "message-state", "dialog-scope"],
)
def test_search_navigation_rejects_mismatched_context(  # noqa: PLR0913
    token_dialog: int,
    token_query: str,
    token_state: str,
    dialog: str,
    query: str,
    state: Literal["sent", "scheduled", "all"],
) -> None:
    """Search cursors are bound to query, lifecycle, and dialog scope."""
    token = encode_search_navigation(20, token_dialog, token_query, token_state)
    result = _search_messages_request_context(
        SearchMessages(dialog=dialog, query=query, message_state=state, navigation=token)
    )

    assert getattr(result, "is_error", False) is True


@pytest.mark.parametrize(
    ("token", "dialog", "query", "state", "expected_offset"),
    [
        ("not-a-valid-token", None, "needle", "sent", None),
        (encode_history_navigation(20, dialog_id=123, message_state="sent"), "123", "needle", "sent", None),
        (encode_search_navigation(20, 0, "needle", "sent"), None, "needle", "sent", 20),
    ],
    ids=["malformed", "wrong-kind", "valid-global"],
)
def test_search_navigation_context_handles_decode_and_success_paths(
    token: str,
    dialog: str | None,
    query: str,
    state: Literal["sent", "scheduled", "all"],
    expected_offset: int | None,
) -> None:
    result = _search_messages_request_context(
        SearchMessages(dialog=dialog, query=query, message_state=state, navigation=token)
    )

    if expected_offset is None:
        assert getattr(result, "is_error", False) is True
    else:
        assert getattr(result, "offset", None) == expected_offset


def test_history_navigation_rejects_mismatched_topic_scope() -> None:
    from mcp_telegram.pagination import encode_history_navigation

    navigation = encode_history_navigation(42, dialog_id=123, topic_id=7, message_state="sent")
    result = DaemonReadingService._decode_history_navigation(navigation, 123, "newest", "sent", topic_id=8)

    assert isinstance(result, dict)
    assert result["error"] == "invalid_navigation"
    assert "topic" in result["message"]


@pytest.mark.asyncio
async def test_search_navigation_binds_name_resolved_dialog_scope() -> None:
    from mcp_telegram.pagination import encode_search_navigation

    conn = _make_db_with_dialogs()
    _seed_dialog_row(conn, 1, name="Named Dialog")
    server = make_server(conn)
    token = encode_search_navigation(20, 999, "needle", "sent")

    result = await server._search_messages(
        {"dialog": "Named Dialog", "query": "needle", "message_state": "sent", "navigation": token}
    )

    assert result["ok"] is False
    assert result["error"] == "invalid_navigation"
    assert "dialog" in result["message"]


@pytest.mark.asyncio
async def test_search_scheduled_scoped_navigation_roundtrip() -> None:
    conn = _make_db_with_dialogs()
    server = make_server(conn)
    _create_scheduled_table(conn)
    for message_id, offset in enumerate((100, 200, 300), start=1):
        _insert_scheduled(conn, message_id, FUTURE_BASE + offset, f"needle {message_id}")

    first = await server._search_messages({"dialog_id": 1, "query": "needle", "message_state": "scheduled", "limit": 2})
    token = first["data"]["next_navigation"]
    nav = decode_navigation_token(token)
    assert nav.dialog_id == 1
    assert nav.query == "needle"
    assert nav.message_state == "scheduled"

    second = await server._search_messages(
        {
            "dialog_id": 1,
            "query": "needle",
            "message_state": "scheduled",
            "limit": 2,
            "offset": nav.value,
        }
    )
    assert [row["message_id"] for row in first["data"]["messages"]] == [1, 2]
    assert [row["message_id"] for row in second["data"]["messages"]] == [3]


@pytest.mark.asyncio
async def test_search_scheduled_global_navigation_roundtrip() -> None:
    conn = _make_db_with_dialogs()
    server = make_server(conn)
    _create_scheduled_table(conn)
    for message_id, (dialog_id, offset) in enumerate(((1, 100), (2, 200), (1, 300)), start=1):
        _insert_scheduled(conn, message_id, FUTURE_BASE + offset, f"needle {message_id}", dialog_id=dialog_id)

    first = await server._search_messages({"query": "needle", "message_state": "scheduled", "limit": 2})
    token = first["data"]["next_navigation"]
    nav = decode_navigation_token(token)
    assert nav.dialog_id == 0
    assert nav.message_state == "scheduled"

    second = await server._search_messages(
        {"query": "needle", "message_state": "scheduled", "limit": 2, "offset": nav.value}
    )
    assert [row["message_id"] for row in first["data"]["messages"]] == [1, 2]
    assert [row["message_id"] for row in second["data"]["messages"]] == [3]


@pytest.mark.asyncio
@pytest.mark.parametrize("global_mode", [False, True], ids=["scoped", "global"])
async def test_search_all_navigation_roundtrip_preserves_chronological_order(global_mode: bool) -> None:
    conn = _make_db_with_dialogs(with_fts=True)
    server = make_server(conn)
    _create_scheduled_table(conn)
    _insert_message(conn, 1, 1, sent_at=FUTURE_BASE + 100, text="needle sent 1")
    conn.execute("INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, 1, 'needle')")
    _insert_message(conn, 1, 3, sent_at=FUTURE_BASE + 300, text="needle sent 3")
    conn.execute("INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (1, 3, 'needle')")
    _insert_scheduled(conn, 2, FUTURE_BASE + 200, "needle scheduled 2")
    conn.commit()

    request: dict[str, object] = {"query": "needle", "message_state": "all", "limit": 2}
    if not global_mode:
        request["dialog_id"] = 1
    first = await server._search_messages(request)
    token = first["data"]["next_navigation"]
    nav = decode_navigation_token(token)
    assert nav.dialog_id == (0 if global_mode else 1)
    assert nav.query == "needle"
    assert nav.message_state == "all"

    second = await server._search_messages({**request, "offset": nav.value})
    assert [row["message_id"] for row in first["data"]["messages"]] == [1, 2]
    assert [row["message_id"] for row in second["data"]["messages"]] == [3]


@pytest.mark.asyncio
async def test_list_dialogs_scheduled_summary_and_filter() -> None:
    conn = _make_db_with_dialogs()
    server = make_server(conn)
    _seed_dialog_row(conn, 1, name="Future chat")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 11, FUTURE_BASE + 200, "future")

    all_dialogs = await server._list_dialogs({})
    scheduled_dialogs = await server._list_dialogs({"message_state": "scheduled"})

    row = all_dialogs["data"]["dialogs"][0]
    assert row["scheduled_count"] == 1
    assert row["next_scheduled_at"] == FUTURE_BASE + 200
    assert [d["id"] for d in scheduled_dialogs["data"]["dialogs"]] == [1]


@pytest.mark.asyncio
async def test_scheduled_reads_filter_own_scope_and_expose_basis() -> None:
    conn = _make_db_with_dialogs()
    server = make_server(conn)
    _seed_dialog_row(conn, 1, name="Own chat")
    _seed_dialog_row(conn, 2, name="Other chat")
    _create_scheduled_table(conn)
    _create_own_only_table(conn)
    conn.execute("INSERT INTO own_only_dialogs VALUES (1, '[\"direct_message\"]', 1700000000)")
    _insert_scheduled(conn, 11, FUTURE_BASE + 200, "own future")
    conn.execute(
        "INSERT INTO scheduled_messages "
        "(dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name, first_seen_at, updated_at) "
        "VALUES (2, 12, ?, 'other future', 99, 'Me', 1700000000, 1700000000)",
        (FUTURE_BASE + 300,),
    )
    conn.execute("INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (2, 12, 'future')")
    conn.commit()

    result = await server._search_messages({"query": "future", "message_state": "scheduled"})

    assert [row["dialog_id"] for row in result["data"]["messages"]] == [1]
    assert result["data"]["scope"] == "own_only"
    assert result["data"]["messages"][0]["inclusion_basis"] == ["direct_message"]
    dialogs = await server._list_dialogs({"scope": "own_only", "message_state": "scheduled"})
    assert [row["id"] for row in dialogs["data"]["dialogs"]] == [1]
    assert dialogs["data"]["dialogs"][0]["inclusion_basis"] == ["direct_message"]


def test_tool_read_and_search_envelopes_mark_scheduled_visibility() -> None:
    row = {
        "message_id": 11,
        "sent_at": FUTURE_BASE + 200,
        "dialog_id": 1,
        "text": "future",
        "sender_id": 99,
        "sender_first_name": "Me",
        "message_state": "scheduled",
        "scheduled_at": FUTURE_BASE + 200,
        "published_at": None,
    }

    listed = _list_messages_structured_messages([row], dialog_type="User")
    searched = _search_result_structured_rows([row], "future")

    for item in [listed[0], searched[0]]:
        assert item["message_state"] == "scheduled"
        assert item["visibility"] == "author_only"
        assert item["published"] is False
        assert item["unpublished"] is True
        assert item["unseen"] is True
        assert item["scheduled_at"] == FUTURE_BASE + 200
        assert item["published_at"] is None
