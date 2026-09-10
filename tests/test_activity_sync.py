"""Tests for durable archive demand adapters."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypedDict, Unpack, cast

import pytest
from telethon.tl.types import PeerUser

from mcp_telegram import activity_sync
from mcp_telegram.activity_sync import ArchiveBackfillDemandAdapter, ArchiveIncrementalDemandAdapter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, current_rpc_scope

_TEST_TIMEOUT_S = 0.05


@dataclass
class FakeReplies:
    replies: int = 0


@dataclass
class FakeMessage:
    id: int
    date: datetime
    message: str
    peer_id: object
    replies: object | None = None
    reactions: object | None = None
    out: bool = True


class _SearchEntityLike(Protocol):
    id: int
    first_name: str | None
    last_name: str | None
    title: str | None
    username: str | None


@dataclass
class FakeSearchResult:
    messages: list[FakeMessage]
    users: list[_SearchEntityLike] = field(default_factory=list)
    chats: list[_SearchEntityLike] = field(default_factory=list)
    count: int | None = None


class _MsgKwargs(TypedDict, total=False):
    text: str
    replies: int
    out: bool


class _FakeClient:
    def __init__(self, batches: list[FakeSearchResult]) -> None:
        self._batches = list(batches)
        self.scopes: list[TelegramRpcScope] = []

    async def __call__(self, request: object) -> FakeSearchResult:
        del request
        self.scopes.append(current_rpc_scope())
        return self._batches.pop(0) if self._batches else FakeSearchResult(messages=[])

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()


def _make_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "sync.db"
    ensure_sync_schema(db_path)
    return sqlite3.connect(db_path)


def _msg(msg_id: int, user_id: int, ts: int, **kwargs: Unpack[_MsgKwargs]) -> FakeMessage:
    return FakeMessage(
        id=msg_id,
        date=datetime.fromtimestamp(ts, tz=UTC),
        message=kwargs.get("text", "hi"),
        peer_id=PeerUser(user_id=user_id),
        replies=FakeReplies(replies=kwargs.get("replies", 0)) if kwargs.get("replies", 0) else None,
        out=kwargs.get("out", True),
    )


@dataclass
class _TrimMessage:
    id: int
    date: datetime | None


def test_trim_incremental_batch_keeps_message_at_min_date() -> None:
    min_date = 1_700_000_000
    in_window, past_window = activity_sync._trim_incremental_batch(
        [
            _TrimMessage(2, datetime.fromtimestamp(min_date + 1, tz=UTC)),
            _TrimMessage(1, datetime.fromtimestamp(min_date, tz=UTC)),
        ],
        min_date,
    )
    assert [message.id for message in in_window] == [2, 1]
    assert past_window is False


def test_trim_incremental_batch_stops_at_first_message_before_window() -> None:
    min_date = 1_700_000_000
    in_window, past_window = activity_sync._trim_incremental_batch(
        [
            _TrimMessage(3, datetime.fromtimestamp(min_date + 2, tz=UTC)),
            _TrimMessage(2, datetime.fromtimestamp(min_date - 1, tz=UTC)),
            _TrimMessage(1, datetime.fromtimestamp(min_date + 1, tz=UTC)),
        ],
        min_date,
    )
    assert [message.id for message in in_window] == [3]
    assert past_window is True


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = _make_db(tmp_path)
    try:
        yield connection
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_archive_backfill_adapter_commits_one_page_with_precise_scope(conn: sqlite3.Connection) -> None:
    client = _FakeClient([FakeSearchResult(messages=[_msg(100, 42, 1_700_000_100)]), FakeSearchResult(messages=[])])
    budget = RpcAttemptBudget(limit=1)
    adapter = ArchiveBackfillDemandAdapter(client, conn, asyncio.Event(), _TEST_TIMEOUT_S)

    assert adapter.status(1_700_000_000.0) is not None
    await adapter.run_slice(budget)
    state = dict(
        cast(list[tuple[str, str | None]], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    )
    assert state["backfill_offset_id"] == "100"
    assert state["backfill_complete"] == "0"
    assert client.scopes[0].demand_kind is DemandKind.ARCHIVE_BACKFILL
    assert client.scopes[0].attempt_budget is budget

    await adapter.run_slice(RpcAttemptBudget(limit=1))
    assert adapter.status(1_700_000_000.0) is None


@pytest.mark.asyncio
async def test_archive_incremental_adapter_resumes_from_key_value_state(conn: sqlite3.Connection) -> None:
    last_sync_at = int(time.time()) - 7_200
    with conn:
        conn.execute("UPDATE activity_sync_state SET value='1' WHERE key='backfill_complete'")
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES ('last_sync_at', ?)",
            (str(last_sync_at),),
        )
    client = _FakeClient([FakeSearchResult(messages=[_msg(12, 42, last_sync_at + 30)]), FakeSearchResult(messages=[])])
    first_budget = RpcAttemptBudget(limit=1)
    adapter = ArchiveIncrementalDemandAdapter(client, conn, asyncio.Event(), 3_600.0, _TEST_TIMEOUT_S)

    initial = adapter.status(float(last_sync_at))
    assert initial is not None
    assert initial.release_at == last_sync_at + 3_600
    await adapter.run_slice(first_budget)
    state = dict(
        cast(list[tuple[str, str | None]], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    )
    assert state["incremental_min_date"] == str(last_sync_at - 60)
    assert state["incremental_offset_id"] == "12"
    assert client.scopes[0].demand_kind is DemandKind.ARCHIVE_INCREMENTAL
    assert client.scopes[0].attempt_budget is first_budget

    await adapter.run_slice(RpcAttemptBudget(limit=1))
    final_state = dict(
        cast(list[tuple[str, str | None]], conn.execute("SELECT key, value FROM activity_sync_state").fetchall())
    )
    assert "incremental_min_date" not in final_state
    assert "incremental_offset_id" not in final_state
    assert int(final_state["last_sync_at"] or 0) > last_sync_at
