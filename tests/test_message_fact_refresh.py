from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.message_fact_refresh import (
    _NEXT_READ_AT_RELEASE_SQL,
    MessageFactRefreshDemandAdapter,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    _claim_reaction_pages,
    _next_release_at,
    _reaction_release_at,
    _read_at_candidates,
    _terminal_read_at_suppressed,
    refresh_message_facts_once,
)
from mcp_telegram.reactions import ReactionDetailRefresher
from mcp_telegram.reactions.detail import ReactionDetailResult
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptBudgetExhaustedError
from mcp_telegram.telegram_fact_queries import _persist_message_too_old
from mcp_telegram.telegram_reading import ReadDateFetchResult, ReadDateReason, TelegramReadReceiptGateway
from mcp_telegram.telegram_rpc_consumers import TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, rpc_scope
from tests.history_enrollment_helpers import seed_full_history_enrollment


def _make_db(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE synced_dialogs (
            dialog_id INTEGER PRIMARY KEY,
            status TEXT NOT NULL,
            read_outbox_max_id INTEGER
        );
        CREATE TABLE full_history_enrollment (
            dialog_id INTEGER PRIMARY KEY,
            enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
            source TEXT NOT NULL CHECK(source IN ('explicit', 'automatic', 'migration')),
            updated_at INTEGER NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL
        );
        CREATE TABLE messages (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            sent_at INTEGER NOT NULL,
            out INTEGER NOT NULL,
            media_kind TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE message_reaction_aggregate_state (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            generation INTEGER NOT NULL,
            observed_at INTEGER NOT NULL,
            observation_sequence INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_rank INTEGER NOT NULL,
            aggregate_row_count INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        CREATE TABLE message_reaction_event_status (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            aggregate_generation INTEGER NOT NULL,
            detail_generation INTEGER NOT NULL DEFAULT 0,
            display_generation INTEGER NOT NULL DEFAULT 0,
            published_generation INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL,
            checked_at INTEGER NOT NULL DEFAULT 0,
            next_offset TEXT,
            next_attempt_at INTEGER,
            staged_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (dialog_id, message_id)
        );
        CREATE TABLE message_read_facts (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            read_at INTEGER,
            checked_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            reason TEXT NOT NULL CHECK (reason IN (
                'resolved', 'date_omitted', 'message_not_read_yet', 'flood_wait',
                'transient', 'message_too_old', 'privacy_restricted',
                'not_mutual_contact', 'invalid_target', 'access_lost'
            )),
            next_attempt_at INTEGER,
            PRIMARY KEY (dialog_id, message_id)
        ) WITHOUT ROWID;
        CREATE TABLE read_date_expiry_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            expired_through_sent_at INTEGER,
            observed_at INTEGER,
            witness_dialog_id INTEGER,
            witness_message_id INTEGER
        ) WITHOUT ROWID;
        INSERT INTO read_date_expiry_state(singleton) VALUES (1);
        CREATE INDEX idx_message_read_facts_checked
        ON message_read_facts(dialog_id, checked_at);
        CREATE INDEX idx_message_read_facts_next_attempt
        ON message_read_facts(dialog_id, next_attempt_at)
        WHERE next_attempt_at IS NOT NULL;
        CREATE TABLE reaction_detail_pacing_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            window_started_at INTEGER NOT NULL,
            release_at INTEGER NOT NULL,
            claimed_pages INTEGER NOT NULL DEFAULT 0,
            started_pages INTEGER NOT NULL DEFAULT 0
        );
        INSERT INTO reaction_detail_pacing_state VALUES (1, 0, 0, 0, 0);
        """
    )
    return conn


def _policy(*, reaction_max: int = 10, read_at_max: int = 10) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        reaction_max_messages_per_cycle=reaction_max,
        read_at_max_messages_per_cycle=read_at_max,
        pause_seconds=0.01,
        read_at_ttl_seconds=600,
    )


class _ReadReceiptGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[object, int]] = []

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        self.calls.append((entity, message_id))
        return ReadDateFetchResult(read_at=1_700_000_000 + message_id, status="complete")


class _BlockingReadReceiptGateway:
    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        del entity, message_id
        self.entered.set()
        await asyncio.Event().wait()
        return ReadDateFetchResult(status="complete")


class _FailingReadReceiptGateway:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
        del entity, message_id
        raise self.error


def _budget_exhausted_error() -> Exception:
    return RpcAttemptBudgetExhaustedError("slice budget exhausted")


def _admission_closed_error() -> Exception:
    with rpc_scope(TelegramRpcSource.READ_RECEIPT_PROBE) as scope:
        return RpcAdmissionClosedError(scope, "scheduler closed")


def _seed_reaction_candidates(conn: sqlite3.Connection, count: int = 8) -> None:
    conn.execute("INSERT INTO synced_dialogs VALUES (1, 'synced', NULL)")
    seed_full_history_enrollment(conn, 1, enabled=True)
    for message_id in range(1, count + 1):
        conn.execute("INSERT INTO messages VALUES (1, ?, ?, 0, NULL, 0)", (message_id, message_id))
        conn.execute(
            "INSERT INTO message_reaction_aggregate_state VALUES (1, ?, 1, 1, ?, 'history', 1, 1)",
            (message_id, message_id),
        )
        conn.execute(
            "INSERT INTO message_reaction_event_status "
            "(dialog_id, message_id, aggregate_generation, status, checked_at, next_attempt_at) "
            "VALUES (1, ?, 1, 'stale', 1, 1)",
            (message_id,),
        )
    conn.commit()


class _RecordingReactionRefresher:
    def __init__(self, calls: list[int], result: ReactionDetailResult | None = None) -> None:
        self.calls = calls
        self.result = result or ReactionDetailResult("partial", fetched_pages=1)

    async def refresh_one(
        self, dialog_id: int, message_id: int, generation: int, **kwargs: object
    ) -> ReactionDetailResult:
        cancellation_event = kwargs.get("cancellation_event")
        del dialog_id, generation
        self.calls.append(message_id)
        if isinstance(cancellation_event, asyncio.Event) and cancellation_event.is_set():
            return ReactionDetailResult("cancelled")
        return self.result


@pytest.mark.asyncio
async def test_reaction_pacing_caps_repeated_slices_and_opens_next_window() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn)
    calls: list[int] = []
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
        cast(TelegramReadReceiptGateway, object()),
    )
    policy = MessageFactRefreshPolicy(
        reaction_max_messages_per_cycle=10,
        read_at_max_messages_per_cycle=0,
        pause_seconds=0,
        read_at_ttl_seconds=600,
        reaction_detail_max_pages_per_cycle=5,
        reaction_detail_cycle_seconds=600,
    )

    for _ in range(3):
        await refresh_message_facts_once(deps, policy, now=100)
    assert len(calls) == 5
    assert conn.execute(
        "SELECT window_started_at, release_at, claimed_pages, started_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (100, 700, 5, 5)

    await refresh_message_facts_once(deps, policy, now=699)
    assert len(calls) == 5
    await refresh_message_facts_once(deps, policy, now=700)
    assert len(calls) == 10
    conn.close()


@pytest.mark.asyncio
async def test_reaction_pacing_claim_survives_cancellation_and_restart(tmp_path: object) -> None:
    path = cast(Path, tmp_path) / "sync.db"
    conn = _make_db(path)
    _seed_reaction_candidates(conn)
    calls: list[int] = []
    event = asyncio.Event()
    policy = MessageFactRefreshPolicy(10, 0, 0, 600, 5, 600)
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
        cast(TelegramReadReceiptGateway, object()),
    )
    await refresh_message_facts_once(deps, policy, now=100, shutdown_event=event)
    assert len(calls) == 5
    event.set()
    await refresh_message_facts_once(deps, policy, now=100, shutdown_event=event)
    assert len(calls) == 5
    conn.close()

    reopened = sqlite3.connect(path)
    calls.clear()
    reopened_deps = MessageFactRefreshDeps(
        reopened,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
        cast(TelegramReadReceiptGateway, object()),
    )
    await refresh_message_facts_once(reopened_deps, policy, now=100)
    assert calls == []
    assert reopened.execute("SELECT claimed_pages, started_pages FROM reaction_detail_pacing_state").fetchone() == (
        5,
        5,
    )
    reopened.close()


@pytest.mark.asyncio
async def test_open_reaction_window_accepts_only_one_claim_batch() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn, count=2)
    calls: list[int] = []
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
        cast(TelegramReadReceiptGateway, object()),
    )
    policy = MessageFactRefreshPolicy(10, 0, 0, 600, 5, 600)

    await refresh_message_facts_once(deps, policy, now=100)
    conn.execute("INSERT INTO messages VALUES (1, 3, 3, 0, NULL, 0)")
    conn.execute("INSERT INTO message_reaction_aggregate_state VALUES (1, 3, 1, 1, 3, 'history', 1, 1)")
    conn.execute(
        "INSERT INTO message_reaction_event_status "
        "(dialog_id, message_id, aggregate_generation, status, checked_at, next_attempt_at) "
        "VALUES (1, 3, 1, 'stale', 1, 1)"
    )
    conn.commit()
    await refresh_message_facts_once(deps, policy, now=101)
    assert len(calls) == 2
    assert conn.execute(
        "SELECT window_started_at, release_at, claimed_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (100, 700, 2)
    conn.close()


def test_concurrent_reaction_claims_share_one_window(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    conn = _make_db(path)
    _seed_reaction_candidates(conn)
    conn.close()

    def claim() -> int:
        worker_conn = sqlite3.connect(path, timeout=5)
        try:
            return len(
                _claim_reaction_pages(
                    worker_conn,
                    now=100,
                    max_pages=5,
                    cycle_seconds=600,
                    candidate_limit=10,
                )
            )
        finally:
            worker_conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = sorted(executor.map(lambda _: claim(), range(2)))

    assert results == [0, 5]
    check = sqlite3.connect(path)
    assert check.execute(
        "SELECT window_started_at, release_at, claimed_pages, started_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (100, 700, 5, 0)
    check.close()


@pytest.mark.asyncio
async def test_empty_reaction_scan_does_not_open_or_reset_window() -> None:
    conn = _make_db()
    calls: list[int] = []
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
        cast(TelegramReadReceiptGateway, object()),
    )
    policy = MessageFactRefreshPolicy(10, 0, 0, 600, 5, 600)

    await refresh_message_facts_once(deps, policy, now=100)
    assert calls == []
    assert conn.execute(
        "SELECT window_started_at, release_at, claimed_pages, started_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (0, 0, 0, 0)
    conn.close()


@pytest.mark.asyncio
async def test_set_shutdown_skips_reaction_claim() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn, count=1)
    calls: list[int] = []
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
        cast(TelegramReadReceiptGateway, object()),
    )
    shutdown_event = asyncio.Event()
    shutdown_event.set()
    await refresh_message_facts_once(
        deps,
        MessageFactRefreshPolicy(10, 0, 0, 600, 5, 600),
        now=100,
        shutdown_event=shutdown_event,
    )
    assert calls == []
    assert conn.execute(
        "SELECT window_started_at, release_at, claimed_pages, started_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (0, 0, 0, 0)
    conn.close()


@pytest.mark.asyncio
async def test_demand_adapter_passes_shutdown_to_message_fact_refresh() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn, count=1)
    calls: list[int] = []
    shutdown_event = asyncio.Event()
    shutdown_event.set()
    adapter = MessageFactRefreshDemandAdapter(
        MessageFactRefreshDeps(
            conn,
            cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls)),
            cast(TelegramReadReceiptGateway, object()),
        ),
        MessageFactRefreshPolicy(10, 0, 0, 600, 5, 600),
        shutdown_event=shutdown_event,
    )

    await adapter.run_slice(RpcAttemptBudget(limit=1))

    assert calls == []
    assert conn.execute(
        "SELECT window_started_at, release_at, claimed_pages, started_pages FROM reaction_detail_pacing_state"
    ).fetchone() == (0, 0, 0, 0)
    conn.close()


@pytest.mark.asyncio
async def test_reaction_flood_wait_extends_durable_release() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn, count=1)
    calls: list[int] = []
    result = ReactionDetailResult("unavailable", failure_kind="flood_wait", retry_after=1_000)
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, _RecordingReactionRefresher(calls, result)),
        cast(TelegramReadReceiptGateway, object()),
        clock=lambda: 350,
    )
    await refresh_message_facts_once(
        deps,
        MessageFactRefreshPolicy(10, 0, 0, 600, 5, 600),
        now=100,
    )
    assert conn.execute("SELECT release_at FROM reaction_detail_pacing_state").fetchone() == (1_350,)
    conn.close()


@pytest.mark.asyncio
async def test_read_date_runs_before_reaction_when_both_lanes_are_due() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn, count=1)
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 2);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 2, 1000, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    order: list[str] = []

    class OrderedReadGateway(_ReadReceiptGateway):
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            order.append("read")
            return await super().fetch_outbox_read_date(entity, message_id)

    class OrderedReactionRefresher(_RecordingReactionRefresher):
        async def refresh_one(
            self, dialog_id: int, message_id: int, generation: int, **kwargs: object
        ) -> ReactionDetailResult:
            order.append("reaction")
            return await super().refresh_one(dialog_id, message_id, generation, **kwargs)

    await refresh_message_facts_once(
        MessageFactRefreshDeps(
            conn,
            cast(ReactionDetailRefresher, OrderedReactionRefresher([])),
            cast(TelegramReadReceiptGateway, OrderedReadGateway()),
        ),
        MessageFactRefreshPolicy(10, 1, 0, 600, 5, 600),
        now=2_000,
    )
    assert order == ["read", "reaction"]
    conn.close()


def test_open_reaction_pacing_boundary_does_not_hide_due_read_dates() -> None:
    conn = _make_db()
    _seed_reaction_candidates(conn, count=1)
    conn.execute("UPDATE reaction_detail_pacing_state SET release_at=700 WHERE singleton=1")
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 2);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 2, 1000, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    deps = MessageFactRefreshDeps(
        conn,
        cast(ReactionDetailRefresher, object()),
        cast(TelegramReadReceiptGateway, object()),
    )
    policy = MessageFactRefreshPolicy(10, 1, 0, 600, 5, 600)
    status = MessageFactRefreshDemandAdapter(deps, policy).status(100)
    assert _reaction_release_at(conn) == 700
    assert status is not None
    assert status.release_at == 0
    conn.close()


@pytest.mark.asyncio
async def test_read_at_cycle_telemetry_is_aggregate_and_terminal_safe() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES
                (20, 1, 1000, 1, NULL, 0),
                (20, 2, 1001, 1, NULL, 0),
                (20, 3, 1002, 1, NULL, 0);
        INSERT INTO message_read_facts VALUES
            (20, 2, NULL, 1000, 'missing', 'date_omitted', 1600),
            (20, 3, 1700000003, 1000, 'complete', 'resolved', NULL);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    observations: list[Mapping[str, object]] = []
    try:
        await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionDetailRefresher, object()),
                cast(TelegramReadReceiptGateway, _ReadReceiptGateway()),
                read_at_observer=observations.append,
            ),
            _policy(reaction_max=0),
            now=2_000,
        )
    finally:
        conn.close()

    assert observations == [
        {
            "first_attempts": 1,
            "retry_attempts": 1,
            "terminal_suppressed": 1,
            "complete": 2,
            "missing": 0,
            "unavailable": 0,
            "reason_counts": {
                "resolved": 2,
                "date_omitted": 0,
                "message_not_read_yet": 0,
                "flood_wait": 0,
                "transient": 0,
                "message_too_old": 0,
                "privacy_restricted": 0,
                "not_mutual_contact": 0,
                "invalid_target": 0,
                "access_lost": 0,
            },
            "rpc_attempts": 2,
            "message_too_old_responses": 0,
            "locally_classified": 0,
            "cutoff_backlog_suppressed": 0,
            "cutoff_skipped": 0,
            "candidate_count": 2,
            "measurement_complete": True,
        }
    ]


@pytest.mark.asyncio
async def test_read_at_attempt_telemetry_distinguishes_equal_message_ids_across_dialogs() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10), (21, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user'), (21, 'user');
            INSERT INTO messages VALUES (20, 1, 1000, 1, NULL, 0), (21, 1, 1001, 1, NULL, 0);
            INSERT INTO message_read_facts VALUES (20, 1, NULL, 1000, 'missing', 'date_omitted', 1600);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    seed_full_history_enrollment(conn, 21, enabled=True)
    observations: list[Mapping[str, object]] = []
    try:
        await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionDetailRefresher, object()),
                cast(TelegramReadReceiptGateway, _ReadReceiptGateway()),
                read_at_observer=observations.append,
            ),
            _policy(reaction_max=0),
            now=2_000,
        )
    finally:
        conn.close()

    assert observations[0]["first_attempts"] == 1
    assert observations[0]["retry_attempts"] == 1


@pytest.mark.asyncio
async def test_canceled_read_at_cycle_publishes_no_incomplete_telemetry() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    gateway = _BlockingReadReceiptGateway()
    observations: list[Mapping[str, object]] = []
    task = asyncio.create_task(
        refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionDetailRefresher, object()),
                cast(TelegramReadReceiptGateway, gateway),
                read_at_observer=observations.append,
            ),
            _policy(reaction_max=0),
            now=2_000,
        )
    )
    await gateway.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert observations == []
    assert conn.execute("SELECT COUNT(*) FROM message_read_facts").fetchone() == (0,)
    conn.close()


@pytest.mark.asyncio
async def test_message_too_old_cutoff_is_global_and_sent_at_based() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10), (21, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user'), (21, 'user');
        INSERT INTO messages VALUES
            (20, 1, 100, 1, NULL, 0),
            (20, 2, 300, 1, NULL, 0),
            (21, 1, 50, 1, NULL, 0),
            (21, 2, 301, 1, NULL, 0),
            (21, 3, 302, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    seed_full_history_enrollment(conn, 21, enabled=True)
    calls: list[tuple[int, int]] = []
    observations: list[Mapping[str, object]] = []

    class Gateway:
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            dialog_id = int(cast(int, entity))
            calls.append((dialog_id, message_id))
            if (dialog_id, message_id) == (20, 2):
                return ReadDateFetchResult(status="unavailable", reason=ReadDateReason.MESSAGE_TOO_OLD)
            return ReadDateFetchResult(read_at=1_700_000_000, status="complete")

    await refresh_message_facts_once(
        MessageFactRefreshDeps(
            conn,
            cast(ReactionDetailRefresher, object()),
            cast(TelegramReadReceiptGateway, Gateway()),
            read_at_observer=observations.append,
        ),
        _policy(reaction_max=0, read_at_max=10),
        now=2_000,
    )

    assert calls == [(21, 3), (21, 2), (20, 2)]
    assert conn.execute("SELECT expired_through_sent_at FROM read_date_expiry_state").fetchone() == (300,)
    assert conn.execute(
        "SELECT message_id, reason FROM message_read_facts WHERE dialog_id=20 ORDER BY message_id"
    ).fetchall() == [(1, "message_too_old"), (2, "message_too_old")]
    assert conn.execute(
        "SELECT message_id, reason FROM message_read_facts WHERE dialog_id=21 ORDER BY message_id"
    ).fetchall() == [(1, "message_too_old"), (2, "resolved"), (3, "resolved")]
    assert [message.message_id for message in _read_at_candidates(conn, stale_before_utc=2_000, limit=10)] == []

    first_observation = observations[0]
    assert first_observation["candidate_count"] == 5
    assert first_observation["rpc_attempts"] == 3
    assert cast(int, first_observation["first_attempts"]) + cast(int, first_observation["retry_attempts"]) == 3
    assert first_observation["message_too_old_responses"] == 1
    assert first_observation["locally_classified"] == 3
    assert first_observation["cutoff_backlog_suppressed"] == 0
    assert first_observation["cutoff_skipped"] == 1

    # History imported after the witness remains outside both candidate and
    # release SQL, so it cannot trigger a gateway call.
    conn.execute("INSERT INTO messages VALUES (20, 3, 200, 1, NULL, 0)")
    conn.commit()
    await refresh_message_facts_once(
        MessageFactRefreshDeps(
            conn,
            cast(ReactionDetailRefresher, object()),
            cast(TelegramReadReceiptGateway, Gateway()),
            read_at_observer=observations.append,
        ),
        _policy(reaction_max=0, read_at_max=10),
        now=2_001,
    )
    assert _read_at_candidates(conn, stale_before_utc=2_001, limit=10) == []
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL) is None
    assert calls == [(21, 3), (21, 2), (20, 2)]
    second_observation = observations[1]
    assert second_observation["candidate_count"] == 0
    assert second_observation["rpc_attempts"] == 0
    assert second_observation["cutoff_backlog_suppressed"] == 1
    assert second_observation["cutoff_skipped"] == 0
    conn.close()


def test_older_message_too_old_witness_does_not_advance_or_reclassify() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 2);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 1, 100, 1, NULL, 0), (20, 2, 300, 1, NULL, 0);
        INSERT INTO message_read_facts VALUES
            (20, 1, NULL, 1000, 'unavailable', 'message_too_old', NULL),
            (20, 2, NULL, 1000, 'unavailable', 'message_too_old', NULL);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    conn.execute(
        "UPDATE read_date_expiry_state SET expired_through_sent_at=300, observed_at=1000, "
        "witness_dialog_id=20, witness_message_id=2 WHERE singleton=1"
    )
    conn.commit()

    classified = _persist_message_too_old(conn, 20, 1, sent_at=100, checked_at=2000)

    assert classified == 0
    assert conn.execute("SELECT expired_through_sent_at FROM read_date_expiry_state").fetchone() == (300,)
    assert conn.execute("SELECT COUNT(*) FROM message_read_facts WHERE reason='message_too_old'").fetchone() == (2,)
    conn.close()


@pytest.mark.asyncio
async def test_privacy_outcome_does_not_advance_read_date_cutoff() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 1);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 1, 100, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)

    class Gateway:
        async def fetch_outbox_read_date(self, entity: object, message_id: int) -> ReadDateFetchResult:
            del entity, message_id
            return ReadDateFetchResult(status="unavailable", reason=ReadDateReason.PRIVACY_RESTRICTED)

    await refresh_message_facts_once(
        MessageFactRefreshDeps(
            conn, cast(ReactionDetailRefresher, object()), cast(TelegramReadReceiptGateway, Gateway())
        ),
        _policy(reaction_max=0, read_at_max=1),
        now=2_000,
    )
    assert conn.execute("SELECT expired_through_sent_at FROM read_date_expiry_state").fetchone() == (None,)
    assert conn.execute(
        "SELECT status, reason, next_attempt_at FROM message_read_facts WHERE dialog_id=20 AND message_id=1"
    ).fetchone() == ("unavailable", "privacy_restricted", None)
    conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [_budget_exhausted_error, _admission_closed_error],
    ids=["budget-exhausted", "admission-closed"],
)
async def test_read_at_control_errors_leave_facts_and_telemetry_untouched(
    error_factory: Callable[[], Exception],
) -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    observations: list[Mapping[str, object]] = []
    error = error_factory()

    try:
        with pytest.raises(type(error)):
            await refresh_message_facts_once(
                MessageFactRefreshDeps(
                    conn,
                    cast(ReactionDetailRefresher, object()),
                    cast(TelegramReadReceiptGateway, _FailingReadReceiptGateway(error)),
                    read_at_observer=observations.append,
                ),
                _policy(reaction_max=0),
                now=2_000,
            )
        assert conn.execute("SELECT COUNT(*) FROM message_read_facts").fetchone() == (0,)
        assert observations == []
    finally:
        conn.close()


def test_read_at_cursor_null_has_no_candidate_or_release() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', NULL);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)

    assert _read_at_candidates(conn, stale_before_utc=2_000, limit=10) == []
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL) is None
    adapter = MessageFactRefreshDemandAdapter(
        MessageFactRefreshDeps(
            conn, cast(ReactionDetailRefresher, object()), cast(TelegramReadReceiptGateway, object())
        ),
        _policy(reaction_max=0),
    )
    assert adapter.status(2_000) is None
    conn.close()


def test_read_at_message_above_cursor_is_excluded() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 5);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES
            (20, 5, 1000, 1, NULL, 0),
            (20, 6, 1001, 1, NULL, 0);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)

    assert [message.message_id for message in _read_at_candidates(conn, stale_before_utc=2_000, limit=10)] == [5]
    conn.close()


def test_terminal_read_at_set_has_no_status_or_release() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 5);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES (20, 5, 1000, 1, NULL, 0);
        INSERT INTO message_read_facts VALUES (20, 5, 1700000005, 1000, 'complete', 'resolved', NULL);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    adapter = MessageFactRefreshDemandAdapter(
        MessageFactRefreshDeps(
            conn, cast(ReactionDetailRefresher, object()), cast(TelegramReadReceiptGateway, object())
        ),
        _policy(reaction_max=0),
    )

    assert _read_at_candidates(conn, stale_before_utc=2_000, limit=10) == []
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL) is None
    assert adapter.status(2_000) is None
    conn.close()


def test_read_at_candidate_and_release_boundaries_match_v70_schedule() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES
            (20, 1, 1000, 1, NULL, 0),
            (20, 2, 1001, 1, NULL, 0),
            (20, 3, 1002, 1, NULL, 0),
            (20, 4, 1003, 1, NULL, 0),
            (20, 5, 1004, 1, NULL, 1);
        INSERT INTO message_read_facts VALUES
            (20, 1, NULL, 1000, 'unavailable', 'transient', 2000),
            (20, 2, NULL, 1000, 'unavailable', 'transient', 3000),
            (20, 3, NULL, 1000, 'unavailable', 'invalid_target', NULL),
            (20, 4, 1700000004, 1000, 'complete', 'resolved', NULL),
            (20, 5, NULL, 1000, 'unavailable', 'invalid_target', NULL);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)

    assert [message.message_id for message in _read_at_candidates(conn, stale_before_utc=2000, limit=10)] == [1]
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL) == 2000
    assert _terminal_read_at_suppressed(conn) == 2

    conn.execute("UPDATE message_read_facts SET next_attempt_at = 3000 WHERE message_id = 1")
    conn.commit()
    assert _read_at_candidates(conn, stale_before_utc=2000, limit=10) == []
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL) == 3000

    conn.close()


@pytest.mark.asyncio
async def test_refresh_message_facts_once_respects_zero_budget() -> None:
    conn = _make_db()
    read_receipts = _ReadReceiptGateway()

    try:
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionDetailRefresher, object()),
                cast(TelegramReadReceiptGateway, read_receipts),
            ),
            _policy(reaction_max=0, read_at_max=0),
            now=2_000,
            shutdown_event=asyncio.Event(),
        )
    finally:
        conn.close()

    assert result.reaction_refreshed == 0
    assert read_receipts.calls == []
