# pyright: reportAny=false

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon.errors import ChannelPrivateError
from telethon.tl.types import PeerUser

from mcp_telegram.activity_peer_resolve import LinkedChatResolution
from mcp_telegram.event_handlers import EventHandlerManager, _NewMessageEvent
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.own_only import OwnOnlyContext, query_own_only_candidates
from mcp_telegram.scheduled_messages import (
    ScheduledDiscoveryDemandAdapter,
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    ScheduledRepairDemandAdapter,
    _unix_timestamp,
    mark_scheduled_messages_removed,
    scheduled_dialog_id,
    upsert_scheduled_message,
    verify_scheduled_publication,
)
from mcp_telegram.sync_db import (
    SCHEDULED_ACTIVE_REPAIR_SECONDS,
    SCHEDULED_QUIET_DISCOVERY_SECONDS,
    _open_sync_db,
    ensure_sync_schema,
)
from mcp_telegram.telegram_demand import AcquisitionKind, DemandStatus, RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import (
    current_rpc_scope,
)


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
        self.scopes = []
        self.input_entity_calls: list[int] = []
        self.entity_calls: list[int] = []

    async def get_input_entity(self, _dialog_id: int) -> object:
        self.input_entity_calls.append(_dialog_id)
        return _dialog_id

    async def get_entity(self, _dialog_id: int) -> object:
        self.entity_calls.append(_dialog_id)
        entity = self.entities.get(_dialog_id)
        if isinstance(entity, Exception):
            raise entity
        return entity

    async def __call__(self, _request: object, **_kwargs: object) -> object:
        self.scopes.append(current_rpc_scope())
        self.requests.append((_request, _kwargs))
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
    worker = ScheduledMessageReconciler(
        client, conn, asyncio.Event(), policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0)
    )

    assert await worker.run_once() == 1
    assert conn.execute(
        "SELECT message_state, unpublished, unseen FROM scheduled_messages WHERE message_id=11"
    ).fetchone() == ("unknown_missing", 1, 1)
    assert len(client.requests) == 1
    assert client.requests[0][1] == {}


@pytest.mark.asyncio
async def test_reconciliation_floodwait_records_retry_and_stops_account_pass(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        [(42,), (43,)],
    )
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    client = _ScheduledSnapshotClient(call_error=TelegramRpcThrottled(retry_after_seconds=30))
    worker = ScheduledMessageReconciler(
        client, conn, asyncio.Event(), policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0)
    )

    assert await worker.run_once() == 0
    retry_at, error = conn.execute(
        "SELECT next_retry_at, last_error FROM scheduled_sync_state WHERE key='account'"
    ).fetchone()
    assert retry_at is not None and retry_at >= 30
    assert error == "TelegramRpcThrottled"
    assert len(client.requests) == 1
    assert client.requests[0][1] == {}


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
    worker = ScheduledMessageReconciler(
        client, conn, asyncio.Event(), policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0)
    )

    assert await worker.run_once() == 0
    assert client.requests == []
    assert client.input_entity_calls == []


@pytest.mark.asyncio
async def test_reconciliation_classifies_and_enrolls_own_only_candidates(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    captured: dict[str, float | None] = {}

    async def fake_resolve_linked_chat_id(
        client: object, conn: sqlite3.Connection, channel_id: int, *, timeout_s: float | None
    ) -> LinkedChatResolution:
        del client, conn, channel_id
        captured["timeout_s"] = timeout_s
        return LinkedChatResolution(linked_chat_id=discussion_id, flood_wait_seconds=None)

    monkeypatch.setattr("mcp_telegram.scheduled_messages.resolve_linked_chat_id", fake_resolve_linked_chat_id)
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=42, personal_channel_id=9001),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=53.0),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO scheduled_reconciliation_state "
        "(dialog_id, repair_due_at, discovery_due_at, updated_at) VALUES (?, NULL, 0, 0)",
        [(7,), (personal_id,), (admin_id,), (unrelated_id,), (discussion_id,), (999,)],
    )
    conn.commit()

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
    # An absent dialog row is not proof that prior ownership was revoked.
    assert conn.execute("SELECT 1 FROM own_only_dialogs WHERE dialog_id=999").fetchone() == (1,)
    assert len(client.entity_calls) == 2
    assert set(client.entity_calls) == {admin_id, unrelated_id}
    assert captured == {"timeout_s": 53.0}


@pytest.mark.asyncio
async def test_reconciliation_access_lost_candidate_log_has_dialog_context(
    conn: sqlite3.Connection,
    caplog: pytest.LogCaptureFixture,
) -> None:
    private_channel_id = -1000000009004
    channel_name = "Archived Fixture Channel"
    conn.execute(
        "INSERT INTO dialogs (dialog_id, name, type, hidden, archived) VALUES (?, ?, ?, ?, ?)",
        (private_channel_id, channel_name, "channel", 1, 1),
    )
    conn.commit()
    client = _ScheduledSnapshotClient(
        entities={
            private_channel_id: ChannelPrivateError(request=None),
        },
    )
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=42, personal_channel_id=9001),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )
    conn.execute(
        "INSERT OR REPLACE INTO scheduled_reconciliation_state "
        "(dialog_id, repair_due_at, discovery_due_at, updated_at) VALUES (?, NULL, 0, 0)",
        (private_channel_id,),
    )
    conn.commit()

    with caplog.at_level("WARNING", logger="mcp_telegram.access_lifecycle"):
        assert await worker.run_once() == 0

    records = [record for record in caplog.records if record.message.startswith("access_lost ")]
    assert len(records) == 1
    assert f"dialog_id={private_channel_id}" in records[0].message
    assert "reason_code=ChannelPrivateError" in records[0].message
    assert channel_name not in records[0].message
    assert records[0].exc_info is None
    status_row = conn.execute(
        "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id = ?",
        (private_channel_id,),
    ).fetchone()
    assert status_row is not None
    assert status_row[0] == "access_lost"
    assert status_row[1] is not None
    event_row = conn.execute(
        "SELECT kind, dialog_id FROM conversation_history_events WHERE dialog_id = ?",
        (private_channel_id,),
    ).fetchone()
    assert event_row == ("access_lost", private_channel_id)
    assert [row["dialog_id"] for row in query_own_only_candidates(conn, personal_channel_id=9001)] == []


@pytest.mark.asyncio
async def test_raw_scheduled_updates_ingest_without_messages_row(conn: sqlite3.Connection) -> None:
    client = MagicMock()
    manager = EventHandlerManager(client, conn, asyncio.Event(), client.get_input_entity)
    offered: list[DemandKind] = []
    shadow = MagicMock()

    def offer(kind: DemandKind) -> bool:
        assert not conn.in_transaction
        offered.append(kind)
        return True

    shadow.offer.side_effect = offer
    manager.bind_demand_shadow(shadow)
    scheduled = _message(21, "created", scheduled_at=1_900_000_021)
    await manager.on_raw_new_scheduled_message(SimpleNamespace(message=scheduled))
    await manager.on_raw_delete_scheduled_messages(
        SimpleNamespace(peer=PeerUser(user_id=42), messages=[21], sent_messages=None)
    )

    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute(
        "SELECT message_state, visibility, unpublished FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("cancelled", "author_only", 1)
    assert offered == [
        DemandKind.SCHEDULED_REPAIR,
        DemandKind.SCHEDULED_DISCOVERY,
        DemandKind.SCHEDULED_REPAIR,
        DemandKind.SCHEDULED_DISCOVERY,
    ]


@pytest.mark.asyncio
async def test_publication_reconciliation_runs_before_sync_enrollment(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(21), now=100)
    mark_scheduled_messages_removed(conn, 42, [21], [901], now=200)
    client = MagicMock()
    manager = EventHandlerManager(client, conn, asyncio.Event(), client.get_input_entity)
    message = _message(901, "published")
    message.from_scheduled = True

    await manager.on_new_message(cast(_NewMessageEvent, SimpleNamespace(chat_id=42, is_private=False, message=message)))

    assert conn.execute(
        "SELECT message_state, published_message_id FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("published", 901)


# ---------------------------------------------------------------------------
# scheduled_dialog_id fallback path (raw-update peer object support)
# ---------------------------------------------------------------------------


def test_scheduled_dialog_id_fallback_channel_id() -> None:
    """When get_peer_id raises TypeError, fall back to channel_id attribute."""
    peer = SimpleNamespace(channel_id=123456)
    assert scheduled_dialog_id(peer) == -1000000000000 - 123456


def test_scheduled_dialog_id_fallback_chat_id() -> None:
    """When get_peer_id raises TypeError, fall back to chat_id attribute."""
    peer = SimpleNamespace(chat_id=789)
    assert scheduled_dialog_id(peer) == -789


def test_scheduled_dialog_id_fallback_user_id() -> None:
    """When get_peer_id raises TypeError and no channel/chat_id, fall back to user_id."""
    peer = SimpleNamespace(user_id=42)
    assert scheduled_dialog_id(peer) == 42


def test_scheduled_dialog_id_none() -> None:
    """None peer returns None."""
    assert scheduled_dialog_id(None) is None


# ---------------------------------------------------------------------------
# _unix_timestamp datetime branch
# ---------------------------------------------------------------------------


def test_unix_timestamp_from_datetime() -> None:
    """_unix_timestamp converts datetime to int Unix timestamp."""
    dt = datetime(2025, 6, 15, 12, 30, 0, tzinfo=UTC)
    assert _unix_timestamp(dt) == int(dt.timestamp())


def test_unix_timestamp_from_int() -> None:
    """_unix_timestamp passes through int values unchanged."""
    assert _unix_timestamp(1_700_000_000) == 1_700_000_000


def test_unix_timestamp_from_none() -> None:
    """_unix_timestamp returns None for None input."""
    assert _unix_timestamp(None) is None


def test_realtime_updates_coalesce_into_one_dirty_dialog(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    upsert_scheduled_message(conn, 42, _message(12), now=101)
    conn.commit()

    state = conn.execute(
        "SELECT repair_due_at, dirty_since, dirty_generation FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone()
    assert state == (100, 100, 2)
    assert conn.execute("SELECT inclusion_basis FROM own_only_dialogs WHERE dialog_id=42").fetchone() == (
        '["scheduled_event"]',
    )


@pytest.mark.asyncio
async def test_reconciliation_processes_only_one_bounded_slice(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO dialogs(dialog_id, type, hidden) VALUES (?, 'user', 0)",
        [(dialog_id,) for dialog_id in range(1, 6)],
    )
    conn.executemany(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (?, NULL, 0, 0)",
        [(dialog_id,) for dialog_id in range(1, 6)],
    )
    conn.commit()
    client = _ScheduledSnapshotClient()
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=999),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10, max_dialogs_per_slice=2),
    )

    await worker.run_once()

    assert len(client.requests) == 2
    assert conn.execute("SELECT COUNT(*) FROM scheduled_reconciliation_state WHERE discovery_due_at=0").fetchone() == (
        3,
    )


@pytest.mark.asyncio
async def test_concurrent_event_prevents_stale_snapshot_apply(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()

    class _ConcurrentClient(_ScheduledSnapshotClient):
        async def __call__(self, _request: object, **_kwargs: object) -> object:
            del _request, _kwargs
            upsert_scheduled_message(conn, 42, _message(12), now=200)
            conn.commit()
            return SimpleNamespace(messages=[])

    worker = ScheduledMessageReconciler(
        _ConcurrentClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )
    assert await worker.run_once() == 0
    assert conn.execute(
        "SELECT message_id FROM scheduled_messages WHERE dialog_id=42 AND message_state='scheduled' ORDER BY message_id"
    ).fetchall() == [(11,), (12,)]
    assert conn.execute(
        "SELECT dirty_generation, dirty_since FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone() == (2, 100)


def test_scheduled_policy_targets_have_one_code_owned_definition() -> None:
    policy = ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10)
    repair_target = demand_contract(DemandKind.SCHEDULED_REPAIR).freshness_target
    discovery_target = demand_contract(DemandKind.SCHEDULED_DISCOVERY).freshness_target
    assert repair_target is not None
    assert discovery_target is not None
    assert int(repair_target.total_seconds()) == SCHEDULED_ACTIVE_REPAIR_SECONDS
    assert int(discovery_target.total_seconds()) == SCHEDULED_QUIET_DISCOVERY_SECONDS
    assert not hasattr(policy, "active_repair_seconds")
    assert not hasattr(policy, "quiet_discovery_seconds")


def test_scheduled_demand_status_reads_repair_and_discovery_due_state(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (42, 100, 200, 0)"
    )
    conn.commit()
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    repair = ScheduledRepairDemandAdapter(reconciler).status(100)
    discovery = ScheduledDiscoveryDemandAdapter(reconciler).status(100)

    assert repair is not None
    assert repair.release_at == 100
    assert repair.freshness_deadline == 100
    assert discovery is not None
    assert discovery.release_at == 200
    assert discovery.freshness_deadline == 200


def test_scheduled_discovery_status_wakes_for_unseeded_candidate_after_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "cold-start.db"
    ensure_sync_schema(db_path)
    conn = _open_sync_db(db_path)
    conn.execute("INSERT INTO dialogs(dialog_id, type, hidden) VALUES (42, 'user', 0)")
    conn.execute(
        "INSERT INTO own_only_dialogs(dialog_id, inclusion_basis, updated_at) VALUES (42, ?, 1)",
        ('["direct_message"]',),
    )
    conn.commit()
    policy = ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10)
    try:
        adapter = ScheduledDiscoveryDemandAdapter(
            ScheduledMessageReconciler(_ScheduledSnapshotClient(), conn, asyncio.Event(), policy=policy)
        )
        changes_before = conn.total_changes
        status = adapter.status(100.0)
        assert status == DemandStatus(release_at=0.0)
        assert conn.total_changes == changes_before
    finally:
        conn.close()

    reopened = _open_sync_db(db_path)
    try:
        adapter = ScheduledDiscoveryDemandAdapter(
            ScheduledMessageReconciler(_ScheduledSnapshotClient(), reopened, asyncio.Event(), policy=policy)
        )
        assert adapter.status(100.0) == DemandStatus(release_at=0.0)
        assert reopened.execute("SELECT COUNT(*) FROM scheduled_reconciliation_state").fetchone() == (0,)
    finally:
        reopened.close()


@pytest.mark.parametrize(
    ("adapter_type", "queue_due_at"),
    [
        (ScheduledRepairDemandAdapter, 100),
        (ScheduledDiscoveryDemandAdapter, 200),
    ],
)
def test_scheduled_demand_status_honors_future_account_retry(
    conn: sqlite3.Connection,
    adapter_type: type[ScheduledRepairDemandAdapter] | type[ScheduledDiscoveryDemandAdapter],
    queue_due_at: int,
) -> None:
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (42, 100, 200, 0)"
    )
    conn.execute("UPDATE scheduled_sync_state SET next_retry_at=300 WHERE key='account'")
    conn.commit()
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    status = adapter_type(reconciler).status(100)

    assert status is not None
    assert status.release_at == 300
    assert status.freshness_deadline == queue_due_at


@pytest.mark.parametrize(
    ("adapter_type", "queue_due_at"),
    [
        (ScheduledRepairDemandAdapter, 300),
        (ScheduledDiscoveryDemandAdapter, 400),
    ],
)
def test_scheduled_demand_status_keeps_later_queue_release(
    conn: sqlite3.Connection,
    adapter_type: type[ScheduledRepairDemandAdapter] | type[ScheduledDiscoveryDemandAdapter],
    queue_due_at: int,
) -> None:
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (42, ?, ?, 0)",
        (300, 400),
    )
    conn.execute("UPDATE scheduled_sync_state SET next_retry_at=200 WHERE key='account'")
    conn.commit()
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    status = adapter_type(reconciler).status(100)

    assert status is not None
    assert status.release_at == queue_due_at
    assert status.freshness_deadline == queue_due_at


@pytest.mark.asyncio
async def test_scheduled_repair_adapter_runs_only_repair_rows_with_precise_scope(conn: sqlite3.Connection) -> None:
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.execute(
        "UPDATE scheduled_reconciliation_state SET repair_due_at=0, discovery_due_at=9999999999 WHERE dialog_id=42"
    )
    conn.commit()
    client = _ScheduledSnapshotClient({42: []})
    reconciler = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )
    budget = RpcAttemptBudget(limit=16)

    await ScheduledRepairDemandAdapter(reconciler).run_slice(budget)

    assert len(client.requests) == 1
    assert client.scopes[0].demand_kind is DemandKind.SCHEDULED_REPAIR
    assert client.scopes[0].acquisition_kind is AcquisitionKind.SCHEDULED_MESSAGES_SNAPSHOT
    assert client.scopes[0].attempt_budget is budget
    assert conn.execute("SELECT repair_due_at FROM scheduled_reconciliation_state WHERE dialog_id=42").fetchone() == (
        None,
    )


@pytest.mark.asyncio
async def test_scheduled_discovery_adapter_runs_only_discovery_rows_with_precise_scope(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("INSERT INTO dialogs(dialog_id, type, hidden) VALUES (42, 'user', 0)")
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (42, NULL, 0, 0)"
    )
    conn.commit()
    client = _ScheduledSnapshotClient({42: []})
    reconciler = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=999),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    await ScheduledDiscoveryDemandAdapter(reconciler).run_slice(RpcAttemptBudget(limit=16))

    assert len(client.requests) == 1
    assert client.scopes[0].demand_kind is DemandKind.SCHEDULED_DISCOVERY
    assert client.scopes[0].acquisition_kind is AcquisitionKind.SCHEDULED_MESSAGES_SNAPSHOT
    assert conn.execute("SELECT repair_due_at FROM scheduled_reconciliation_state WHERE dialog_id=42").fetchone() == (
        None,
    )


@pytest.mark.asyncio
async def test_legacy_run_once_attributes_each_selected_row_to_its_demand_kind(
    conn: sqlite3.Connection,
) -> None:
    upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.execute(
        "UPDATE scheduled_reconciliation_state SET repair_due_at=0, discovery_due_at=9999999999 WHERE dialog_id=42"
    )
    conn.execute("INSERT INTO dialogs(dialog_id, type, hidden) VALUES (43, 'user', 0)")
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (43, NULL, 0, 0)"
    )
    conn.commit()
    client = _ScheduledSnapshotClient({42: [], 43: []})
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=999),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10, max_dialogs_per_slice=2),
    )

    await worker.run_once()

    assert [scope.demand_kind for scope in client.scopes] == [
        DemandKind.SCHEDULED_REPAIR,
        DemandKind.SCHEDULED_DISCOVERY,
    ]


@pytest.mark.asyncio
async def test_future_account_retry_does_not_appear_runnable(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (42, 0, 0, 0)"
    )
    conn.execute("UPDATE scheduled_sync_state SET next_retry_at=? WHERE key='account'", (now + 30,))
    conn.commit()
    client = _ScheduledSnapshotClient()
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10, state_scan_seconds=60),
    )

    assert await worker.run_once() == 0
    assert client.requests == []
    assert worker._wait_timeout(now=now) == 30


def test_candidate_seed_is_not_repeated_for_an_immediate_slice(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def fake_candidates(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr("mcp_telegram.scheduled_messages.query_own_only_candidates", fake_candidates)
    worker = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    worker._seed_candidates_if_due(100)
    worker._seed_candidates_if_due(100)

    assert calls == 1


def test_due_selection_prioritizes_oldest_dirty_dialog(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO scheduled_reconciliation_state "
        "(dialog_id, repair_due_at, discovery_due_at, dirty_since, updated_at) VALUES (?, 0, 999, ?, 0)",
        [(41, 30), (42, 10), (43, None)],
    )
    conn.commit()
    worker = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    assert [row[0] for row in worker._due_rows(100)] == [42, 41, 43]


@pytest.mark.asyncio
async def test_excluded_discovery_removes_only_scheduled_ownership_basis(conn: sqlite3.Connection) -> None:
    dialog_id = -1000000009005
    conn.execute(
        "INSERT INTO dialogs(dialog_id, type, hidden) VALUES (?, 'channel', 0)",
        (dialog_id,),
    )
    conn.execute(
        "INSERT INTO own_only_dialogs(dialog_id, inclusion_basis, updated_at) VALUES (?, ?, ?)",
        (dialog_id, '["owned_channel","scheduled_event"]', 1),
    )
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, repair_due_at, discovery_due_at, updated_at) "
        "VALUES (?, NULL, 0, 0)",
        (dialog_id,),
    )
    conn.commit()
    worker = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(entities={dialog_id: SimpleNamespace(creator=False, admin_rights=None)}),
        conn,
        asyncio.Event(),
        OwnOnlyContext(account_id=42),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
    )

    assert await worker.run_once() == 0
    assert conn.execute("SELECT inclusion_basis FROM own_only_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        '["owned_channel"]',
    )
