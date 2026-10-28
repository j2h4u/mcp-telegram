"""Bounded topic acquisition owns RPC attempts, retries and enrollment checks."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.tl.functions.messages import GetForumTopicsRequest
from telethon.tl.types import (
    ChannelForbidden,
    ChatForbidden,
    ForumTopic,
    InputPeerUser,
    PeerNotifySettings,
    PeerUser,
    User,
    UserEmpty,
)

from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import current_rpc_scope
from mcp_telegram.topics.catalog import TopicCatalogRepository
from mcp_telegram.topics.demand import TopicSnapshotDemandAdapter
from mcp_telegram.topics.telegram_adapter import TelethonTelegramTopicGateway
from tests.sqlite_writer_helpers import inline_writer


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "topics.sqlite"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (42,1,'explicit',100)"
        )
        conn.commit()
        yield conn


def _topic(topic_id: int) -> ForumTopic:
    return ForumTopic(
        id=topic_id,
        date=datetime.fromtimestamp(100 + topic_id, UTC),
        peer=PeerUser(42),
        title=f"Topic {topic_id}",
        icon_color=6,
        top_message=topic_id * 10,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=PeerUser(42),
        notify_settings=PeerNotifySettings(),
        icon_emoji_id=77,
    )


class _CountingClient:
    """Debit each simulated scalar transport attempt, including cold peer resolution."""

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.page_offsets: list[int] = []
        self.entity: object = User(id=42, bot=True, bot_forum_view=True)
        self.failure: BaseException | None = None

    def _attempt(self, label: str) -> None:
        scope = current_rpc_scope()
        assert scope.demand_kind is DemandKind.TOPIC_SNAPSHOT
        assert scope.acquisition_kind is AcquisitionKind.TOPIC_SNAPSHOT
        assert scope.attempt_budget is not None
        scope.attempt_budget.debit()
        self.attempts.append(label)

    async def get_entity(self, entity: object) -> object:
        assert entity == 42
        self._attempt("cold_identity_lookup")
        self._attempt("entity_lookup")
        return self.entity

    async def get_input_entity(self, entity: object) -> object:
        assert entity is self.entity
        self._attempt("input_peer_lookup")
        return InputPeerUser(42, 0)

    async def __call__(self, request: object) -> object:
        assert isinstance(request, GetForumTopicsRequest), "optional icon RPC is not part of catalog acquisition"
        self._attempt("topic_page")
        self.page_offsets.append(request.offset_topic)
        if self.failure is not None:
            raise self.failure
        topic_id = request.offset_topic + 1
        return SimpleNamespace(topics=[_topic(topic_id)], count=2, messages=[], order_by_create_date=True)


def _adapter(conn: sqlite3.Connection, client: _CountingClient) -> TopicSnapshotDemandAdapter:
    return TopicSnapshotDemandAdapter(
        TelethonTelegramTopicGateway(client), conn, writer=inline_writer(conn), account_id=1
    )


def _selected_budget(adapter: TopicSnapshotDemandAdapter, limit: int) -> RpcAttemptBudget:
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    return RpcAttemptBudget(limit=limit, selected=status.selected)


async def _run_selected(adapter: TopicSnapshotDemandAdapter, limit: int) -> object:
    return await adapter.run_slice(_selected_budget(adapter, limit))


@pytest.mark.asyncio
async def test_all_nested_rpc_attempts_count_and_successive_slices_advance(db: sqlite3.Connection) -> None:
    client = _CountingClient()
    adapter = _adapter(db, client)
    first = _selected_budget(adapter, 4)

    await adapter.run_slice(first)

    assert first.attempts == 4
    assert client.page_offsets == [0]
    assert TopicCatalogRepository(db).coverage(42)["coverage"] == "partial"
    second_adapter = _adapter(db, client)
    second = _selected_budget(second_adapter, 4)
    # Recreate the owner to show that page position is not process-local memory.
    await second_adapter.run_slice(second)

    assert second.attempts == 4
    assert client.page_offsets == [0, 1]
    assert TopicCatalogRepository(db).coverage(42)["coverage"] == "complete"
    assert len(client.attempts) == 8


@pytest.mark.asyncio
async def test_selected_topic_sibling_progresses_and_stale_revision_is_rejected(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (43,1,'explicit',100)")
    db.commit()

    class SiblingClient(_CountingClient):
        async def get_entity(self, entity: object) -> object:
            dialog_id = int(cast(int, entity))
            self._attempt("entity_lookup")
            return User(id=dialog_id, bot=True, bot_forum_view=True)

        async def get_input_entity(self, entity: object) -> object:
            self._attempt("input_peer_lookup")
            return InputPeerUser(int(cast(User, entity).id), 0)

    client = SiblingClient()
    adapter = _adapter(db, client)
    blocked = adapter.status(time.time())
    assert blocked is not None and blocked.selected is not None
    selected = adapter.status(time.time(), excluded=frozenset({blocked.selected}))
    assert selected is not None and selected.selected is not None
    assert selected.selected.key.target == "43"

    outcome = await adapter.run_slice(RpcAttemptBudget(limit=4, selected=selected.selected))
    assert outcome.kind is SliceOutcomeKind.PROGRESSED
    assert client.page_offsets == [0]
    stale = await adapter.run_slice(RpcAttemptBudget(limit=4, selected=selected.selected))
    assert stale.kind is SliceOutcomeKind.STALE
    assert client.page_offsets == [0]


@pytest.mark.asyncio
async def test_budget_exhaustion_prevents_extra_send_and_retains_pending(db: sqlite3.Connection) -> None:
    client = _CountingClient()
    budget = _selected_budget(_adapter(db, client), 3)

    await _adapter(db, client).run_slice(budget)

    assert budget.attempts == 3
    assert client.page_offsets == []
    coverage = TopicCatalogRepository(db).coverage(42)
    assert coverage["pending"] is True
    assert coverage["completed_at"] is None
    assert coverage["reason"] == "bootstrap"


@pytest.mark.asyncio
async def test_flood_wait_defers_without_erasing_cursor_or_readable_facts(
    db: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1000.0
    monkeypatch.setattr("mcp_telegram.topics.demand.time.time", lambda: now)
    client = _CountingClient()
    adapter = _adapter(db, client)
    await _run_selected(adapter, 4)
    client.failure = TelegramRpcThrottled(retry_after_seconds=30)

    with pytest.raises(TelegramRpcThrottled):
        await _run_selected(adapter, 4)

    coverage = TopicCatalogRepository(db).coverage(42)
    assert coverage["coverage"] == "partial"
    assert coverage["retry_at"] is None
    client.failure = None
    now = 1031.0
    await _run_selected(_adapter(db, client), 4)

    assert client.page_offsets == [0, 1, 1]
    assert TopicCatalogRepository(db).coverage(42)["coverage"] == "complete"


@pytest.mark.asyncio
async def test_disabled_enrollment_never_acquires(db: sqlite3.Connection) -> None:
    db.execute("UPDATE full_history_enrollment SET enabled=0")
    db.commit()
    client = _CountingClient()

    assert _adapter(db, client).status(time.time()) is None

    assert client.attempts == []
    assert TopicCatalogRepository(db).coverage(42)["coverage"] == "not_observed"


@pytest.mark.asyncio
async def test_observed_complete_normal_user_is_not_applicable_without_rpc(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO dialogs(dialog_id,name,type,identity_complete,identity_observed_at,identity_source) "
        "VALUES (42,'Known person','user',1,100,'directory')"
    )
    db.commit()
    client = _CountingClient()

    await _run_selected(_adapter(db, client), 4)

    assert client.attempts == []
    coverage = TopicCatalogRepository(db).coverage(42)
    assert coverage["coverage"] == "not_applicable"
    assert coverage["completed_at"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "complete", "source"),
    [("bot", 1, "directory"), ("unknown", 1, "directory"), ("user", 0, "directory"), ("user", 1, "legacy")],
)
async def test_missing_capability_in_uncertain_identity_does_not_imply_not_applicable(
    db: sqlite3.Connection,
    kind: str,
    complete: int,
    source: str,
) -> None:
    db.execute(
        "INSERT INTO dialogs(dialog_id,name,type,identity_complete,identity_observed_at,identity_source) "
        "VALUES (42,'Uncertain identity',?,?,100,?)",
        (kind, complete, source),
    )
    db.commit()
    client = _CountingClient()
    client.entity = User(id=42, bot=True, bot_forum_view=True)

    await _run_selected(_adapter(db, client), 4)

    assert client.page_offsets == [0]
    assert TopicCatalogRepository(db).coverage(42)["coverage"] == "partial"


@pytest.mark.asyncio
async def test_actual_non_topic_capable_entity_does_not_publish_empty_catalog(db: sqlite3.Connection) -> None:
    client = _CountingClient()
    client.entity = User(id=42, bot=True, bot_forum_view=False)
    budget = _selected_budget(_adapter(db, client), 4)

    await _adapter(db, client).run_slice(budget)

    assert budget.attempts == 2
    assert client.page_offsets == []
    coverage = TopicCatalogRepository(db).coverage(42)
    assert coverage["coverage"] == "not_applicable"
    assert coverage["completed_at"] is None


@pytest.mark.asyncio
async def test_latched_protection_escapes_to_shared_coordinator_without_retry_deadline(db: sqlite3.Connection) -> None:
    client = _CountingClient()
    client.failure = TelegramRpcThrottled(latched=True)

    with pytest.raises(TelegramRpcThrottled) as caught:
        await _run_selected(_adapter(db, client), 4)

    assert caught.value.latched
    coverage = TopicCatalogRepository(db).coverage(42)
    assert coverage["retry_at"] is None
    assert coverage["completed_at"] is None
    assert coverage["pending"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["min_user", "empty_user", "unknown"])
async def test_missing_capability_variants_preserve_observations_and_pending_work(
    db: sqlite3.Connection,
    variant: str,
) -> None:
    client = _CountingClient()
    adapter = _adapter(db, client)
    await _run_selected(adapter, 4)
    if variant == "min_user":
        client.entity = User(id=42, min=True)
    elif variant == "empty_user":
        client.entity = UserEmpty(id=42)
    else:
        client.entity = SimpleNamespace(id=42)

    await _run_selected(adapter, 4)

    coverage = TopicCatalogRepository(db).coverage(42)
    assert coverage["coverage"] == "partial"
    assert coverage["pending"] is True
    assert coverage["reason"] == "capability_not_observed"
    assert coverage["retry_at"] is None
    assert coverage["completed_at"] is None
    assert client.page_offsets == [0]


@pytest.mark.asyncio
@pytest.mark.parametrize("channel", [False, True])
async def test_forbidden_access_blocks_without_retry_and_retains_resumable_page(
    db: sqlite3.Connection,
    channel: bool,
) -> None:
    client = _CountingClient()
    adapter = _adapter(db, client)
    await _run_selected(adapter, 4)
    client.entity = (
        ChannelForbidden(id=42, access_hash=0, title="Unavailable")
        if channel
        else ChatForbidden(id=42, title="Unavailable")
    )
    position_sql = "SELECT generation,offset_date,offset_id,offset_topic FROM topic_catalog_state WHERE dialog_id=42"
    cursor = cast(tuple[object, ...], db.execute(position_sql).fetchone())

    await _run_selected(adapter, 4)

    repository = TopicCatalogRepository(db)
    assert repository.coverage(42) == {
        "coverage": "partial",
        "completed_at": None,
        "pending": False,
        "reason": "source_access_unavailable",
        "retry_at": None,
    }
    assert db.execute(position_sql).fetchone() == cursor
    assert db.execute("SELECT topic_id,title FROM topic_metadata").fetchall() == [(1, "Topic 1")]
    assert adapter.status(10**12) is None
    attempts = list(client.attempts)
    repository.bootstrap()
    assert repository.next_pending() is None
    assert client.attempts == attempts
    db.commit()
    path = cast(str, db.execute("PRAGMA database_list").fetchone()[2])
    with closing(sqlite3.connect(path)) as reopened:
        restarted = TopicCatalogRepository(reopened)
        restarted.bootstrap()
        assert restarted.next_pending() is None
        assert restarted.coverage(42) == repository.coverage(42)

    client.entity = User(id=42, bot=True, bot_forum_view=True)
    repository.request(42, "explicit_enable")
    db.commit()
    await _run_selected(_adapter(db, client), 4)

    assert client.page_offsets == [0, 1]
    assert repository.coverage(42)["reason"] != "source_access_unavailable"
