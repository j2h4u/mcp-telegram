from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Mapping
from typing import cast

import pytest

from mcp_telegram.message_fact_refresh import (
    _NEXT_READ_AT_RELEASE_SQL,
    MessageFactRefreshDemandAdapter,
    MessageFactRefreshDeps,
    MessageFactRefreshPolicy,
    _next_release_at,
    _read_at_candidates,
    refresh_message_facts_once,
)
from mcp_telegram.reactions.contracts import ReactionFreshness
from mcp_telegram.reactions.refresh import ReactionFreshener
from mcp_telegram.telegram_demand import RpcAttemptBudgetExhaustedError
from mcp_telegram.telegram_reading import ReadDateFetchResult, TelegramReadReceiptGateway
from mcp_telegram.telegram_rpc_consumers import TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import RpcAdmissionClosedError, rpc_scope
from tests.history_enrollment_helpers import seed_full_history_enrollment


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
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
            media_kind TEXT
        );
        CREATE TABLE message_reactions (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            emoji TEXT NOT NULL,
            count INTEGER NOT NULL
        );
        CREATE TABLE message_reactions_freshness (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            checked_at INTEGER NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        CREATE TABLE message_read_facts (
            dialog_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            read_at INTEGER,
            checked_at INTEGER NOT NULL,
            status TEXT NOT NULL,
            PRIMARY KEY (dialog_id, message_id)
        );
        """
    )
    return conn


def _policy(*, reaction_max: int = 10, read_at_max: int = 10) -> MessageFactRefreshPolicy:
    return MessageFactRefreshPolicy(
        reaction_max_messages_per_cycle=reaction_max,
        read_at_max_messages_per_cycle=read_at_max,
        pause_seconds=0.01,
        reaction_ttl_seconds=600,
        read_at_ttl_seconds=600,
    )


class _ReactionFreshener:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object, list[int]]] = []

    async def refresh(self, dialog_id: int, entity: object, message_ids: list[int]) -> ReactionFreshness:
        self.calls.append((dialog_id, entity, message_ids))
        return ReactionFreshness(
            requested_count=len(message_ids),
            fresh_count=0,
            stale_count=len(message_ids),
            refreshed_count=len(message_ids),
            status="refreshed",
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


@pytest.mark.asyncio
async def test_refresh_message_facts_once_refreshes_reactions_and_read_at() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (10, 'synced', 10), (20, 'synced', 10), (30, 'access_lost', 10);
        INSERT INTO entities VALUES (10, 'user'), (20, 'user'), (30, 'user');
            INSERT INTO messages VALUES
                (10, 1, 1000, 0, NULL),
                (20, 2, 1001, 1, NULL),
                (30, 3, 1002, 1, NULL);
        INSERT INTO message_reactions VALUES (10, 1, '👍', 1);
        """
    )
    seed_full_history_enrollment(conn, 10, enabled=True)
    seed_full_history_enrollment(conn, 20, enabled=True)
    seed_full_history_enrollment(conn, 30, enabled=False)
    reactions = _ReactionFreshener()
    read_receipts = _ReadReceiptGateway()

    try:
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, reactions),
                cast(TelegramReadReceiptGateway, read_receipts),
            ),
            _policy(),
            now=2_000,
        )
        stored_read_facts = conn.execute("SELECT read_at, checked_at, status FROM message_read_facts").fetchall()
    finally:
        conn.close()

    assert result.reaction_refreshed == 1
    assert reactions.calls == [(10, 10, [1])]
    assert read_receipts.calls == [(20, 2)]
    assert stored_read_facts == [(1_700_000_002, 2_000, "complete")]


@pytest.mark.asyncio
async def test_read_at_cycle_telemetry_is_aggregate_and_terminal_safe() -> None:
    conn = _make_db()
    conn.executescript(
        """
        INSERT INTO synced_dialogs VALUES (20, 'synced', 10);
        INSERT INTO entities VALUES (20, 'user');
        INSERT INTO messages VALUES
            (20, 1, 1000, 1, NULL),
            (20, 2, 1001, 1, NULL),
            (20, 3, 1002, 1, NULL);
        INSERT INTO message_read_facts VALUES
            (20, 2, NULL, 1000, 'missing'),
            (20, 3, 1700000003, 1000, 'complete');
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    observations: list[Mapping[str, object]] = []
    try:
        await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, _ReactionFreshener()),
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
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL), (21, 1, 1001, 1, NULL);
        INSERT INTO message_read_facts VALUES (20, 1, NULL, 1000, 'missing');
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    seed_full_history_enrollment(conn, 21, enabled=True)
    observations: list[Mapping[str, object]] = []
    try:
        await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, _ReactionFreshener()),
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
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    gateway = _BlockingReadReceiptGateway()
    observations: list[Mapping[str, object]] = []
    task = asyncio.create_task(
        refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, _ReactionFreshener()),
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
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL);
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
                    cast(ReactionFreshener, _ReactionFreshener()),
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
        INSERT INTO messages VALUES (20, 1, 1000, 1, NULL);
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)

    assert _read_at_candidates(conn, stale_before_utc=2_000, limit=10) == []
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL, 600) is None
    adapter = MessageFactRefreshDemandAdapter(
        MessageFactRefreshDeps(conn, cast(ReactionFreshener, _ReactionFreshener()), cast(TelegramReadReceiptGateway, object())),
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
            (20, 5, 1000, 1, NULL),
            (20, 6, 1001, 1, NULL);
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
        INSERT INTO messages VALUES (20, 5, 1000, 1, NULL);
        INSERT INTO message_read_facts VALUES (20, 5, 1700000005, 1000, 'complete');
        """
    )
    seed_full_history_enrollment(conn, 20, enabled=True)
    adapter = MessageFactRefreshDemandAdapter(
        MessageFactRefreshDeps(conn, cast(ReactionFreshener, _ReactionFreshener()), cast(TelegramReadReceiptGateway, object())),
        _policy(reaction_max=0),
    )

    assert _read_at_candidates(conn, stale_before_utc=2_000, limit=10) == []
    assert _next_release_at(conn, _NEXT_READ_AT_RELEASE_SQL, 600) is None
    assert adapter.status(2_000) is None
    conn.close()


@pytest.mark.asyncio
async def test_refresh_message_facts_once_respects_zero_budget() -> None:
    conn = _make_db()
    reactions = _ReactionFreshener()
    read_receipts = _ReadReceiptGateway()

    try:
        result = await refresh_message_facts_once(
            MessageFactRefreshDeps(
                conn,
                cast(ReactionFreshener, reactions),
                cast(TelegramReadReceiptGateway, read_receipts),
            ),
            _policy(reaction_max=0, read_at_max=0),
            now=2_000,
            shutdown_event=asyncio.Event(),
        )
    finally:
        conn.close()

    assert result.reaction_refreshed == 0
    assert reactions.calls == []
    assert read_receipts.calls == []
