"""Focused Slice 2 aggregate ordering and detail lifecycle contracts."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon.errors import (  # type: ignore[import-untyped]
    ChannelPrivateError,
    ChatAdminRequiredError,
    MsgIdInvalidError,
    PeerIdInvalidError,
)

from mcp_telegram.daemon_message import cached_reaction_freshness, project_cached_message_facts
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.message_fact_refresh import (
    _NEXT_REACTION_RELEASE_SQL,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    _next_release_at,
    _reaction_candidates,
    refresh_message_facts_once,
)
from mcp_telegram.models import ReadMessage
from mcp_telegram.reactions.contracts import (
    ReactionAggregate,
    ReactionDetailFetchResult,
    ReactionDetailPage,
    ReactionEvent,
)
from mcp_telegram.reactions.detail import ReactionDetailRefresher, ReactionDetailResult
from mcp_telegram.reactions.persistence import allocate_observation_boundary, apply_aggregate_observation
from mcp_telegram.reactions.telegram_adapter import TelethonTelegramReactionGateway
from mcp_telegram.sync_db import _apply_migration_68, _apply_migration_69, ensure_sync_schema
from mcp_telegram.telegram_fact_queries import reaction_event_projection
from mcp_telegram.telegram_reading import GatewayFailure, GatewayFailureKind, TelegramReadReceiptGateway
from mcp_telegram.telegram_rpc_consumers import TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, rpc_scope


def _db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO synced_dialogs(dialog_id,status) VALUES (1,'synced')")
    conn.execute("INSERT INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (1,1,'explicit',1)")
    conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,2,1)")
    conn.commit()
    return conn


def test_aggregate_ordering_accepts_empty_and_rejects_older(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 2)], source="history", observed_at=10, observation_sequence=1
    )
    assert apply_aggregate_observation(conn, 1, 2, [], source="raw_update", observed_at=10, observation_sequence=2)
    assert reaction_event_projection(conn, 1, [2]) == ({}, {2: "complete"})
    assert not apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("🔥", 1)], source="history", observed_at=9, observation_sequence=3
    )
    assert conn.execute("SELECT COUNT(*) FROM message_reactions").fetchone() == (0,)
    assert conn.execute("SELECT generation,source FROM message_reaction_aggregate_state").fetchone() == (
        2,
        "raw_update",
    )
    assert conn.execute(
        "SELECT aggregate_generation, detail_generation FROM message_reaction_event_status"
    ).fetchone() == (
        2,
        2,
    )
    conn.close()


def test_detail_partial_offset_survives_restart_and_publishes(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    # A migrated aggregate may predate its detail receipt. The first page
    # acquisition must recreate that stale receipt transactionally.
    conn.execute("DELETE FROM message_reaction_event_status WHERE dialog_id=1 AND message_id=2")
    conn.commit()

    class Gateway:
        def __init__(self) -> None:
            self.offsets: list[str | None] = []

        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id
            del limit
            self.offsets.append(offset)
            events = (ReactionEvent(7, "👍", None),)
            return ReactionDetailFetchResult(page=ReactionDetailPage(events, "next" if offset is None else None))

    gateway = Gateway()
    refresher = ReactionDetailRefresher(conn, gateway, now=lambda: 20)
    first = asyncio.run(refresher.refresh_one(1, 2, 1, entity=SimpleNamespace(), now=20))
    assert first.status == "partial"
    assert conn.execute("SELECT next_offset FROM message_reaction_event_status").fetchone() == ("next",)
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events WHERE display_generation=0").fetchone() == (1,)
    assert reaction_event_projection(conn, 1, [2]) == ({}, {2: "partial"})

    second = asyncio.run(refresher.refresh_one(1, 2, 1, entity=SimpleNamespace(), offset="next", now=21))
    assert second.status == "complete"
    assert gateway.offsets == [None, "next"]
    assert conn.execute("SELECT status,display_generation FROM message_reaction_event_status").fetchone() == (
        "complete",
        1,
    )
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events WHERE display_generation=1").fetchone() == (2,)
    conn.close()


def test_detail_persistence_requires_an_idle_dedicated_connection(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.commit()
    conn.execute("UPDATE message_reaction_event_status SET checked_at=2 WHERE dialog_id=1 AND message_id=2")

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            return ReactionDetailFetchResult(page=ReactionDetailPage((ReactionEvent(7, "👍", None),), None))

    with pytest.raises(RuntimeError, match="idle dedicated connection"):
        asyncio.run(ReactionDetailRefresher(conn, Gateway()).refresh_one(1, 2, 1, entity=1))
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events").fetchone() == (0,)
    conn.rollback()
    conn.close()


def test_detail_gateway_uses_one_page_and_never_get_messages() -> None:
    class Client:
        def __init__(self) -> None:
            self.pages = 0
            self.resolved: list[object] = []

        async def get_messages(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("detail acquisition must not call get_messages")

        async def get_input_entity(self, entity: object) -> object:
            self.resolved.append(entity)
            return SimpleNamespace(peer_id=entity)

        async def __call__(self, request: object) -> object:
            self.pages += 1
            assert request.__class__.__name__ == "GetMessageReactionsListRequest"
            return SimpleNamespace(reactions=(), next_offset=None)

    client = Client()
    result = asyncio.run(TelethonTelegramReactionGateway(client).fetch_reaction_page(42, 2, offset=None, limit=100))
    assert result.ok
    assert client.pages == 1
    assert client.resolved == [42]


@pytest.mark.parametrize(
    ("error", "kind", "retry_after"),
    [
        (ValueError("private chat"), GatewayFailureKind.INVALID_TARGET, None),
        (ChannelPrivateError(request=None), GatewayFailureKind.ACCESS_LOST, None),
        (TelegramRpcThrottled(retry_after_seconds=17), GatewayFailureKind.FLOOD_WAIT, 17),
    ],
)
def test_detail_gateway_translates_private_and_flood_failures(
    error: Exception, kind: GatewayFailureKind, retry_after: int | None
) -> None:
    class Client:
        async def get_input_entity(self, entity: object) -> object:
            del entity
            raise error

        async def __call__(self, request: object) -> object:
            del request
            raise AssertionError("request must not be sent after entity failure")

    result = asyncio.run(TelethonTelegramReactionGateway(Client()).fetch_reaction_page(42, 2, offset=None, limit=100))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind == kind
    assert result.failure.retry_after == retry_after


@pytest.mark.parametrize(
    ("error_type", "kind"),
    [
        (MsgIdInvalidError, GatewayFailureKind.INVALID_TARGET),
        (PeerIdInvalidError, GatewayFailureKind.INVALID_TARGET),
        (ChatAdminRequiredError, GatewayFailureKind.INVALID_TARGET),
    ],
)
def test_detail_gateway_classifies_known_permanent_rpc_symbols(
    error_type: type[Exception], kind: GatewayFailureKind
) -> None:
    class Client:
        async def get_input_entity(self, entity: object) -> object:
            del entity
            raise error_type(None)

        async def __call__(self, request: object) -> object:
            del request
            raise AssertionError("request must not be sent after permanent entity failure")

    result = asyncio.run(TelethonTelegramReactionGateway(Client()).fetch_reaction_page(42, 2, offset=None, limit=100))
    assert not result.ok
    assert result.failure is not None
    assert result.failure.kind is kind
    assert result.failure.retryable is False


def test_detail_gateway_propagates_rpc_admission_closed() -> None:
    with rpc_scope(TelegramRpcSource.MESSAGE_FACT_REFRESH) as scope:
        closed = RpcAdmissionClosedError(scope, "scheduler closed")

    class Client:
        async def get_input_entity(self, entity: object) -> object:
            del entity
            raise closed

        async def __call__(self, request: object) -> object:
            del request
            raise AssertionError("request must not be sent after admission close")

    with pytest.raises(RpcAdmissionClosedError, match="scheduler closed"):
        asyncio.run(TelethonTelegramReactionGateway(Client()).fetch_reaction_page(42, 2, offset=None, limit=100))


def test_real_refresher_exposes_flood_wait_cycle_stop(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.commit()

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            return ReactionDetailFetchResult(
                failure=GatewayFailure(GatewayFailureKind.FLOOD_WAIT, "Flood", "wait", False, retry_after=17)
            )

    result = asyncio.run(ReactionDetailRefresher(conn, Gateway()).refresh_one(1, 2, 1, entity=1, now=10))
    assert result.status == "unavailable"
    assert result.failure_kind == GatewayFailureKind.FLOOD_WAIT.value
    assert result.retry_after == 17
    assert result.stop_cycle
    assert conn.execute("SELECT next_attempt_at FROM message_reaction_event_status").fetchone() == (27,)
    conn.close()


@pytest.mark.parametrize("kind", list(GatewayFailureKind))
def test_only_explicit_terminal_failure_kinds_are_terminal(kind: GatewayFailureKind) -> None:
    failure = GatewayFailure(kind, "synthetic", "synthetic", retryable=False)
    terminal = ReactionDetailRefresher._is_terminal_failure(failure)
    assert terminal is (kind in (GatewayFailureKind.INVALID_TARGET, GatewayFailureKind.ACCESS_LOST))


def test_identical_aggregate_advances_boundary_without_invalidating_detail(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=10, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='complete', detail_generation=1, display_generation=1, "
        "published_generation=1 WHERE dialog_id=1 AND message_id=2"
    )
    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=11, observation_sequence=2
    )
    assert conn.execute(
        "SELECT generation, observed_at, observation_sequence, status, detail_generation, display_generation "
        "FROM message_reaction_aggregate_state JOIN message_reaction_event_status USING (dialog_id, message_id)"
    ).fetchone() == (1, 11, 2, "complete", 1, 1)
    conn.close()


def test_identical_empty_aggregate_keeps_current_detail_generation(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=10, observation_sequence=1
    )
    assert apply_aggregate_observation(conn, 1, 2, [], source="raw_update", observed_at=11, observation_sequence=2)
    assert conn.execute(
        "SELECT generation, detail_generation, status FROM message_reaction_aggregate_state "
        "JOIN message_reaction_event_status USING (dialog_id, message_id)"
    ).fetchone() == (2, 2, "complete")
    assert apply_aggregate_observation(conn, 1, 2, [], source="raw_update", observed_at=12, observation_sequence=3)
    assert conn.execute("SELECT generation, observation_sequence FROM message_reaction_aggregate_state").fetchone() == (
        2,
        3,
    )
    assert conn.execute(
        "SELECT aggregate_generation, detail_generation, display_generation, published_generation, status "
        "FROM message_reaction_event_status"
    ).fetchone() == (2, 2, 2, 2, "complete")
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events").fetchone() == (0,)
    conn.close()


def test_observation_counter_is_durable_and_monotonic(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("UPDATE message_reaction_observation_counter SET next_sequence=40 WHERE singleton=1")
    first = allocate_observation_boundary(conn, "history", observed_at=1)
    second = allocate_observation_boundary(conn, "raw_update", observed_at=1)
    assert (first.sequence, second.sequence) == (41, 42)
    conn.close()


def test_cached_read_projection_is_local_and_reports_cached_only(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    message = ReadMessage(message_id=2, sent_at=1, dialog_id=1)

    projected = project_cached_message_facts(conn, 1, [message])

    assert projected[0].reaction_events == ()
    assert projected[0].reaction_events_status == "stale"
    assert cached_reaction_freshness(1).status == "cached_only"
    conn.close()


def test_refresh_cycle_passes_generation_and_offset_and_limits_detail_pages(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,3,2)")
    conn.execute("UPDATE messages SET sent_at=3 WHERE dialog_id=1 AND message_id=2")
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    apply_aggregate_observation(
        conn, 1, 3, [ReactionAggregate("🔥", 1)], source="history", observed_at=2, observation_sequence=2
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='partial', next_offset='resume', next_attempt_at=0 "
        "WHERE dialog_id=1 AND message_id=2"
    )

    class Refresher:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int, int, str | None]] = []

        async def refresh_one(  # noqa: PLR0913
            self,
            dialog_id: int,
            message_id: int,
            generation: int,
            *,
            entity: object,
            offset: str | None,
            cancellation_event: asyncio.Event | None,
            now: int,
        ) -> ReactionDetailResult:
            del entity, cancellation_event, now
            self.calls.append((dialog_id, message_id, generation, offset))
            return ReactionDetailResult("partial", fetched_pages=1)

    refresher = Refresher()
    result = asyncio.run(
        refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn, cast(ReactionDetailRefresher, refresher), cast(TelegramReadReceiptGateway, object())
            ),
            MessageFactRefreshPolicy(
                reaction_max_messages_per_cycle=10,
                read_at_max_messages_per_cycle=0,
                pause_seconds=0,
                read_at_ttl_seconds=600,
                reaction_detail_max_pages_per_cycle=1,
            ),
            now=10,
        )
    )
    assert refresher.calls == [(1, 2, 1, "resume")]
    assert result.reaction_refreshed == 1
    conn.close()


def test_refresh_cycle_stops_reaction_probes_after_flood_wait(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,3,2)")
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    apply_aggregate_observation(
        conn, 1, 3, [ReactionAggregate("🔥", 1)], source="history", observed_at=2, observation_sequence=2
    )
    conn.commit()

    class Refresher:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def refresh_one(  # noqa: PLR0913
            self,
            dialog_id: int,
            message_id: int,
            generation: int,
            *,
            entity: object,
            offset: str | None,
            cancellation_event: asyncio.Event | None,
            now: int,
        ) -> ReactionDetailResult:
            del dialog_id, generation, entity, offset, cancellation_event, now
            self.calls.append(message_id)
            return ReactionDetailResult("unavailable", failure_kind="flood_wait", retry_after=17)

    refresher = Refresher()
    result = asyncio.run(
        refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn, cast(ReactionDetailRefresher, refresher), cast(TelegramReadReceiptGateway, object())
            ),
            MessageFactRefreshPolicy(
                reaction_max_messages_per_cycle=10,
                read_at_max_messages_per_cycle=0,
                pause_seconds=0,
                read_at_ttl_seconds=600,
                reaction_detail_max_pages_per_cycle=5,
            ),
            now=10,
        )
    )
    assert refresher.calls == [2]
    assert result.reaction_refreshed == 0
    conn.close()


def test_reaction_migration_preserves_empty_receipts_and_cleans_orphans() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER, applied_at INTEGER);
        CREATE TABLE messages(dialog_id INTEGER, message_id INTEGER, PRIMARY KEY(dialog_id, message_id));
        CREATE TABLE message_reactions_freshness(
            dialog_id INTEGER, message_id INTEGER, checked_at INTEGER,
            PRIMARY KEY(dialog_id, message_id)
        );
        CREATE TABLE message_reactions(
            dialog_id INTEGER, message_id INTEGER, emoji TEXT, count INTEGER,
            PRIMARY KEY(dialog_id, message_id, emoji)
        );
        CREATE TABLE message_reaction_events(
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            dialog_id INTEGER, message_id INTEGER, reactor_id INTEGER,
            emoji TEXT, reacted_at INTEGER, fetched_at INTEGER NOT NULL
        );
        CREATE TABLE message_reaction_event_status(
            dialog_id INTEGER, message_id INTEGER, checked_at INTEGER NOT NULL,
            status TEXT NOT NULL, returned_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(dialog_id, message_id)
        );
        INSERT INTO messages VALUES (1, 2), (1, 3), (1, 4), (1, 5);
        INSERT INTO message_reactions_freshness VALUES (1, 2, 100);
        INSERT INTO message_reaction_event_status VALUES (1, 2, 101, 'complete', 0);
        INSERT INTO message_reaction_event_status VALUES (1, 3, 102, 'unavailable', 1);
        INSERT INTO message_reaction_event_status VALUES (1, 4, 104, 'partial', 1);
        INSERT INTO message_reaction_event_status VALUES (1, 99, 103, 'complete', 1);
        INSERT INTO message_reaction_events(dialog_id, message_id, emoji, fetched_at)
            VALUES (1, 3, '👍', 102), (1, 4, '👋', 104), (1, 5, '🔥', 105), (1, 99, '🔥', 103);
        """
    )

    _apply_migration_68(conn, 67)

    assert conn.execute(
        "SELECT dialog_id, message_id, aggregate_row_count FROM message_reaction_aggregate_state ORDER BY message_id"
    ).fetchall() == [(1, 2, 0), (1, 3, 0), (1, 4, 0), (1, 5, 0)]
    assert conn.execute(
        "SELECT message_id, status, display_generation FROM message_reaction_event_status ORDER BY message_id"
    ).fetchall() == [
        (2, "complete", 1),
        (3, "unavailable", 1),
        (4, "partial", 1),
        (5, "stale", 1),
    ]
    assert conn.execute("SELECT message_id FROM message_reaction_events ORDER BY message_id").fetchall() == [
        (3,),
        (4,),
        (5,),
    ]
    assert conn.execute("SELECT next_sequence FROM message_reaction_observation_counter").fetchone() == (5,)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='message_reactions_freshness'").fetchone() is None
    conn.close()


def test_pacing_migration_preserves_reaction_candidate_and_detail_state() -> None:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE schema_version(version INTEGER, applied_at INTEGER);
        INSERT INTO schema_version VALUES (68, 1);
        CREATE TABLE message_reaction_aggregate_state(
            dialog_id INTEGER, message_id INTEGER, generation INTEGER,
            aggregate_row_count INTEGER, PRIMARY KEY(dialog_id, message_id)
        );
        CREATE TABLE message_reaction_event_status(
            dialog_id INTEGER, message_id INTEGER, status TEXT,
            next_offset TEXT, PRIMARY KEY(dialog_id, message_id)
        );
        CREATE TABLE message_reaction_events(
            event_id INTEGER PRIMARY KEY, dialog_id INTEGER, message_id INTEGER, emoji TEXT
        );
        INSERT INTO message_reaction_aggregate_state VALUES (1, 2, 7, 1);
        INSERT INTO message_reaction_event_status VALUES (1, 2, 'partial', 'resume');
        INSERT INTO message_reaction_events VALUES (9, 1, 2, '👍');
        """
    )

    assert _apply_migration_69(conn, 68) == 69
    assert conn.execute("SELECT generation, aggregate_row_count FROM message_reaction_aggregate_state").fetchone() == (
        7,
        1,
    )
    assert conn.execute("SELECT status, next_offset FROM message_reaction_event_status").fetchone() == (
        "partial",
        "resume",
    )
    assert conn.execute("SELECT event_id, emoji FROM message_reaction_events").fetchone() == (9, "👍")
    assert conn.execute(
        "SELECT window_started_at, release_at, claimed_pages, started_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (0, 0, 0, 0)
    conn.close()


def test_detail_replaces_migrated_display_rows_on_first_complete_page(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='stale', display_generation=1, detail_generation=0 "
        "WHERE dialog_id=1 AND message_id=2"
    )
    conn.execute(
        "INSERT INTO message_reaction_events(dialog_id,message_id,reactor_id,emoji,fetched_at,detail_generation,page_ordinal,display_generation) "
        "VALUES (1,2,8,'legacy',2,0,0,1)"
    )
    conn.commit()

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            return ReactionDetailFetchResult(page=ReactionDetailPage((ReactionEvent(9, "👍", None),), None))

    result = asyncio.run(ReactionDetailRefresher(conn, Gateway(), now=lambda: 3).refresh_one(1, 2, 1, entity=1, now=3))
    assert result.status == "complete"
    assert conn.execute(
        "SELECT emoji, detail_generation, display_generation FROM message_reaction_events"
    ).fetchall() == [("👍", 1, 1)]
    conn.close()


def test_detail_stale_writer_failure_and_non_advancing_offset_preserve_display(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='complete', detail_generation=1, display_generation=1, "
        "published_generation=1 WHERE dialog_id=1 AND message_id=2"
    )
    conn.execute(
        "INSERT INTO message_reaction_events(dialog_id,message_id,reactor_id,emoji,fetched_at,detail_generation,page_ordinal,display_generation) "
        "VALUES (1,2,8,'legacy',2,1,0,1)"
    )
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("🔥", 2)], source="raw_update", observed_at=2, observation_sequence=2
    )
    conn.commit()

    class FailingGateway:
        calls = 0

        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            self.calls += 1
            return ReactionDetailFetchResult(
                failure=GatewayFailure(GatewayFailureKind.FLOOD_WAIT, "Flood", "wait", True, retry_after=17)
            )

    gateway = FailingGateway()
    refresher = ReactionDetailRefresher(conn, gateway, now=lambda: 10)
    assert asyncio.run(refresher.refresh_one(1, 2, 99, entity=1, now=10)).status == "stale_writer"
    assert gateway.calls == 0
    result = asyncio.run(refresher.refresh_one(1, 2, 2, entity=1, now=10))
    assert result.status == "unavailable"
    assert conn.execute("SELECT status, next_attempt_at FROM message_reaction_event_status").fetchone() == (
        "unavailable",
        27,
    )
    assert conn.execute("SELECT emoji FROM message_reaction_events WHERE display_generation=1").fetchone() == (
        "legacy",
    )

    conn.execute(
        "UPDATE message_reaction_event_status SET status='partial', next_offset='same', staged_count=1, "
        "aggregate_generation=2 WHERE dialog_id=1 AND message_id=2"
    )
    conn.commit()

    class RepeatingGateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, limit
            return ReactionDetailFetchResult(page=ReactionDetailPage((), offset))

    result = asyncio.run(
        ReactionDetailRefresher(conn, RepeatingGateway(), now=lambda: 11).refresh_one(
            1, 2, 2, entity=1, offset="same", now=11
        )
    )
    assert result.status == "partial"
    assert conn.execute("SELECT failure_kind FROM message_reaction_event_status").fetchone() == (
        "non_advancing_offset",
    )
    conn.close()


@pytest.mark.parametrize("failure_kind", [GatewayFailureKind.INVALID_TARGET, GatewayFailureKind.ACCESS_LOST])
def test_permanent_detail_failure_is_terminal_until_new_aggregate(
    tmp_path: Path, failure_kind: GatewayFailureKind
) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='complete', detail_generation=1, display_generation=1, "
        "published_generation=1 WHERE dialog_id=1 AND message_id=2"
    )
    conn.execute(
        "INSERT INTO message_reaction_events(dialog_id,message_id,reactor_id,emoji,fetched_at,detail_generation,page_ordinal,display_generation) "
        "VALUES (1,2,8,'legacy',2,1,0,1)"
    )
    conn.commit()
    observations: list[tuple[str, str]] = []

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            return ReactionDetailFetchResult(failure=GatewayFailure(failure_kind, "Permanent", "unavailable", False))

    result = asyncio.run(
        ReactionDetailRefresher(
            conn, Gateway(), observation_sink=lambda kind, outcome: observations.append((kind, outcome))
        ).refresh_one(1, 2, 1, entity=1, now=10)
    )
    assert result.status == "unavailable"
    assert result.failure_kind == failure_kind.value
    assert result.retry_after is None
    assert observations[-1] == ("reaction.detail", "terminal_unavailable")
    assert conn.execute(
        "SELECT status, next_attempt_at, failure_kind, display_generation, published_generation "
        "FROM message_reaction_event_status"
    ).fetchone() == ("unavailable", None, failure_kind.value, 1, 1)
    assert conn.execute("SELECT emoji FROM message_reaction_events WHERE display_generation=1").fetchone() == (
        "legacy",
    )
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == []
    assert _next_release_at(conn, _NEXT_REACTION_RELEASE_SQL, 0) is None

    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("🔥", 2)], source="raw_update", observed_at=2, observation_sequence=2
    )
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == [(1, 2, 2, None)]
    conn.close()


def test_terminal_failure_clears_partial_staging_without_erasing_display(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='partial', detail_generation=1, display_generation=1, "
        "published_generation=1, next_offset='resume', staged_count=1 WHERE dialog_id=1 AND message_id=2"
    )
    conn.execute(
        "INSERT INTO message_reaction_events(dialog_id,message_id,reactor_id,emoji,fetched_at,detail_generation,page_ordinal,display_generation) "
        "VALUES (1,2,8,'legacy',2,1,0,1), (1,2,9,'staged',2,1,1,0)"
    )
    conn.commit()

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            return ReactionDetailFetchResult(
                failure=GatewayFailure(GatewayFailureKind.INVALID_TARGET, "Permanent", "target", False)
            )

    result = asyncio.run(
        ReactionDetailRefresher(conn, Gateway()).refresh_one(1, 2, 1, entity=1, offset="resume", now=10)
    )
    assert result.status == "unavailable"
    assert result.next_offset is None
    assert conn.execute(
        "SELECT next_offset, staged_count, next_attempt_at FROM message_reaction_event_status"
    ).fetchone() == (None, 0, None)
    assert conn.execute(
        "SELECT emoji, display_generation FROM message_reaction_events ORDER BY display_generation, emoji"
    ).fetchall() == [("legacy", 1)]
    conn.close()


def test_identical_aggregate_keeps_terminal_detail_suppressed(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='unavailable', failure_kind='access_lost', "
        "next_attempt_at=NULL WHERE dialog_id=1 AND message_id=2"
    )
    conn.commit()
    assert apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=2, observation_sequence=2
    )
    assert conn.execute("SELECT generation, observation_sequence FROM message_reaction_aggregate_state").fetchone() == (
        1,
        2,
    )
    assert conn.execute(
        "SELECT status, failure_kind, next_attempt_at FROM message_reaction_event_status"
    ).fetchone() == ("unavailable", "access_lost", None)
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == []
    conn.close()


def test_detail_final_cas_discards_page_when_status_changes_before_update(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "CREATE TRIGGER detail_status_race BEFORE INSERT ON message_reaction_events BEGIN "
        "UPDATE message_reaction_event_status SET status='partial', next_offset='race' "
        "WHERE dialog_id=1 AND message_id=2; END"
    )
    conn.commit()

    class Gateway:
        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            return ReactionDetailFetchResult(page=ReactionDetailPage((ReactionEvent(4, "👍", None),), None))

    result = asyncio.run(ReactionDetailRefresher(conn, Gateway(), now=lambda: 5).refresh_one(1, 2, 1, entity=1, now=5))
    assert result.status == "stale_writer"
    assert conn.execute("SELECT COUNT(*) FROM message_reaction_events").fetchone() == (0,)
    assert conn.execute("SELECT status, next_offset FROM message_reaction_event_status").fetchone() == (
        "stale",
        None,
    )
    conn.close()


def test_detail_rechecks_enrollment_after_gateway_returns(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )

    class BlockingGateway:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def fetch_reaction_page(
            self, entity: object, message_id: int, *, offset: str | None, limit: int
        ) -> ReactionDetailFetchResult:
            del entity, message_id, offset, limit
            self.entered.set()
            await self.release.wait()
            return ReactionDetailFetchResult(page=ReactionDetailPage((), None))

    async def run() -> ReactionDetailResult:
        gateway = BlockingGateway()
        task = asyncio.create_task(
            ReactionDetailRefresher(conn, gateway, now=lambda: 4).refresh_one(1, 2, 1, entity=1, now=4)
        )
        await gateway.entered.wait()
        conn.execute("UPDATE full_history_enrollment SET enabled=0 WHERE dialog_id=1")
        conn.commit()
        gateway.release.set()
        return await task

    result = asyncio.run(run())
    assert result.status == "ineligible"
    assert conn.execute("SELECT status FROM message_reaction_event_status").fetchone() == ("stale",)
    conn.close()


def test_empty_aggregate_is_not_a_detail_candidate_without_explicit_retry(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    apply_aggregate_observation(conn, 1, 2, [], source="raw_update", observed_at=2, observation_sequence=2)
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == []
    conn.execute(
        "UPDATE message_reaction_event_status SET status='unavailable', next_attempt_at=0 WHERE dialog_id=1 AND message_id=2"
    )
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == [(1, 2, 2, None)]
    conn.execute(
        "UPDATE message_reaction_event_status SET status='complete', detail_generation=2, display_generation=1, "
        "published_generation=2 WHERE dialog_id=1 AND message_id=2"
    )
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == []
    assert _next_release_at(conn, _NEXT_REACTION_RELEASE_SQL, 600) is None

    class TerminalRefresher:
        async def refresh_one(self, *args: object, **kwargs: object) -> ReactionDetailResult:
            del args, kwargs
            raise AssertionError("terminal empty aggregate must not issue detail RPC")

    result = asyncio.run(
        refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionDetailRefresher, TerminalRefresher()),
                cast(TelegramReadReceiptGateway, object()),
            ),
            MessageFactRefreshPolicy(
                reaction_max_messages_per_cycle=1,
                read_at_max_messages_per_cycle=0,
                pause_seconds=0,
                read_at_ttl_seconds=600,
            ),
            now=10,
        )
    )
    assert result.reaction_refreshed == 0
    conn.close()


def test_reaction_candidates_prioritize_due_retries_over_future_unavailable_rows(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    for message_id, sent_at in ((3, 30), (4, 20), (5, 10)):
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,?,?)", (message_id, sent_at))
        apply_aggregate_observation(
            conn,
            1,
            message_id,
            [ReactionAggregate("👍", 1)],
            source="history",
            observed_at=1,
            observation_sequence=message_id,
        )
    conn.commit()
    conn.executemany(
        "UPDATE message_reaction_event_status SET status=?, next_attempt_at=?, checked_at=? "
        "WHERE dialog_id=1 AND message_id=?",
        [
            ("unavailable", 100, 1, 2),
            ("unavailable", 0, 50, 3),
            ("unavailable", 0, 10, 4),
            ("stale", 999, 0, 5),
        ],
    )
    conn.commit()
    rows = _reaction_candidates(conn, stale_before_utc=100, limit=3)
    assert [row[1] for row in rows] == [5, 4, 3]
    assert _next_release_at(conn, _NEXT_REACTION_RELEASE_SQL, 0) == 0
    conn.close()


def test_partial_resume_candidates_precede_stale_backlog_fairly(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    for message_id, sent_at in ((6, 20), (7, 10), (8, 1)):
        conn.execute("INSERT INTO messages(dialog_id,message_id,sent_at) VALUES (1,?,?)", (message_id, sent_at))
        apply_aggregate_observation(
            conn,
            1,
            message_id,
            [ReactionAggregate("👍", 1)],
            source="history",
            observed_at=1,
            observation_sequence=message_id,
        )
    conn.commit()
    conn.executemany(
        "UPDATE message_reaction_event_status SET status=?, next_offset=?, next_attempt_at=?, checked_at=? "
        "WHERE dialog_id=1 AND message_id=?",
        [("partial", "resume-6", 0, 50, 6), ("partial", "resume-7", 0, 10, 7), ("stale", None, 0, 0, 8)],
    )
    conn.commit()
    assert [row[1] for row in _reaction_candidates(conn, stale_before_utc=10, limit=3)] == [7, 6, 8]
    conn.close()


def test_deleted_reaction_message_is_not_released_or_selected(tmp_path: Path) -> None:
    conn = _db(tmp_path)
    apply_aggregate_observation(
        conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='unavailable', next_attempt_at=0 "
        "WHERE dialog_id=1 AND message_id=2"
    )
    conn.execute("UPDATE messages SET is_deleted=1 WHERE dialog_id=1 AND message_id=2")
    conn.commit()
    assert _reaction_candidates(conn, stale_before_utc=10, limit=10) == []
    assert _next_release_at(conn, _NEXT_REACTION_RELEASE_SQL, 0) is None
    conn.close()
