"""Real guarded writes and rejection of foreign unfinished work."""

import asyncio
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

import pytest

from mcp_telegram.access_lifecycle import set_access_lost
from mcp_telegram.history_enrollment import enable_history
from mcp_telegram.message_fact_refresh import _claim_reaction_pages
from mcp_telegram.reactions.contracts import ReactionAggregate, ReactionDetailFetchResult, ReactionDetailPage
from mcp_telegram.reactions.detail import ReactionDetailRefresher
from mcp_telegram.reactions.persistence import apply_aggregate_observation
from mcp_telegram.scheduled_messages import _record_retry, upsert_scheduled_message
from mcp_telegram.sync_db import _open_sync_db, ensure_sync_schema
from mcp_telegram.sync_transactions import enable_runtime_writes, write_transaction
from mcp_telegram.telegram_fact_queries import persist_read_at
from tests.test_scheduled_messages import _message

Operation = Literal["access", "history", "claim", "read_date", "scheduled", "reaction"]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    connection = _open_sync_db(path)
    connection.execute("CREATE TABLE unrelated (value INTEGER)")
    connection.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (1, 'synced')")
    connection.execute("INSERT INTO full_history_enrollment VALUES (1, 1, 'explicit', 1)")
    connection.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (1, 2, 1)")
    connection.commit()
    try:
        yield connection
    finally:
        connection.close()


def _operation(conn: sqlite3.Connection, operation: Operation) -> None:
    if operation == "access":
        set_access_lost(conn, 1, 10)
    elif operation == "history":
        enable_history(conn, 1, now=10)
    elif operation == "claim":
        _claim_reaction_pages(conn, now=10, max_pages=1, cycle_seconds=10, candidate_limit=1)
    elif operation == "read_date":
        persist_read_at(conn, 1, 2, read_at=5, checked_at=10, status="complete")
    elif operation == "scheduled":
        _record_retry(conn, 20, "transient")
    else:
        ReactionDetailRefresher(conn, _Gateway(conn))._persist_failure(
            1, 2, 1, expected_status="stale", expected_offset=None, staged_count=0, failure=None, when=10
        )


@pytest.mark.parametrize("operation", ["access", "history", "claim", "read_date", "scheduled", "reaction"])
def test_owner_rejection_preserves_foreign_work(conn: sqlite3.Connection, operation: Operation) -> None:
    conn.execute("INSERT INTO unrelated VALUES (7)")
    with pytest.raises(RuntimeError):
        _operation(conn, operation)
    assert conn.in_transaction
    assert conn.execute("SELECT value FROM unrelated").fetchall() == [(7,)]
    conn.rollback()
    assert conn.execute("SELECT value FROM unrelated").fetchall() == []


@pytest.mark.parametrize("operation", ["access", "history", "read_date", "scheduled"])
def test_standalone_units_restore_runtime_guard(conn: sqlite3.Connection, operation: Operation) -> None:
    enable_runtime_writes(conn)
    _operation(conn, operation)
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("INSERT INTO unrelated VALUES (7)")


class _Gateway:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.calls = 0

    async def fetch_reaction_page(
        self, entity: object, message_id: int, *, offset: str | None, limit: int
    ) -> ReactionDetailFetchResult:
        assert not self.conn.in_transaction
        assert self.conn.execute("PRAGMA query_only").fetchone() == (1,)
        self.calls += 1
        await asyncio.sleep(0)
        return ReactionDetailFetchResult(page=ReactionDetailPage((), None))


@pytest.mark.asyncio
async def test_reaction_rpc_releases_writer_and_restores_guard(conn: sqlite3.Connection) -> None:
    enable_runtime_writes(conn)
    with write_transaction(conn):
        apply_aggregate_observation(
            conn, 1, 2, [ReactionAggregate("👍", 1)], source="history", observed_at=1, observation_sequence=1
        )
    gateway = _Gateway(conn)
    result = await ReactionDetailRefresher(conn, gateway).refresh_one(1, 2, 1, entity=1, now=10)
    assert result.status == "complete"
    assert gateway.calls == 1
    assert not conn.in_transaction
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)


def test_scheduled_leaf_requires_owned_parent_and_never_commits_it(conn: sqlite3.Connection) -> None:
    enable_runtime_writes(conn)
    with pytest.raises(RuntimeError, match="owned"):
        upsert_scheduled_message(conn, 1, _message(3), now=10)
    with pytest.raises(RuntimeError, match="abort parent"):
        with write_transaction(conn):
            conn.execute("INSERT INTO unrelated VALUES (7)")
            upsert_scheduled_message(conn, 1, _message(3), now=10)
            assert conn.in_transaction
            raise RuntimeError("abort parent")
    assert conn.execute("SELECT value FROM unrelated").fetchall() == []
    assert conn.execute("SELECT message_id FROM scheduled_messages").fetchall() == []
    assert conn.execute("PRAGMA query_only").fetchone() == (1,)
