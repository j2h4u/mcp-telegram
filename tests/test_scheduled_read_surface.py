from __future__ import annotations

import sqlite3
import time
from collections.abc import Mapping
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from mcp_telegram.pagination import decode_navigation_token, encode_history_navigation, encode_search_navigation
from mcp_telegram.reading import ReadingService
from mcp_telegram.reading.scheduled_projection import scheduled_row_to_wire
from mcp_telegram.tools.reading import (
    SearchMessages,
    _list_messages_structured_messages,
    _message_lifecycle_fields,
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
LIFECYCLE_FIELDS = {
    "message_state",
    "visibility",
    "unpublished",
    "published",
    "unseen",
    "scheduled_at",
    "published_at",
    "inclusion_basis",
}


def _create_draft_projection(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE draft_current (
            account_id INTEGER, dialog_id INTEGER, top_message_id INTEGER, subdialog_peer_id INTEGER,
            state TEXT, text TEXT, entities_json TEXT, reply_to_json TEXT, media_json TEXT,
            suggested_post_json TEXT, rich_message_json TEXT, effect_id INTEGER, no_webpage INTEGER,
            invert_media INTEGER, composition_complete INTEGER, source_kind TEXT, source_observed_at INTEGER,
            observation_started_at INTEGER, observation_completed_at INTEGER, projection_revision INTEGER,
            normalization_version INTEGER
        );
        CREATE TABLE draft_sync_state (
            account_id INTEGER PRIMARY KEY, status TEXT, coverage_status TEXT,
            observation_started_at INTEGER, observation_completed_at INTEGER, reason TEXT
        );
        """
    )


@pytest.mark.asyncio
async def test_list_messages_draft_is_local_and_has_no_sent_identity() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _create_draft_projection(conn)
    conn.execute(
        "INSERT INTO draft_current VALUES (7, 1, 0, 0, 'present', ?, '[]', NULL, NULL, NULL, NULL, NULL, 0, 0, 1, 'realtime_present', 100, 100, 101, 2, 1)",
        ("a composition longer than the obsolete eighty character dialog preview limit, without truncation",),
    )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 100, 101, NULL)")
    conn.commit()

    result = await server._list_messages({"dialog_id": 1, "message_state": "draft"})

    assert result["ok"] is True
    row = result["data"]["messages"][0]
    assert row["message_state"] == "draft"
    assert "message_id" not in row and "sent_at" not in row
    assert row["text"].startswith("a composition longer")
    assert result["data"]["draft_coverage"]["freshness"] == "current"


@pytest.mark.asyncio
@pytest.mark.parametrize("message_state", ["draft", "all"])
async def test_draft_response_budget_keeps_complete_rows_reachable(message_state: str) -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _create_draft_projection(conn)
    for topic_id in range(1, 6):
        conn.execute(
            "INSERT INTO draft_current VALUES (7, 1, ?, 0, 'present', ?, '[]', NULL, NULL, NULL, NULL, NULL, "
            "NULL, NULL, 1, 'realtime_present', 100, 100, 101, ?, 1)",
            (topic_id, "x" * 65_536, topic_id),
        )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 100, 101, NULL)")
    conn.commit()

    assert server._get_reading_service()._deps.draft_response_budget_bytes == 256 * 1024

    first = await server._list_messages({"dialog_id": 1, "message_state": message_state, "limit": 5})

    assert first["data"]["truncation"] == {
        "is_truncated": True,
        "shown_count": 3,
        "hidden_count": 2,
        "reason": "response_budget",
    }
    assert first["data"]["next_navigation"] is not None
    assert all(len(row["text"]) == 65_536 for row in first["data"]["messages"])

    second = await server._list_messages(
        {
            "dialog_id": 1,
            "message_state": message_state,
            "limit": 5,
            "navigation": first["data"]["next_navigation"],
        }
    )

    assert second["data"]["truncation"]["is_truncated"] is False
    assert second["data"]["next_navigation"] is None
    assert len(first["data"]["messages"] + second["data"]["messages"]) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("message_state", ["draft", "all"])
async def test_natural_draft_dialog_resolution_never_falls_through_to_telegram(message_state: str) -> None:
    get_entity = AsyncMock(side_effect=AssertionError("draft selector must remain local"))
    client = MagicMock()
    client.get_entity = get_entity
    server = make_server(client=client)

    result = await server._list_messages({"dialog": "@not_cached", "message_state": message_state})

    assert result["ok"] is False
    assert result["error"] in {"dialog_directory_incomplete", "dialog_not_found", "stale_local_directory"}
    assert result["required_action"] == (
        "Use an exact dialog id, or refresh the local dialog directory before retrying this username."
    )
    get_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_draft_cursor_rejects_changed_projection() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _create_draft_projection(conn)
    for topic_id, revision in ((1, 1), (2, 2)):
        conn.execute(
            "INSERT INTO draft_current VALUES (7, 1, ?, 0, 'present', 'draft', '[]', NULL, NULL, NULL, NULL, NULL, 0, 0, 1, 'realtime_present', 100, 100, 101, ?, 1)",
            (topic_id, revision),
        )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 100, 101, NULL)")
    conn.commit()
    first = await server._list_messages({"dialog_id": 1, "message_state": "draft", "limit": 1})
    conn.execute("UPDATE draft_current SET projection_revision = 3 WHERE top_message_id = 2")
    conn.commit()

    second = await server._list_messages(
        {"dialog_id": 1, "message_state": "draft", "limit": 1, "navigation": first["data"]["next_navigation"]}
    )

    assert second["error"] == "draft_projection_changed"


@pytest.mark.parametrize(
    ("row", "sent_at", "expected"),
    [
        (
            {"inclusion_basis": []},
            100,
            {
                "message_state": "sent",
                "visibility": "chat_visible",
                "unpublished": False,
                "published": True,
                "unseen": False,
                "scheduled_at": None,
                "published_at": 100,
                "inclusion_basis": [],
            },
        ),
        (
            {
                "message_state": "unknown",
                "scheduled_at": 300,
                "published_at": 200,
                "inclusion_basis": ["direct_message"],
            },
            100,
            {
                "message_state": "sent",
                "visibility": "chat_visible",
                "unpublished": False,
                "published": True,
                "unseen": False,
                "scheduled_at": None,
                "published_at": 200,
                "inclusion_basis": ["direct_message"],
            },
        ),
        (
            {"message_state": "scheduled", "scheduled_at": 300, "published_at": 200},
            100,
            {
                "message_state": "scheduled",
                "visibility": "author_only",
                "unpublished": True,
                "published": False,
                "unseen": True,
                "scheduled_at": 300,
                "published_at": 200,
                "inclusion_basis": [],
            },
        ),
        (
            {"message_state": "scheduled", "scheduled_at": 300, "inclusion_basis": ["direct_message"]},
            100,
            {
                "message_state": "scheduled",
                "visibility": "author_only",
                "unpublished": True,
                "published": False,
                "unseen": True,
                "scheduled_at": 300,
                "published_at": None,
                "inclusion_basis": ["direct_message"],
            },
        ),
    ],
    ids=["default-sent", "unknown-sent-explicit-publication", "scheduled-explicit-publication", "scheduled"],
)
def test_message_lifecycle_fields_contract(row: dict[str, object], sent_at: int, expected: dict[str, object]) -> None:
    lifecycle = _message_lifecycle_fields(row, sent_at=sent_at)

    assert set(lifecycle) == LIFECYCLE_FIELDS
    assert lifecycle == expected


def test_message_lifecycle_fields_does_not_alias_mutable_inclusion_basis() -> None:
    inclusion_basis = ["direct_message"]
    lifecycle = _message_lifecycle_fields({"inclusion_basis": inclusion_basis}, sent_at=100)

    projected_basis = lifecycle["inclusion_basis"]
    assert isinstance(projected_basis, list)
    projected_basis.append("own_only_dialog")

    assert inclusion_basis == ["direct_message"]


@pytest.mark.parametrize(
    ("text", "media_kind", "media_payload", "content_kind", "expected_text", "expected_media"),
    [
        ("plain text", None, None, "message_text", "plain text", None),
        ("caption", "photo", "{}", "message_text", "caption", "[фото]"),
        (None, "photo", "{}", "media_description", None, "[фото]"),
        ("", None, None, "none", None, None),
    ],
    ids=["plain-text", "caption-and-media", "media-only", "empty"],
)
def test_scheduled_row_mapper_preserves_canonical_content_shape(  # noqa: PLR0913
    text: str | None,
    media_kind: str | None,
    media_payload: str | None,
    content_kind: str,
    expected_text: str | None,
    expected_media: str | None,
) -> None:
    item = scheduled_row_to_wire(
        {
            "message_id": 11,
            "sent_at": FUTURE_BASE + 200,
            "dialog_id": 1,
            "text": text,
            "media_kind": media_kind,
            "media_payload": media_payload,
        },
        inclusion_basis=(),
    )

    assert item["content_kind"] == content_kind
    assert item["text"] == expected_text
    assert item["media_description"] == expected_media
    assert item["scheduled_at"] == FUTURE_BASE + 200


def test_scheduled_caption_and_media_use_public_structured_delivery_shape() -> None:
    structured = _list_messages_structured_messages(
        [
            {
                "message_id": 11,
                "sent_at": FUTURE_BASE + 200,
                "dialog_id": 1,
                "text": "caption",
                "media_kind": "photo",
                "media_description": "[фото]",
                "content_kind": "message_text",
            }
        ],
        dialog_type="User",
    )[0]

    assert structured["content"] == {
        "text": "caption",
        "is_telegram_content": True,
        "content_kind": "message_text",
    }
    assert structured["media"] == {"type": "photo", "description": "[фото]"}


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
            media_kind TEXT CHECK (media_kind IN ('photo', 'video', 'audio', 'voice', 'document', 'animation', 'sticker', 'custom_emoji', 'poll', 'location', 'venue', 'contact', 'link_preview', 'game', 'invoice', 'dice', 'story', 'other')),
            media_payload TEXT CHECK (media_payload IS NULL OR (json_valid(media_payload) AND json_type(media_payload) = 'object')),
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
    _create_own_only_table(conn)


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
    conn.execute(
        "INSERT OR IGNORE INTO own_only_dialogs(dialog_id, inclusion_basis, updated_at) VALUES (?, ?, ?)",
        (dialog_id, '["direct_message"]', 1700000000),
    )
    conn.commit()


def _create_own_only_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS own_only_dialogs (dialog_id INTEGER PRIMARY KEY, inclusion_basis TEXT NOT NULL, updated_at INTEGER NOT NULL)"
    )
    conn.commit()


@pytest.mark.asyncio
async def test_list_messages_scheduled_is_pending_only_and_local() -> None:
    server = make_server()
    conn = server._conn
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 11, FUTURE_BASE + 200, "later")
    _insert_scheduled(conn, 10, FUTURE_BASE + 100, "sooner")
    conn.execute(
        "UPDATE scheduled_messages SET text = NULL, media_kind = 'photo', media_payload = '{}' WHERE message_id = 10"
    )
    conn.commit()
    _insert_scheduled(conn, 12, FUTURE_BASE + 300, "cancelled", state="cancelled")

    result = await server._list_messages({"dialog_id": 1, "message_state": "scheduled", "direction": "oldest"})

    assert result["ok"] is True
    rows = result["data"]["messages"]
    assert [row["message_id"] for row in rows] == [10, 11]
    assert all(row["message_state"] == "scheduled" for row in rows)
    assert all(row["scheduled_at"] == row["sent_at"] for row in rows)
    assert rows[0]["content_kind"] == "media_description"
    assert rows[0]["text"] is None
    assert rows[0]["media_description"] == "[фото]"
    assert rows[1]["content_kind"] == "message_text"
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
    assert rows[0]["message_state"] == "sent"
    assert rows[1]["message_state"] == "scheduled"
    assert rows[1]["unpublished"] is True
    assert rows[1]["unseen"] is True
    assert result["data"]["source"] == "sync_db+scheduled_messages+draft_current"


@pytest.mark.asyncio
async def test_list_messages_defaults_to_all_and_sent_remains_explicit() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=1700000000, text="published")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, FUTURE_BASE + 100, "future")

    default_result = await server._list_messages({"dialog_id": 1, "direction": "oldest"})
    all_result = await server._list_messages({"dialog_id": 1, "message_state": "all", "direction": "oldest"})
    sent_result = await server._list_messages({"dialog_id": 1, "message_state": "sent", "direction": "oldest"})

    assert default_result["data"] == all_result["data"]
    assert [row["message_state"] for row in default_result["data"]["messages"]] == ["sent", "scheduled"]
    assert [row["text"] for row in sent_result["data"]["messages"]] == ["published"]


@pytest.mark.asyncio
async def test_default_all_unread_uses_only_the_published_unread_cursor() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    conn.execute("UPDATE synced_dialogs SET read_inbox_max_id = 100 WHERE dialog_id = 1")
    _insert_message(conn, 1, 99, sent_at=FUTURE_BASE - 300, text="read")
    _insert_message(conn, 1, 101, sent_at=FUTURE_BASE - 100, text="unread")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 102, FUTURE_BASE + 100, "scheduled")
    _create_draft_projection(conn)
    conn.execute(
        f"INSERT INTO draft_current VALUES (7, 1, 0, 0, 'present', 'draft', '[]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 'realtime_present', {FUTURE_BASE}, {FUTURE_BASE}, {FUTURE_BASE}, 1, 1)"
    )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', ?, ?, NULL)", (FUTURE_BASE, FUTURE_BASE))
    conn.commit()

    result = await server._list_messages({"dialog_id": 1, "unread": True, "direction": "oldest"})

    assert [row["text"] for row in result["data"]["messages"]] == ["unread"]
    assert [row["message_state"] for row in result["data"]["messages"]] == ["sent"]
    assert result["data"]["source"] == "sync_db"
    assert "draft_coverage" not in result["data"]


@pytest.mark.asyncio
async def test_all_unread_pagination_skips_mutable_local_projections(monkeypatch: pytest.MonkeyPatch) -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    conn.execute("UPDATE synced_dialogs SET read_inbox_max_id = 0 WHERE dialog_id = 1")
    _insert_message(conn, 1, 1, sent_at=100, text="first unread")
    _insert_message(conn, 1, 2, sent_at=200, text="second unread")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 3, FUTURE_BASE + 100, "scheduled")
    _create_draft_projection(conn)
    conn.execute(
        "INSERT INTO draft_current VALUES (7, 1, 0, 0, 'present', 'draft', '[]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 'realtime_present', 100, 100, 100, 1, 1)"
    )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 100, 100, NULL)")
    conn.commit()

    reading = server._get_reading_service()

    def _unexpected_projection(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("all+unread must not query a mutable local projection")

    monkeypatch.setattr(reading, "_list_scheduled_messages_from_db", _unexpected_projection)
    monkeypatch.setattr(reading, "_list_draft_messages_from_db", _unexpected_projection)

    first = await server._list_messages({"dialog_id": 1, "unread": True, "direction": "oldest", "limit": 1})
    token = first["data"]["next_navigation"]
    assert token is not None
    assert decode_navigation_token(token).unread is True

    conn.execute("UPDATE draft_current SET projection_revision = 2 WHERE dialog_id = 1")
    conn.commit()
    second = await server._list_messages(
        {"dialog_id": 1, "unread": True, "direction": "oldest", "limit": 1, "navigation": token}
    )

    assert [row["text"] for row in first["data"]["messages"] + second["data"]["messages"]] == [
        "first unread",
        "second unread",
    ]
    assert second["data"]["source"] == "sync_db"


@pytest.mark.asyncio
async def test_all_unread_cursor_rejects_continuation_without_unread() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    conn.execute("UPDATE synced_dialogs SET read_inbox_max_id = 0 WHERE dialog_id = 1")
    _insert_message(conn, 1, 1, sent_at=100, text="first unread")
    _insert_message(conn, 1, 2, sent_at=200, text="second unread")
    _create_draft_projection(conn)
    conn.execute(
        "INSERT INTO draft_current VALUES (7, 1, 0, 0, 'present', 'draft', '[]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 'realtime_present', 150, 150, 150, 1, 1)"
    )
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 150, 150, NULL)")
    conn.commit()

    first = await server._list_messages({"dialog_id": 1, "unread": True, "direction": "oldest", "limit": 1})
    token = first["data"]["next_navigation"]
    assert token is not None
    assert decode_navigation_token(token).draft_fingerprint is None

    second = await server._list_messages(
        {"dialog_id": 1, "message_state": "all", "direction": "oldest", "limit": 2, "navigation": token}
    )

    assert second["ok"] is False
    assert second["error"] == "invalid_navigation"
    assert "unread" in second["message"]


@pytest.mark.asyncio
async def test_all_cursor_rejects_continuation_with_unread() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    conn.execute("UPDATE synced_dialogs SET read_inbox_max_id = 0 WHERE dialog_id = 1")
    _insert_message(conn, 1, 1, sent_at=100, text="first")
    _insert_message(conn, 1, 2, sent_at=200, text="second")

    first = await server._list_messages({"dialog_id": 1, "direction": "oldest", "limit": 1})
    token = first["data"]["next_navigation"]
    assert token is not None
    assert decode_navigation_token(token).unread is False

    second = await server._list_messages(
        {"dialog_id": 1, "direction": "oldest", "limit": 1, "unread": True, "navigation": token}
    )

    assert second["ok"] is False
    assert second["error"] == "invalid_navigation"
    assert "unread" in second["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("message_state", [None, "sent"])
async def test_unread_requires_a_known_read_position(message_state: str | None) -> None:
    server = make_server()
    _insert_synced_dialog(server._conn, 1, status="synced")
    _insert_message(server._conn, 1, 1, sent_at=100, text="unclassified")
    request: dict[str, object] = {"dialog_id": 1, "unread": True}
    if message_state is not None:
        request["message_state"] = message_state

    result = await server._list_messages(request)

    assert result["ok"] is False
    assert result["error"] == "read_position_pending"
    assert result["required_action"] == "Retry shortly while the sync daemon reconciles read positions."


@pytest.mark.asyncio
@pytest.mark.parametrize("message_state", ["scheduled", "draft"])
async def test_non_sent_states_reject_unread(message_state: str) -> None:
    result = await make_server()._list_messages({"dialog_id": 1, "message_state": message_state, "unread": True})

    assert result["ok"] is False
    assert result["error"] == "unread_state_unsupported"
    assert 'message_state="sent" or message_state="all"' in result["message"]


@pytest.mark.asyncio
async def test_omitted_state_rejects_context_with_actionable_sent_retry() -> None:
    result = await make_server()._list_messages({"dialog_id": 1, "context_message_id": 1})

    assert result["ok"] is False
    assert result["error"] == "all_context_unsupported"
    assert 'retry with message_state="sent"' in result["message"]


@pytest.mark.asyncio
async def test_default_all_empty_exact_topic_has_truthful_local_attribution() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=100, text="another topic", forum_topic_id=8)

    result = await server._list_messages({"dialog_id": 1, "topic_id": 7})

    assert result["ok"] is True
    assert result["data"]["messages"] == []
    assert result["data"]["selection_state"] == "unknown"
    assert result["data"]["topic_attribution"]["state"] == "unknown"


@pytest.mark.asyncio
async def test_default_all_marks_not_synced_empty_history_as_local_only() -> None:
    iter_messages = MagicMock(side_effect=AssertionError("all reads must not call Telegram"))
    client = MagicMock()
    client.iter_messages = iter_messages
    server = make_server(client=client)
    _insert_synced_dialog(server._conn, 1, status="not_synced")

    result = await server._list_messages({"dialog_id": 1})

    assert result["ok"] is True
    assert result["data"]["messages"] == []
    assert result["data"]["coverage"] == "local_only"
    assert result["data"]["dialog_access"] == "local_only"
    assert result["data"]["required_action"] == (
        "Mark the dialog for sync to make a complete local history available, or retry with "
        'message_state="sent" for an on-demand live page.'
    )
    iter_messages.assert_not_called()


@pytest.mark.asyncio
async def test_list_messages_all_uses_numeric_tie_break_for_sent_and_scheduled_rows() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    shared_timestamp = FUTURE_BASE + 100
    _insert_message(conn, 1, 10, sent_at=shared_timestamp, text="sent ten")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, shared_timestamp, "scheduled two")

    result = await server._list_messages({"dialog_id": 1, "message_state": "all", "direction": "oldest"})

    assert [row["message_id"] for row in result["data"]["messages"]] == [2, 10]


@pytest.mark.asyncio
async def test_list_messages_all_topic_cursor_preserves_topic_scope() -> None:
    server = make_server()
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=100, text="topic first", forum_topic_id=7)
    _insert_message(conn, 1, 2, sent_at=200, text="topic second", forum_topic_id=7)
    _insert_message(conn, 1, 3, sent_at=300, text="other topic", forum_topic_id=8)

    first = await server._list_messages(
        {"dialog_id": 1, "message_state": "all", "direction": "oldest", "limit": 1, "topic_id": 7}
    )
    token = first["data"]["next_navigation"]
    assert token is not None
    assert decode_navigation_token(token).topic_id == 7

    second = await server._list_messages(
        {
            "dialog_id": 1,
            "message_state": "all",
            "direction": "oldest",
            "limit": 1,
            "topic_id": 7,
            "navigation": token,
        }
    )

    assert [row["message_id"] for row in first["data"]["messages"] + second["data"]["messages"]] == [1, 2]


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
async def test_list_messages_all_paginates_drafts_without_hiding_later_sent_rows() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=FUTURE_BASE - 300, text="sent one")
    _insert_message(conn, 1, 3, sent_at=FUTURE_BASE - 100, text="sent three")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, FUTURE_BASE - 200, "scheduled two")
    _create_draft_projection(conn)
    conn.execute(
        f"INSERT INTO draft_current VALUES (7, 1, 0, 0, 'present', 'draft', '[]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 'realtime_present', {FUTURE_BASE - 150}, {FUTURE_BASE - 150}, {FUTURE_BASE - 150}, 1, 1)"
    )
    conn.execute(
        "INSERT INTO draft_sync_state VALUES (?, 'ready', 'complete', ?, ?, NULL)",
        (7, FUTURE_BASE - 150, FUTURE_BASE - 150),
    )
    conn.commit()

    first = await server._list_messages({"dialog_id": 1, "message_state": "all", "direction": "oldest", "limit": 2})
    assert [row["text"] for row in first["data"]["messages"]] == ["sent one", "scheduled two"]
    token = first["data"]["next_navigation"]
    assert token is not None
    navigation = decode_navigation_token(token)
    assert navigation.value == 1
    assert navigation.scheduled_message_id == 2
    assert navigation.draft_key is None
    assert navigation.draft_fingerprint is not None

    second = await server._list_messages(
        {"dialog_id": 1, "message_state": "all", "direction": "oldest", "limit": 2, "navigation": token}
    )
    assert [row["text"] for row in second["data"]["messages"]] == ["draft", "sent three"]
    assert second["data"]["next_navigation"] is None


@pytest.mark.asyncio
async def test_list_messages_all_invalidates_cursor_when_unseen_draft_changes() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _insert_synced_dialog(conn, 1, status="synced")
    _insert_message(conn, 1, 1, sent_at=FUTURE_BASE - 300, text="sent one")
    _insert_message(conn, 1, 3, sent_at=FUTURE_BASE - 100, text="sent three")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, FUTURE_BASE - 200, "scheduled two")
    _create_draft_projection(conn)
    conn.execute(
        f"INSERT INTO draft_current VALUES (7, 1, 0, 0, 'present', 'draft', '[]', NULL, NULL, NULL, NULL, NULL, NULL, NULL, 1, 'realtime_present', {FUTURE_BASE - 150}, {FUTURE_BASE - 150}, {FUTURE_BASE - 150}, 1, 1)"
    )
    conn.execute(
        "INSERT INTO draft_sync_state VALUES (?, 'ready', 'complete', ?, ?, NULL)",
        (7, FUTURE_BASE - 150, FUTURE_BASE - 150),
    )
    conn.commit()

    first = await server._list_messages({"dialog_id": 1, "message_state": "all", "direction": "oldest", "limit": 2})
    conn.execute("UPDATE draft_current SET projection_revision = 2 WHERE dialog_id = 1")
    conn.commit()

    second = await server._list_messages(
        {
            "dialog_id": 1,
            "message_state": "all",
            "direction": "oldest",
            "limit": 2,
            "navigation": first["data"]["next_navigation"],
        }
    )
    assert second["error"] == "draft_projection_changed"


@pytest.mark.asyncio
async def test_draft_sender_filter_reports_scope_mismatch_without_absence_claim() -> None:
    server = make_server()
    server.self_id = 7
    conn = server._conn
    _create_draft_projection(conn)
    conn.execute("INSERT INTO draft_sync_state VALUES (7, 'ready', 'complete', 100, 100, NULL)")
    conn.commit()

    result = await server._list_messages({"dialog_id": 1, "message_state": "draft", "sender_id": 99})

    coverage = result["data"]["draft_coverage"]
    assert coverage["presence"] == "unknown"
    assert coverage["absence_basis"] is None
    assert coverage["continuity_reason"] == "sender_filter_excludes_author_only_drafts"


@pytest.mark.asyncio
async def test_own_only_cache_fails_closed_for_scheduled_projection() -> None:
    server = make_server()
    conn = server._conn
    dialog_id = 42
    _insert_synced_dialog(conn, dialog_id, status="synced")
    _insert_message(conn, dialog_id, 1, sent_at=FUTURE_BASE - 300, text="sent")
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 2, FUTURE_BASE + 100, "scheduled", dialog_id=dialog_id)
    conn.execute("DELETE FROM own_only_dialogs WHERE dialog_id = ?", (dialog_id,))
    conn.commit()
    scheduled_without_basis = await server._list_messages({"dialog_id": dialog_id, "message_state": "scheduled"})
    all_without_basis = await server._list_messages(
        {"dialog_id": dialog_id, "message_state": "all", "direction": "oldest"}
    )
    assert scheduled_without_basis["data"]["messages"] == []
    assert [row["message_id"] for row in all_without_basis["data"]["messages"]] == [1]

    conn.execute(
        "INSERT INTO own_only_dialogs(dialog_id, inclusion_basis, updated_at) VALUES (?, ?, ?)",
        (dialog_id, '["direct_message"]', 1700000000),
    )
    conn.commit()
    scheduled_with_basis = await server._list_messages({"dialog_id": dialog_id, "message_state": "scheduled"})
    all_with_basis = await server._list_messages(
        {"dialog_id": dialog_id, "message_state": "all", "direction": "oldest"}
    )
    assert [row["message_id"] for row in scheduled_with_basis["data"]["messages"]] == [2]
    assert [row["message_id"] for row in all_with_basis["data"]["messages"]] == [1, 2]


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


@pytest.mark.asyncio
async def test_list_and_search_scheduled_rows_have_identical_wire_shape() -> None:
    conn = _make_db_with_dialogs()
    server = make_server(conn)
    _create_scheduled_table(conn)
    _insert_scheduled(conn, 11, FUTURE_BASE + 200, "needle in future")

    listed = await server._list_messages({"dialog_id": 1, "message_state": "scheduled", "direction": "oldest"})
    searched = await server._search_messages(
        {"dialog_id": 1, "query": "needle", "message_state": "scheduled", "limit": 20}
    )

    assert listed["data"]["messages"] == searched["data"]["messages"]


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
    result = ReadingService._decode_history_navigation(navigation, 123, "newest", "sent", topic_id=8)

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


@pytest.mark.parametrize(
    ("message_state", "scheduled_at"),
    [("sent", None), ("scheduled", FUTURE_BASE + 200)],
)
def test_tool_read_and_search_lifecycle_matches_shared_contract(message_state: str, scheduled_at: int | None) -> None:
    row = {
        "message_id": 11,
        "sent_at": FUTURE_BASE + 200,
        "dialog_id": 1,
        "text": "future",
        "sender_id": 99,
        "sender_first_name": "Me",
        "message_state": message_state,
        "scheduled_at": scheduled_at,
        "published_at": None,
        "inclusion_basis": ["direct_message"],
    }

    listed = _list_messages_structured_messages([row], dialog_type="User")
    searched = _search_result_structured_rows([row], "future")
    expected = _message_lifecycle_fields(row, sent_at=FUTURE_BASE + 200)

    for item in [listed[0], searched[0]]:
        assert {field: item[field] for field in LIFECYCLE_FIELDS} == expected


def test_tool_read_and_search_route_lifecycle_through_shared_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    row = {
        "message_id": 11,
        "sent_at": FUTURE_BASE + 200,
        "dialog_id": 1,
        "text": "future",
        "sender_id": 99,
        "sender_first_name": "Me",
    }
    projected = {
        "message_state": "sent",
        "visibility": "chat_visible",
        "unpublished": False,
        "published": True,
        "unseen": False,
        "scheduled_at": None,
        "published_at": FUTURE_BASE + 200,
        "inclusion_basis": ["shared-helper"],
    }
    calls: list[tuple[Mapping[str, object], int | None]] = []

    def project(candidate: Mapping[str, object], *, sent_at: int | None) -> dict[str, object]:
        calls.append((candidate, sent_at))
        return projected.copy()

    monkeypatch.setattr("mcp_telegram.tools.reading._message_lifecycle_fields", project)

    listed = _list_messages_structured_messages([row], dialog_type="User")[0]
    searched = _search_result_structured_rows([row], "future")[0]

    assert {field: listed[field] for field in LIFECYCLE_FIELDS} == projected
    assert {field: searched[field] for field in LIFECYCLE_FIELDS} == projected
    assert calls == [(row, FUTURE_BASE + 200), (row, FUTURE_BASE + 200)]
