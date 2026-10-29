# pyright: reportAny=false

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest
from telethon import types
from telethon.errors import ChannelPrivateError
from telethon.tl.types import PeerUser

from mcp_telegram.delivered_update import DeliveredUpdate, delivered_update_context
from mcp_telegram.event_handlers import _NewMessageEvent
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.observation_order import allocate_observation_order
from mcp_telegram.scheduled_messages import (
    ScheduledDiscoveryDemandAdapter,
    ScheduledMessageReconciler,
    ScheduledReconciliationPolicy,
    ScheduledRepairDemandAdapter,
    _unix_timestamp,
    mark_missing_from_snapshot,
    mark_scheduled_messages_removed,
    scheduled_dialog_has_due_work,
    scheduled_dialog_id,
    upsert_scheduled_message,
    verify_scheduled_publication,
)
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import AcquisitionKind, DemandStatus, DurableDemandAdapter, RpcAttemptBudget
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind
from mcp_telegram.telegram_rpc_consumers import DURABLE_DEMAND_ORDER, DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcScope,
    current_rpc_scope,
)
from tests.sqlite_writer_helpers import event_manager, inline_writer


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
        self.scopes: list[TelegramRpcScope] = []
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
        return types.messages.Messages(
            messages=cast(list[types.TypeMessage], self.snapshots.get(dialog_id, [])), topics=[], users=[], chats=[]
        )


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    connection.execute("UPDATE scheduled_sync_state SET account_id=42 WHERE key='account'")
    connection.commit()
    yield connection
    connection.close()


async def _run_all_demand_slices(worker: ScheduledMessageReconciler) -> int:
    total = await worker.run_demand_slice(DemandKind.SCHEDULED_REPAIR)
    return total + await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY)


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
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "first", scheduled_at=1_900_000_001), now=100)
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "rescheduled", scheduled_at=1_900_000_101), now=101)
    conn.commit()

    row = conn.execute(
        "SELECT message_id, scheduled_at, text, message_state, visibility, unpublished, unseen FROM scheduled_messages"
    ).fetchone()
    assert row == (11, 1_900_000_101, "rescheduled", "scheduled", "author_only", 1, 1)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
    assert conn.execute("SELECT stemmed_text FROM scheduled_messages_fts").fetchone() == ("rescheduled",)


def test_upsert_retains_due_queue_rows(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, scheduled_at=100), now=101)
    assert conn.execute("SELECT scheduled_at,message_state FROM scheduled_messages").fetchone() == (100, "scheduled")


def test_removal_retains_cancel_and_unverified_publication_evidence(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(12), now=100)
    conn.commit()
    with write_transaction(conn):
        mark_scheduled_messages_removed(conn, 42, [11, 12], [901], now=200)

    rows = conn.execute(
        "SELECT message_id, message_state, visibility, unpublished, publication_hint_message_id "
        "FROM scheduled_messages ORDER BY message_id"
    ).fetchall()
    assert rows == [
        (11, "unknown_missing", "author_only", 1, 901),
        (12, "cancelled", "author_only", 1, None),
    ]
    assert conn.execute("SELECT message_id FROM scheduled_messages_fts ORDER BY message_id").fetchall() == [(11,)]
    conn.commit()
    with write_transaction(conn):
        assert verify_scheduled_publication(conn, 42, 901, published_at=199, now=201) == 1
    assert conn.execute(
        "SELECT message_state, visibility, unpublished, unseen, published_message_id, published_at "
        "FROM scheduled_messages WHERE message_id=11"
    ).fetchone() == ("published", "chat_visible", 0, 0, 901, 199)
    assert conn.execute("SELECT COUNT(*) FROM scheduled_messages_fts").fetchone() == (0,)


@pytest.mark.parametrize("publication_first", [False, True])
def test_publication_evidence_reconciles_either_order_across_restart(tmp_path: Path, publication_first: bool) -> None:
    path = tmp_path / "publication.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    with write_transaction(connection):
        upsert_scheduled_message(connection, 42, _message(11), now=100)
        if publication_first:
            assert verify_scheduled_publication(connection, 42, 901, published_at=190, now=200) == 0
        else:
            mark_scheduled_messages_removed(connection, 42, [11], [901], now=200)
    connection.close()
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    try:
        with write_transaction(connection):
            if publication_first:
                mark_scheduled_messages_removed(connection, 42, [11], [901], now=210)
            else:
                assert verify_scheduled_publication(connection, 42, 901, published_at=190, now=210) == 1
        assert connection.execute(
            "SELECT message_state, published_message_id, published_at, publication_verified_at "
            "FROM scheduled_messages WHERE dialog_id=42 AND message_id=11"
        ).fetchone() == ("published", 901, 190, 200 if publication_first else 210)
        assert connection.execute("SELECT COUNT(*) FROM scheduled_messages_fts").fetchone() == (0,)
    finally:
        connection.close()


def test_duplicate_queue_events_do_not_regress_verified_publication(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11), now=100)
        mark_scheduled_messages_removed(conn, 42, [11], [901], now=200)
        verify_scheduled_publication(conn, 42, 901, published_at=190, now=201)
    before = conn.execute("SELECT * FROM scheduled_messages").fetchone()
    with write_transaction(conn):
        assert verify_scheduled_publication(conn, 42, 901, published_at=999, now=300) == 0
        mark_scheduled_messages_removed(conn, 42, [11], [901], now=301)
        mark_scheduled_messages_removed(conn, 42, [11], now=302)
        mark_scheduled_messages_removed(conn, 42, [11], [902], now=303)
        upsert_scheduled_message(conn, 42, _message(11, "stale queue"), now=304)
    assert conn.execute("SELECT * FROM scheduled_messages").fetchone() == before
    assert conn.execute("SELECT published_at, verified_at FROM scheduled_publication_evidence").fetchone() == (190, 201)
    assert conn.execute("SELECT COUNT(*) FROM scheduled_messages_fts").fetchone() == (0,)


def test_publication_hint_cannot_use_other_dialog_or_ordinary_archive_message(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES (42,901,190,'ordinary')")
        verify_scheduled_publication(conn, 43, 901, published_at=190, now=200)
        mark_scheduled_messages_removed(conn, 42, [11], [901], now=201)
    assert conn.execute(
        "SELECT message_state,published_message_id,publication_verified_at FROM scheduled_messages"
    ).fetchone() == ("unknown_missing", None, None)


def test_publication_without_telegram_date_keeps_publication_time_unknown(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        mark_scheduled_messages_removed(conn, 42, [11], [901], now=200)
        verify_scheduled_publication(conn, 42, 901, now=201)
    assert conn.execute(
        "SELECT message_state,published_at,publication_verified_at FROM scheduled_messages"
    ).fetchone() == ("published", None, 201)


def test_verified_evidence_and_publication_transition_rollback_together(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        mark_scheduled_messages_removed(conn, 42, [11], [901], now=200)
        conn.execute(
            "CREATE TRIGGER reject_publication BEFORE UPDATE ON scheduled_messages "
            "WHEN NEW.message_state='published' BEGIN SELECT RAISE(ABORT,'publication rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="publication rejected"), write_transaction(conn):
        verify_scheduled_publication(conn, 42, 901, published_at=190, now=201)
    assert conn.execute("SELECT COUNT(*) FROM scheduled_publication_evidence").fetchone() == (0,)
    assert conn.execute("SELECT message_state FROM scheduled_messages").fetchone() == ("unknown_missing",)


@pytest.mark.asyncio
async def test_reconciliation_snapshot_marks_disappearance_nonvisible(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO synced_dialogs (dialog_id, status) VALUES (42, 'synced')")
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    client = _ScheduledSnapshotClient({42: []})
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )

    conn.commit()
    assert await _run_all_demand_slices(worker) == 1
    assert conn.execute(
        "SELECT message_state, unpublished, unseen FROM scheduled_messages WHERE message_id=11"
    ).fetchone() == ("unknown_missing", 1, 1)
    assert conn.execute(
        "SELECT message_id FROM scheduled_messages_fts WHERE scheduled_messages_fts MATCH 'draft'"
    ).fetchall() == [(11,)]
    assert len(client.requests) == 1
    assert client.requests[0][1] == {}


def test_scheduled_fts_backfill_restores_unknown_missing_text(conn: sqlite3.Connection) -> None:
    from mcp_telegram.sync_db import _apply_migration_91

    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "retained zebra"), now=100)
        mark_missing_from_snapshot(
            conn,
            42,
            [11],
            now=101,
            observation_order=allocate_observation_order(conn),
        )
        conn.execute("DELETE FROM scheduled_messages_fts WHERE dialog_id=42 AND message_id=11")
    conn.commit()

    assert _apply_migration_91(conn, 90) == 91
    assert conn.execute(
        "SELECT message_id FROM scheduled_messages_fts WHERE scheduled_messages_fts MATCH 'zebra'"
    ).fetchall() == [(11,)]


@pytest.mark.asyncio
async def test_reconciliation_floodwait_does_not_add_domain_retry(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
        [(42,), (43,)],
    )
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    client = _ScheduledSnapshotClient(call_error=TelegramRpcThrottled(retry_after_seconds=30))
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )

    conn.commit()
    adapter = ScheduledDiscoveryDemandAdapter(worker, account_id=42)
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    with pytest.raises(TelegramRpcThrottled):
        await adapter.run_slice(RpcAttemptBudget(limit=1, selected=status.selected))
    retry_at, error = conn.execute(
        "SELECT next_retry_at, last_error FROM scheduled_sync_state WHERE key='account'"
    ).fetchone()
    assert retry_at is None
    assert error is None
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
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )

    conn.commit()
    assert await _run_all_demand_slices(worker) == 0
    assert client.requests == []
    assert client.input_entity_calls == []


@pytest.mark.asyncio
async def test_candidates_cover_directory_peers_and_archived_dialogs(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO dialogs(dialog_id,type,hidden,archived) VALUES (?,?,1,1)",
        [(7, "user"), (8, "bot"), (-9, "chat"), (-1_000_000_000_10, "channel")],
    )
    conn.commit()
    worker = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=53.0),
        writer=inline_writer(conn),
    )
    worker.bind_account_id(42)

    assert worker._candidate_ids() == {7, 8, -9, -1_000_000_000_10, 42}
    assert conn.execute("SELECT COUNT(*) FROM synced_dialogs").fetchone() == (0,)


@pytest.mark.asyncio
async def test_raw_scheduled_updates_ingest_without_messages_row(conn: sqlite3.Connection) -> None:
    client = MagicMock()
    manager = event_manager(client, conn, asyncio.Event())
    offered: list[DemandKind] = []
    sink = MagicMock()

    def offer(kind: DemandKind) -> bool:
        assert not conn.in_transaction
        offered.append(kind)
        return True

    sink.offer.side_effect = offer
    manager.bind_demand_sink(sink)
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
async def test_scheduled_events_on_complete_baseline_do_not_request_snapshots(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,baseline_observed_at,coverage_state) "
        "VALUES(42,?,?, 'complete')",
        (now, now),
    )
    conn.commit()
    client = MagicMock()
    manager = event_manager(client, conn, asyncio.Event())
    sink = MagicMock()
    manager.bind_demand_sink(sink)

    await manager.on_raw_new_scheduled_message(
        SimpleNamespace(message=_message(21, "created", scheduled_at=1_900_000_021))
    )
    await manager.on_raw_delete_scheduled_messages(
        SimpleNamespace(peer=PeerUser(user_id=42), messages=[21], sent_messages=None)
    )

    assert sink.offer.call_count == 0
    assert conn.execute(
        "SELECT message_state FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("cancelled",)


@pytest.mark.asyncio
async def test_publication_reconciliation_runs_before_sync_enrollment(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(21), now=100)
    conn.commit()
    with write_transaction(conn):
        mark_scheduled_messages_removed(conn, 42, [21], [901], now=200)
    client = MagicMock()
    manager = event_manager(client, conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    message = _message(901, "published")
    message.from_scheduled = True

    await manager.on_new_message(cast(_NewMessageEvent, SimpleNamespace(chat_id=42, is_private=False, message=message)))

    assert conn.execute(
        "SELECT message_state, published_message_id, published_at "
        "FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("published", 901, 1_900_000_000)


@pytest.mark.asyncio
async def test_publication_event_before_hint_is_retained_without_sync_enrollment(conn: sqlite3.Connection) -> None:
    client = MagicMock()
    manager = event_manager(client, conn, asyncio.Event())
    manager.bind_demand_sink(MagicMock())
    message = _message(901, "published", scheduled_at=190)
    message.from_scheduled = True
    await manager.on_new_message(cast(_NewMessageEvent, SimpleNamespace(chat_id=42, is_private=False, message=message)))
    await manager.on_raw_delete_scheduled_messages(
        SimpleNamespace(peer=PeerUser(user_id=42), messages=[21], sent_messages=[901])
    )
    assert conn.execute(
        "SELECT message_state, published_message_id, published_at "
        "FROM scheduled_messages WHERE dialog_id=42 AND message_id=21"
    ).fetchone() == ("published", 901, 190)
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone() == (0,)


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
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(12), now=101)
    conn.commit()

    state = conn.execute(
        "SELECT repair_due_at, discovery_due_at, dirty_since, dirty_generation FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone()
    assert state == (None, 100, 100, 2)
    assert conn.execute("SELECT inclusion_basis FROM own_only_dialogs WHERE dialog_id=42").fetchone() is None


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
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10, max_dialogs_per_slice=2),
        writer=inline_writer(conn),
    )
    worker.bind_account_id(999)

    conn.commit()
    await _run_all_demand_slices(worker)

    assert len(client.requests) == 2
    assert conn.execute("SELECT COUNT(*) FROM scheduled_reconciliation_state WHERE discovery_due_at=0").fetchone() == (
        3,
    )


@pytest.mark.asyncio
async def test_concurrent_event_prevents_stale_snapshot_apply(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11), now=100)
    conn.execute("UPDATE scheduled_sync_state SET last_snapshot_at=77,last_error='prior_failure'")
    conn.commit()

    class _ConcurrentClient(_ScheduledSnapshotClient):
        async def __call__(self, _request: object, **_kwargs: object) -> object:
            del _request, _kwargs
            with write_transaction(conn):
                upsert_scheduled_message(conn, 42, _message(12), now=200)
            conn.commit()
            return types.messages.Messages(messages=[], topics=[], users=[], chats=[])

    worker = ScheduledMessageReconciler(
        _ConcurrentClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )
    conn.commit()
    assert await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY) == 0
    assert conn.execute(
        "SELECT message_id FROM scheduled_messages WHERE dialog_id=42 AND message_state='scheduled' ORDER BY message_id"
    ).fetchall() == [(11,), (12,)]
    assert conn.execute(
        "SELECT dirty_generation, dirty_since FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone() == (2, 100)

    assert conn.execute("SELECT last_snapshot_at,last_error FROM scheduled_sync_state").fetchone() == (
        77,
        "prior_failure",
    )


@pytest.mark.asyncio
async def test_continuity_gap_fences_snapshot_when_recovery_is_already_pending(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES(42,'user',0)")
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,dirty_since,dirty_generation,updated_at,"
        "coverage_state,coverage_reason) VALUES(42,0,0,1,0,'unknown','baseline_unknown')"
    )
    conn.commit()
    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()

    class InFlightSnapshotClient(_ScheduledSnapshotClient):
        async def __call__(self, _request: object, **_kwargs: object) -> object:
            snapshot_started.set()
            await release_snapshot.wait()
            return types.messages.Messages(
                messages=cast(list[types.TypeMessage], [_message(99)]), topics=[], users=[], chats=[]
            )

    worker = ScheduledMessageReconciler(
        InFlightSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    acquisition = asyncio.create_task(worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY))
    await snapshot_started.wait()

    await worker.invalidate_continuity(42)
    release_snapshot.set()
    await acquisition

    assert conn.execute(
        "SELECT dirty_generation,discovery_due_at,coverage_state FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone() == (2, 0, "unknown")
    assert conn.execute("SELECT COUNT(*) FROM scheduled_messages WHERE dialog_id=42").fetchone() == (0,)
    assert conn.execute("SELECT snapshot_observation_order FROM scheduled_reconciliation_state").fetchone() == (None,)


@pytest.mark.parametrize("coverage_state", ["inaccessible", "unsupported"])
def test_scheduled_event_reopens_terminal_coverage_for_finite_recovery(
    conn: sqlite3.Connection, coverage_state: str
) -> None:
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,coverage_state,coverage_reason) "
        "VALUES(42,100,?,'prior_terminal_state')",
        (coverage_state,),
    )
    conn.commit()

    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(21, scheduled_at=1_900_000_021), now=200)

    assert conn.execute(
        "SELECT discovery_due_at,dirty_generation,coverage_state,coverage_reason "
        "FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone() == (200, 1, "unknown", "scheduled_event_after_terminal_coverage")
    assert scheduled_dialog_has_due_work(conn, 42)


def test_scheduled_demands_have_no_periodic_freshness_target() -> None:
    policy = ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10)
    assert demand_contract(DemandKind.SCHEDULED_REPAIR).freshness_target is None
    assert demand_contract(DemandKind.SCHEDULED_DISCOVERY).freshness_target is None
    assert not hasattr(policy, "state_scan_seconds")


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
        writer=inline_writer(conn),
    )

    repair = ScheduledRepairDemandAdapter(reconciler, account_id=42).status(100)
    discovery = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42).status(100)

    assert repair is not None
    assert repair.release_at == 100
    assert repair.freshness_deadline == 100
    assert discovery is None
    conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL WHERE dialog_id=42")
    conn.commit()
    discovery = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42).status(100)
    assert discovery is not None and discovery.release_at == 200 and discovery.freshness_deadline == 200


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
            ScheduledMessageReconciler(
                _ScheduledSnapshotClient(), conn, asyncio.Event(), policy=policy, writer=inline_writer(conn)
            ),
            account_id=42,
        )
        changes_before = conn.total_changes
        status = adapter.status(100.0)
        assert status is not None and status.release_at == 0.0
        assert conn.total_changes == changes_before
    finally:
        conn.close()

    reopened = _open_sync_db(db_path)
    try:
        adapter = ScheduledDiscoveryDemandAdapter(
            ScheduledMessageReconciler(
                _ScheduledSnapshotClient(), reopened, asyncio.Event(), policy=policy, writer=inline_writer(reopened)
            ),
            account_id=42,
        )
        status = adapter.status(100.0)
        assert status is not None and status.release_at == 0.0
        assert reopened.execute("SELECT COUNT(*) FROM scheduled_reconciliation_state").fetchone() == (0,)
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_scheduled_seed_receipt_allows_later_unseeded_candidate_under_real_coordinator(
    conn: sqlite3.Connection,
) -> None:
    class IdleAdapter:
        def status(self, now: float, *, excluded: frozenset[object] = frozenset()) -> DemandStatus | None:
            del now, excluded
            return None

        async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
            del budget
            raise AssertionError("only scheduled discovery should run")

    class StopAfterAcceptedSeed:
        def __init__(self, shutdown: asyncio.Event, seed_another: Callable[[], None]) -> None:
            self.shutdown = shutdown
            self.seed_another = seed_another
            self.accepted = 0

        def observe_demand(self, *, outcome: str, **_fields: object) -> None:
            if outcome == "completed":
                self.accepted += 1
                if self.accepted == 1:
                    self.seed_another()
                else:
                    self.shutdown.set()

    def enroll(dialog_id: int) -> None:
        conn.execute("INSERT INTO dialogs(dialog_id, type, hidden) VALUES (?, 'user', 0)", (dialog_id,))
        conn.execute(
            "INSERT INTO own_only_dialogs(dialog_id, inclusion_basis, updated_at) VALUES (?, ?, 1)",
            (dialog_id, '["direct_message"]'),
        )
        conn.commit()

    enroll(42)
    shutdown = asyncio.Event()
    client = _ScheduledSnapshotClient()
    reconciler = ScheduledMessageReconciler(
        client,
        conn,
        shutdown,
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    adapter = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    adapters: dict[DemandKind, DurableDemandAdapter] = {
        kind: adapter if kind is DemandKind.SCHEDULED_DISCOVERY else cast(DurableDemandAdapter, IdleAdapter())
        for kind in DURABLE_DEMAND_ORDER
    }
    coordinator = TelegramDemandCoordinator(
        adapters,
        shutdown,
        clock=lambda: 100.0,
        observer=StopAfterAcceptedSeed(shutdown, lambda: enroll(43)),
    )

    await coordinator.run()
    assert conn.execute("SELECT dialog_id FROM scheduled_reconciliation_state ORDER BY dialog_id").fetchall() == [
        (42,),
        (43,),
    ]
    assert client.requests == []


@pytest.mark.asyncio
async def test_scheduled_seed_binds_account_before_directory_generation_exists(
    conn: sqlite3.Connection,
) -> None:
    conn.execute("UPDATE scheduled_sync_state SET account_id=NULL,directory_generation=NULL WHERE key='account'")
    conn.commit()
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    adapter = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)

    initial_seed = adapter.status(time.time())
    assert initial_seed is not None and initial_seed.selected is not None
    assert initial_seed.selected.key.target == "seed"
    await adapter.run_slice(RpcAttemptBudget(limit=16, selected=initial_seed.selected))
    assert conn.execute(
        "SELECT account_id,directory_generation FROM scheduled_sync_state WHERE key='account'"
    ).fetchone() == (42, None)

    conn.execute("UPDATE dialog_directory_publication SET account_id=42,generation=7 WHERE singleton=1")
    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES(43,'user',0)")
    conn.commit()
    published_seed = adapter.status(time.time())
    assert published_seed is not None and published_seed.selected is not None
    assert published_seed.selected.key.target == "seed"
    await adapter.run_slice(RpcAttemptBudget(limit=16, selected=published_seed.selected))

    assert conn.execute(
        "SELECT account_id,directory_generation FROM scheduled_sync_state WHERE key='account'"
    ).fetchone() == (42, 7)
    new_peer_coverage = conn.execute(
        "SELECT coverage_state,discovery_due_at FROM scheduled_reconciliation_state WHERE dialog_id=43"
    ).fetchone()
    assert new_peer_coverage is not None
    assert new_peer_coverage[0] == "unknown" and new_peer_coverage[1] is not None
    with pytest.raises(RuntimeError, match="account"):
        ScheduledDiscoveryDemandAdapter(reconciler, account_id=99).status(time.time())
    assert conn.execute("SELECT account_id FROM scheduled_sync_state WHERE key='account'").fetchone() == (42,)


@pytest.mark.asyncio
async def test_scheduled_coordinator_runs_selected_dialog_while_queue_head_is_held(
    conn: sqlite3.Connection,
) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, discovery_due_at, updated_at) VALUES (41, ?, ?)",
        (now, now),
    )
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, discovery_due_at, updated_at) VALUES (42, ?, ?)",
        (now, now),
    )
    conn.commit()
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, scheduled_at=now + 3600), now=now)
    conn.commit()

    class IdleAdapter:
        def status(self, timestamp: float, *, excluded: frozenset[object] = frozenset()) -> DemandStatus | None:
            del timestamp, excluded
            return None

        async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
            del budget
            raise AssertionError("only scheduled discovery should run")

    class StopOnCompletion:
        def __init__(self, shutdown: asyncio.Event) -> None:
            self.shutdown = shutdown

        def observe_demand(self, *, outcome: str, **_fields: object) -> None:
            if outcome == "completed":
                self.shutdown.set()

    client = _ScheduledSnapshotClient({42: [_message(12, scheduled_at=now + 3600)]})
    shutdown = asyncio.Event()
    reconciler = ScheduledMessageReconciler(
        client,
        conn,
        shutdown,
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    adapter = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    held = adapter.status(float(now))
    assert held is not None and held.selected is not None and held.selected.key.target == "41"
    adapters: dict[DemandKind, DurableDemandAdapter] = {
        kind: adapter if kind is DemandKind.SCHEDULED_DISCOVERY else cast(DurableDemandAdapter, IdleAdapter())
        for kind in DURABLE_DEMAND_ORDER
    }
    coordinator = TelegramDemandCoordinator(
        adapters,
        shutdown,
        clock=lambda: float(now),
        observer=StopOnCompletion(shutdown),
    )
    coordinator._excluded.add(held.selected)

    await coordinator.run()

    assert len(client.requests) == 1
    assert int(cast(int, cast(SimpleNamespace, client.requests[0][0]).peer)) == 42
    assert conn.execute(
        "SELECT message_state FROM scheduled_messages WHERE dialog_id=42 AND message_id=12"
    ).fetchone() == ("scheduled",)
    assert (
        conn.execute(
            "SELECT snapshot_observation_order FROM scheduled_reconciliation_state WHERE dialog_id=42"
        ).fetchone()[0]
        > 0
    )


@pytest.mark.asyncio
async def test_scheduled_selected_stale_dialog_revision_is_rejected_without_rpc(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id, discovery_due_at, updated_at) VALUES (42, ?, ?)",
        (now, now),
    )
    conn.commit()
    client = _ScheduledSnapshotClient()
    adapter = ScheduledDiscoveryDemandAdapter(
        ScheduledMessageReconciler(
            client,
            conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
            writer=inline_writer(conn),
        ),
        account_id=42,
    )
    status = adapter.status(float(now))
    assert status is not None and status.selected is not None
    conn.execute("UPDATE scheduled_reconciliation_state SET dirty_generation=dirty_generation+1 WHERE dialog_id=42")
    conn.commit()

    outcome = await adapter.run_slice(RpcAttemptBudget(limit=4, selected=status.selected))

    assert outcome.kind is SliceOutcomeKind.STALE
    assert client.requests == []


@pytest.mark.parametrize(
    ("adapter_type", "demand_kind"),
    [
        (ScheduledRepairDemandAdapter, DemandKind.SCHEDULED_REPAIR),
        (ScheduledDiscoveryDemandAdapter, DemandKind.SCHEDULED_DISCOVERY),
    ],
)
@pytest.mark.asyncio
async def test_scheduled_selected_negative_channel_dialog_is_accepted(
    conn: sqlite3.Connection,
    adapter_type: type[ScheduledRepairDemandAdapter] | type[ScheduledDiscoveryDemandAdapter],
    demand_kind: DemandKind,
) -> None:
    now = int(time.time())
    dialog_id = -1_000_000_000_042
    with write_transaction(conn):
        upsert_scheduled_message(conn, dialog_id, _message(11, scheduled_at=now + 3600), now=now)
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,updated_at,baseline_observed_at,coverage_state) "
        "VALUES(42,?,?, 'complete')",
        (now, now),
    )
    conn.execute(
        "UPDATE scheduled_reconciliation_state SET repair_due_at=?, discovery_due_at=? WHERE dialog_id=?",
        (now, now, dialog_id),
    )
    if adapter_type is ScheduledDiscoveryDemandAdapter:
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL")
    conn.commit()
    client = _ScheduledSnapshotClient({dialog_id: []})
    adapter = adapter_type(
        ScheduledMessageReconciler(
            client,
            conn,
            asyncio.Event(),
            policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
            writer=inline_writer(conn),
        ),
        account_id=42,
    )
    status = adapter.status(float(now))
    assert status is not None and status.selected is not None
    assert status.selected.key.target == str(dialog_id)
    assert status.selected.key.demand_kind is demand_kind

    outcome = await adapter.run_slice(RpcAttemptBudget(limit=4, selected=status.selected))

    assert outcome.kind is not SliceOutcomeKind.STALE
    assert len(client.requests) == 1


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
    if adapter_type is ScheduledDiscoveryDemandAdapter:
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL")
    conn.commit()
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )

    status = adapter_type(reconciler, account_id=42).status(100)

    assert status is not None
    assert status.release_at == 300
    assert status.freshness_deadline == queue_due_at


def test_scheduled_demand_rejects_a_different_bound_account(conn: sqlite3.Connection) -> None:
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )

    with pytest.raises(RuntimeError, match="different Telegram account"):
        ScheduledDiscoveryDemandAdapter(reconciler, account_id=99).status(time.time())


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
    if adapter_type is ScheduledDiscoveryDemandAdapter:
        conn.execute("UPDATE scheduled_reconciliation_state SET repair_due_at=NULL")
    conn.commit()
    reconciler = ScheduledMessageReconciler(
        _ScheduledSnapshotClient(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )

    status = adapter_type(reconciler, account_id=42).status(100)

    assert status is not None
    assert status.release_at == queue_due_at
    assert status.freshness_deadline == queue_due_at


@pytest.mark.asyncio
async def test_scheduled_repair_adapter_runs_only_repair_rows_with_precise_scope(conn: sqlite3.Connection) -> None:
    conn.commit()
    with write_transaction(conn):
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
        writer=inline_writer(conn),
    )
    adapter = ScheduledRepairDemandAdapter(reconciler, account_id=42)
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    budget = RpcAttemptBudget(limit=16, selected=status.selected)

    conn.commit()
    await adapter.run_slice(budget)

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
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )
    reconciler.bind_account_id(42)

    conn.commit()
    adapter = ScheduledDiscoveryDemandAdapter(reconciler, account_id=42)
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    await adapter.run_slice(RpcAttemptBudget(limit=16, selected=status.selected))

    assert len(client.requests) == 1
    assert client.scopes[0].demand_kind is DemandKind.SCHEDULED_DISCOVERY
    assert client.scopes[0].acquisition_kind is AcquisitionKind.SCHEDULED_MESSAGES_SNAPSHOT
    assert conn.execute("SELECT repair_due_at FROM scheduled_reconciliation_state WHERE dialog_id=42").fetchone() == (
        None,
    )


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
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )

    conn.commit()
    assert await _run_all_demand_slices(worker) == 0
    assert client.requests == []


@pytest.mark.asyncio
async def test_complete_scheduled_baseline_stays_idle_after_days_and_events(conn: sqlite3.Connection) -> None:
    now = int(time.time())
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,repair_due_at,discovery_due_at,updated_at,"
        "baseline_observed_at,coverage_state) VALUES(42,NULL,NULL,?,?, 'complete')",
        (now, now),
    )
    conn.execute("UPDATE scheduled_sync_state SET account_id=42 WHERE key='account'")
    conn.commit()
    client = _ScheduledSnapshotClient()
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=120.0),
        writer=inline_writer(conn),
    )

    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, scheduled_at=now + 60), now=now)
    conn.commit()

    adapter = ScheduledDiscoveryDemandAdapter(worker, account_id=42)
    assert adapter.status(float(now + 5 * 86_400)) is None
    assert await _run_all_demand_slices(worker) == 0
    assert client.requests == []
    assert conn.execute(
        "SELECT dirty_generation,repair_due_at,discovery_due_at,coverage_state "
        "FROM scheduled_reconciliation_state WHERE dialog_id=42"
    ).fetchone() == (1, None, None, "complete")


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
        writer=inline_writer(conn),
    )

    assert [row[0] for row in worker._due_rows(100)] == [42, 41, 43]


@pytest.mark.asyncio
async def test_retained_scheduled_updates_cannot_overwrite_newer_queue_effects(conn: sqlite3.Connection) -> None:
    original = DeliveredUpdate(allocate_observation_order(conn), 100.0, 1)
    with delivered_update_context(original), write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "old"), now=100)
    with write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "new"), now=100)
    before = conn.execute("SELECT dirty_generation FROM scheduled_reconciliation_state").fetchone()
    with delivered_update_context(original), write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "old"), now=100)
        mark_scheduled_messages_removed(conn, 42, [11], now=100)
    assert conn.execute("SELECT text,message_state FROM scheduled_messages").fetchone() == ("new", "scheduled")
    assert conn.execute("SELECT dirty_generation FROM scheduled_reconciliation_state").fetchone() == before
    with write_transaction(conn):
        mark_scheduled_messages_removed(conn, 42, [11], now=100)
    before = conn.execute("SELECT * FROM scheduled_messages").fetchall()
    with delivered_update_context(original), write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "old"), now=100)
    assert conn.execute("SELECT * FROM scheduled_messages").fetchall() == before
    assert conn.execute("SELECT COUNT(*) FROM scheduled_messages_fts").fetchone() == (0,)


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.asyncio
async def test_authoritative_snapshot_fences_retained_create_including_unknown_absent_id(
    conn: sqlite3.Connection, empty: bool
) -> None:
    original = DeliveredUpdate(allocate_observation_order(conn), 100.0, 1)
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,repair_due_at,discovery_due_at,updated_at) "
        "VALUES(42,0,0,0)"
    )
    conn.commit()
    client = _ScheduledSnapshotClient({42: [] if empty else [_message(11, "snapshot")]})
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    await worker.run_demand_slice(DemandKind.SCHEDULED_REPAIR)
    assert len(client.requests) == 1
    assert conn.execute("SELECT snapshot_observation_order FROM scheduled_reconciliation_state").fetchone() == (2,)
    with delivered_update_context(original), write_transaction(conn):
        upsert_scheduled_message(conn, 42, _message(11, "old"), now=100)
        upsert_scheduled_message(conn, 42, _message(12, "absent"), now=100)
        mark_scheduled_messages_removed(conn, 42, [11, 12], now=100)
    assert conn.execute("SELECT message_id,text,message_state FROM scheduled_messages").fetchall() == (
        [] if empty else [(11, "snapshot", "scheduled")]
    )


@pytest.mark.parametrize(
    "response",
    [
        types.messages.MessagesNotModified(count=0),
        SimpleNamespace(messages=[]),
        types.messages.Messages(messages=cast(list[types.TypeMessage], None), topics=[], chats=[], users=[]),
        types.messages.MessagesSlice(count=1, messages=[], topics=[], chats=[], users=[]),
        types.messages.MessagesSlice(count=0, messages=[], topics=[], chats=[], users=[], inexact=True),
    ],
)
@pytest.mark.asyncio
async def test_incomplete_scheduled_response_never_advances_authoritative_absence_floor(
    conn: sqlite3.Connection, response: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = 1_800_000_000
    monkeypatch.setattr("mcp_telegram.scheduled_messages.time.time", lambda: now)
    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES(42,'user',0)")
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,repair_due_at,discovery_due_at,updated_at) "
        "VALUES(42,0,0,0)"
    )
    conn.execute("UPDATE scheduled_sync_state SET last_snapshot_at=77,last_error='prior_failure' WHERE key='account'")
    conn.commit()

    class Client(_ScheduledSnapshotClient):
        async def __call__(self, _request: object, **_kwargs: object) -> object:
            return response

    worker = ScheduledMessageReconciler(
        Client(),
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )
    await worker.run_demand_slice(DemandKind.SCHEDULED_REPAIR)
    assert conn.execute("SELECT snapshot_observation_order FROM scheduled_reconciliation_state").fetchone() == (None,)
    assert conn.execute(
        "SELECT coverage_state,coverage_reason,repair_due_at,discovery_due_at FROM scheduled_reconciliation_state"
    ).fetchone() == ("unknown", "rpc_error", 0, 0)
    for adapter_type in (ScheduledRepairDemandAdapter, ScheduledDiscoveryDemandAdapter):
        status = adapter_type(
            ScheduledMessageReconciler(
                Client(),
                conn,
                asyncio.Event(),
                policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
                writer=inline_writer(conn),
            ),
            account_id=42,
        ).status(now)
        if adapter_type is ScheduledRepairDemandAdapter:
            assert status is not None and status.release_at == 0
        else:
            assert status is None

    assert conn.execute(
        "SELECT last_snapshot_at,last_error FROM scheduled_sync_state WHERE key='account'"
    ).fetchone() == (77, "prior_failure")


@pytest.mark.asyncio
async def test_access_loss_marks_scheduled_coverage_inaccessible(conn: sqlite3.Connection) -> None:
    dialog_id = -1_000_000_000_042
    conn.execute("INSERT INTO dialogs(dialog_id,type,hidden) VALUES(?,'channel',0)", (dialog_id,))
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,repair_due_at,discovery_due_at,updated_at) "
        "VALUES(?,NULL,0,0)",
        (dialog_id,),
    )
    conn.commit()
    client = _ScheduledSnapshotClient(call_error=ChannelPrivateError(request=None))
    worker = ScheduledMessageReconciler(
        client,
        conn,
        asyncio.Event(),
        policy=ScheduledReconciliationPolicy(activity_rpc_timeout_seconds=10),
        writer=inline_writer(conn),
    )

    await worker.run_demand_slice(DemandKind.SCHEDULED_DISCOVERY)

    assert conn.execute(
        "SELECT coverage_state,coverage_reason,repair_due_at,discovery_due_at "
        "FROM scheduled_reconciliation_state WHERE dialog_id=?",
        (dialog_id,),
    ).fetchone() == ("inaccessible", "ChannelPrivateError", None, None)
    assert conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone() == (
        "access_lost",
    )
