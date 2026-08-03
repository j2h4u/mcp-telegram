# pyright: reportAny=false

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerUser

from mcp_telegram.event_handlers import EventHandlerManager, _NewMessageEvent
from mcp_telegram.own_only import OwnOnlyContext
from mcp_telegram.scheduled_messages import (
    ScheduledMessageReconciler,
    _unix_timestamp,
    mark_scheduled_messages_removed,
    scheduled_dialog_id,
    upsert_scheduled_message,
    verify_scheduled_publication,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema


def _message(message_id: int, text: str = "draft", *, scheduled_at: int = 1_900_000_000) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        date=datetime.fromtimestamp(scheduled_at, tz=UTC),
        message=text,
        sender_id=7,
        sender=SimpleNamespace(first_name="Me"),
        media=None,
        reply_to=None,
        replies=None,
        reactions=None,
        edit_date=None,
        message_thread_id=None,
        is_topic_message=False,
        schedule_repeat_period=None,
        peer_id=PeerUser(user_id=42),
    )


class _ScheduledSnapshotClient:
    def __init__(
        self,
        snapshots: dict[int, list[object]] | None = None,
        *,
        call_error: Exception | None = None,
        entities: dict[int, object] | None = None,
    ) -> None:
        self.snapshots = snapshots or {}
        self.call_error = call_error
        self.entities = entities or {}
        self.requests: list[tuple[object, dict[str, object]]] = []
        self.input_entity_calls: list[int] = []
        self.entity_calls: list[int] = []
        self.flood_sleep_threshold = 60
        self.thresholds_during_call: list[int] = []

    async def get_input_entity(self, _dialog_id: int) -> object:
        self.input_entity_calls.append(_dialog_id)
        return _dialog_id

    async def get_entity(self, _dialog_id: int) -> object:
        self.entity_calls.append(_dialog_id)
        return self.entities.get(_dialog_id)

    async def __call__(self, _request: object, **_kwargs: object) -> object:
        self.requests.append((_request, _kwargs))
        self.thresholds_during_call.append(self.flood_sleep_threshold)
        if self.call_error is not None:
            raise self.call_error
        dialog_id = int(cast(int, cast(SimpleNamespace, _request).peer))
        return SimpleNamespace(messages=self.snapshots.get(dialog_id, []), users=[], chats=[])


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    yield connection
    connection.close()


def test_scheduled_schema_is_separate_and_explicit(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_messages)")}
    assert columns >= {
        "message_id",
        "message_state",
        "visibility",
        "unpublished",
        "unseen",
        "scheduled_at",
        "published_message_id",
        "published_at",
    }


def test_upsert_reschedule_updates_same_queue_identity_without_sent_row(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(11, "first", scheduled_at=1_900_000_001), now=100)
    upsert_scheduled_message(conn, 42, _message(11, "rescheduled", scheduled_at=1_900_000_101), now=101)
    conn.commit()

    row = conn.execute(
        "SELECT message_id, scheduled_at, text, message_state, visibility, unpublished, unseen FROM scheduled_messages"
    ).fetchone()
    assert row == (11, 1_900_000_101, "rescheduled", "scheduled", "author_only", 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT stemmed_text FROM scheduled_messages_fts").fetchone() == ("rescheduled",)


def test_upsert_drops_non_future_queue_rows(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(11, scheduled_at=100), now=101)
    assert conn.execute("SELECT COUNT(*) FROM scheduled_messages").fetchone() == (0,)


def test_removal_retains_cancel_and_unverified_publication_evidence(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    upsert_scheduled_message(conn, 42, _message(12), now=100)
    mark_scheduled_messages_removed(conn, 42, [11, 12], [901], now=200)

    rows = conn.execute(
        "SELECT message_id, message_state, visibility, unpublished, publication_hint_message_id "
        "FROM scheduled_messages ORDER BY message_id"
    ).fetchall()
    assert rows == [
        (11, "unknown_missing", "author_only", 1, 901),
        (12, "cancelled", "author_only", 1, None),
    ]
    assert verify_scheduled_publication(conn, 42, 901, now=201) == 1
    assert conn.execute(
        "SELECT message_state, visibility, unpublished, unseen, published_message_id, published_at "
        "FROM scheduled_messages WHERE message_id=11"
    ).fetchone() == ("published", "chat_visible", 0, 0, 901, 201)
    assert conn.execute("SELECT COUNT(*) FROM scheduled_messages_fts").fetchone() == (0,)


@pytest.mark.asyncio
async def test_reconciliation_snapshot_marks_disappearance_nonvisible(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (42, 'synced')")
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    client = _ScheduledSnapshotClient({42: []})
    worker = ScheduledMessageReconciler(client, conn, asyncio.Event(), 0)

    assert await worker.run_once() == 1
    assert conn.execute(
        "SELECT message_state, unpublished, unseen FROM scheduled_messages WHERE message_id=11"
    ).fetchone() == ("unknown_missing", 1, 1)
    assert len(client.requests) == 1
    assert client.requests[0][1]["flood_sleep_threshold"] == 0
    assert client.thresholds_during_call == [0]
    assert client.flood_sleep_threshold == 60


@pytest.mark.asyncio
async def test_reconciliation_floodwait_records_retry_and_stops_account_pass(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        [(42,), (43,)],
    )
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    client = _ScheduledSnapshotClient(call_error=FloodWaitError(None, 30))
    worker = ScheduledMessageReconciler(client, conn, asyncio.Event(), 0)

    assert await worker.run_once() == 0
    retry_at, error = conn.execute(
        "SELECT next_retry_at, last_error FROM scheduled_sync_state WHERE key='account'"
    ).fetchone()
    assert retry_at is not None and retry_at >= 30
    assert error == "FloodWaitError"
    assert len(client.requests) == 1
    assert client.requests[0][1]["flood_sleep_threshold"] == 0
    assert client.thresholds_during_call == [0]
    assert client.flood_sleep_threshold == 60


@pytest.mark.asyncio
async def test_reconciliation_without_own_only_context_does_not_sweep_all_synced_dialogs(
    conn: sqlite3.Connection,
) -> None:
    conn.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        [(42,), (43,)],
    )
    conn.commit()
    client = _ScheduledSnapshotClient()
    worker = ScheduledMessageReconciler(client, conn, asyncio.Event(), 0)

    assert await worker.run_once() == 0
    assert client.requests == []
    assert client.input_entity_calls == []


@pytest.mark.asyncio
async def test_reconciliation_classifies_and_enrolls_own_only_candidates(conn: sqlite3.Connection) -> None:
    personal_id = -1000000009001
    admin_id = -1000000009002
    unrelated_id = -1000000009003
    discussion_id = -1000000008001
    conn.executemany(
        "INSERT INTO dialogs (dialog_id, type, hidden, linked_chat_id, linked_chat_resolved_at) VALUES (?, ?, 0, ?, ?)",
        [
            (7, "user", None, None),
            (personal_id, "channel", discussion_id, 100),
            (admin_id, "channel", None, None),
            (unrelated_id, "channel", None, None),
            (discussion_id, "forum", None, None),
        ],
    )
    conn.commit()
    conn.execute(
        "INSERT INTO own_only_dialogs (dialog_id, inclusion_basis, updated_at) VALUES (?, ?, ?)",
        (999, '["owned_channel"]', 100),
    )
    conn.commit()

    client = _ScheduledSnapshotClient(
        {personal_id: [_message(99, "personal scheduled")]},
        entities={
            admin_id: SimpleNamespace(creator=False, admin_rights=SimpleNamespace(post_messages=True)),
            unrelated_id: SimpleNamespace(creator=False, admin_rights=SimpleNamespace(post_messages=False)),
        },
    )
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        0,
        OwnOnlyContext(account_id=42, personal_channel_id=9001),
    )

    assert await worker.run_once() == 0
    assert conn.execute("SELECT message_id FROM scheduled_messages WHERE dialog_id=?", (personal_id,)).fetchone() == (
        99,
    )
    enrolled = {
        row[0]: row[1] for row in conn.execute("SELECT dialog_id, status FROM synced_dialogs ORDER BY dialog_id")
    }
    assert set(enrolled) == {7, personal_id, admin_id, discussion_id}
    assert unrelated_id not in enrolled
    assert conn.execute(
        "SELECT inclusion_basis FROM own_only_dialogs WHERE dialog_id=?", (discussion_id,)
    ).fetchone() == ('["personal_channel_discussion"]',)
    assert conn.execute("SELECT 1 FROM own_only_dialogs WHERE dialog_id=999").fetchone() is None
    assert len(client.entity_calls) == 2
    assert set(client.entity_calls) == {admin_id, unrelated_id}
    assert all(kwargs["flood_sleep_threshold"] == 0 for _, kwargs in client.requests)


@pytest.mark.asyncio
async def test_raw_scheduled_updates_ingest_without_messages_row(conn: sqlite3.Connection) -> None:
    manager = EventHandlerManager(MagicMock(), conn, asyncio.Event())
    scheduled = _message(21, "created", scheduled_at=1_900_000_021)
    await manager.on_raw_new_scheduled_message(SimpleNamespace(message=scheduled))
    await manager.on_raw_delete_scheduled_messages(
        SimpleNamespace(peer=PeerUser(user_id=42), messages=[21], sent_messages=None)
    )

    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute(
        "SELECT message_state, visibility, unpublished FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("cancelled", "author_only", 1)


@pytest.mark.asyncio
async def test_publication_reconciliation_runs_before_sync_enrollment(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(21), now=100)
    mark_scheduled_messages_removed(conn, 42, [21], [901], now=200)
    manager = EventHandlerManager(MagicMock(), conn, asyncio.Event())
    message = _message(901, "published")
    message.from_scheduled = True

    await manager.on_new_message(cast(_NewMessageEvent, SimpleNamespace(chat_id=42, is_private=False, message=message)))

    assert conn.execute(
        "SELECT message_state, published_message_id FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("published", 901)


class TestScheduledDialogId:
    def test_none_peer(self) -> None:
        assert scheduled_dialog_id(None) is None

    def test_channel_id_fallback(self) -> None:
        peer = SimpleNamespace(channel_id=123)
        result = scheduled_dialog_id(peer)
        assert result == -1000000000123

    def test_chat_id_fallback(self) -> None:
        peer = SimpleNamespace(chat_id=456)
        result = scheduled_dialog_id(peer)
        assert result == -456

    def test_user_id_fallback(self) -> None:
        peer = SimpleNamespace(user_id=789)
        result = scheduled_dialog_id(peer)
        assert result == 789

    def test_unknown_peer_returns_none(self) -> None:
        peer = SimpleNamespace()
        result = scheduled_dialog_id(peer)
        assert result is None


class TestUnixTimestamp:
    def test_datetime_conversion(self) -> None:
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
        assert _unix_timestamp(dt) == 1767225600

    def test_int_passthrough(self) -> None:
        assert _unix_timestamp(1_700_000_000) == 1_700_000_000

    def test_none_returns_none(self) -> None:
        assert _unix_timestamp(None) is None

    def test_bool_is_not_treated_as_int(self) -> None:
        assert _unix_timestamp(True) is True
