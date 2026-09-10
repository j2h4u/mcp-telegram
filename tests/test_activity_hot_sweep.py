"""Tests for the durable HotSweep demand adapter."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import cast

import pytest

from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter
from mcp_telegram.activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _load_dialog_state,
    _save_dialog_state,
    enroll_activity_dialog,
)
from mcp_telegram.config import ActivityHotSweepConfig
from mcp_telegram.sync_db import _apply_migrations
from mcp_telegram.telegram_demand import RpcAttemptBudget

_TEST_TIMEOUT_S = 120.0
_POLICY = ActivityHotSweepConfig(jitter_max_seconds=0)


@contextmanager
def _make_db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    try:
        yield conn
    finally:
        conn.close()


class _FakeClient:
    async def __call__(self, request: object) -> object:
        del request
        return object()


def _enroll(conn: sqlite3.Connection, dialog_id: int, *, hot_cursor: int | None = None) -> None:
    now = int(time.time())
    enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=now)
    conn.execute(
        "UPDATE activity_dialog_state SET hot_next_due_at = ? WHERE dialog_id = ?",
        (now - 1, dialog_id),
    )
    conn.commit()
    if hot_cursor is not None:
        _save_dialog_state(conn, dialog_id, hot_cursor=hot_cursor)


def _get_state(conn: sqlite3.Connection, dialog_id: int) -> dict[str, int | str | None]:
    return cast(dict[str, int | str | None], _load_dialog_state(conn, dialog_id))


def _make_sweep_result(
    ids: list[int],
    *,
    skip_reason: SkipReason = SkipReason.NONE,
    min_id: int | None = None,
) -> SweepResult:
    if not ids and skip_reason is SkipReason.NONE:
        skip_reason = SkipReason.HISTORY_FLOOR
    return SweepResult(
        fetched_ids=ids,
        persisted=len(ids),
        min_id=min(ids) if min_id is None and ids else min_id,
        max_id=max(ids) if ids else None,
        skip_reason=skip_reason,
        pages_fetched=1,
        rpc_calls=1,
        extracted=len(ids),
        genuinely_new=len(ids),
        completed=skip_reason in (SkipReason.NONE, SkipReason.HISTORY_FLOOR),
    )


def _patch_sweep(
    monkeypatch: pytest.MonkeyPatch,
    results_by_dialog: dict[int, list[SweepResult]],
) -> dict[int, list[tuple[int, int]]]:
    call_log: dict[int, list[tuple[int, int]]] = {}

    async def _fake_sweep(
        *args: object,
        offset_id: int,
        min_id: int,
        limit: int,
        timeout_s: float,
        **_kwargs: object,
    ) -> SweepResult:
        del limit, timeout_s
        _client, _conn, dialog_id = cast(tuple[object, object, int], args)
        call_log.setdefault(dialog_id, []).append((offset_id, min_id))
        queue = results_by_dialog.get(dialog_id, [])
        return queue.pop(0) if queue else _make_sweep_result([])

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", _fake_sweep)
    return call_log


def test_hot_activity_adapter_reports_retry_gated_release() -> None:
    with _make_db() as conn:
        now = int(time.time())
        dialog_id = -100100000099
        _enroll(conn, dialog_id, hot_cursor=10)
        _save_dialog_state(conn, dialog_id, hot_next_retry_at=now + 90)
        adapter = HotActivityDemandAdapter(_FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S)

        status = adapter.status(float(now))

        assert status is not None
        assert status.release_at == now + 90


@pytest.mark.asyncio
async def test_hot_activity_adapter_resumes_page_window_before_advancing_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _make_db() as conn:
        dialog_id = -100100000100
        _enroll(conn, dialog_id, hot_cursor=10)
        first_page = list(range(101, 201))
        second_page = [11, 12]
        call_log = _patch_sweep(
            monkeypatch, {dialog_id: [_make_sweep_result(first_page), _make_sweep_result(second_page)]}
        )
        adapter = HotActivityDemandAdapter(_FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S)

        await adapter.run_slice(RpcAttemptBudget(limit=1))

        state = _get_state(conn, dialog_id)
        resume = conn.execute(
            "SELECT hot_page_offset_id, hot_window_max_id FROM activity_dialog_state WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone()
        assert state["hot_cursor"] == 10
        assert resume == (min(first_page), max(first_page))

        restarted = HotActivityDemandAdapter(_FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S)
        await restarted.run_slice(RpcAttemptBudget(limit=1))

        state = _get_state(conn, dialog_id)
        resume = conn.execute(
            "SELECT hot_page_offset_id, hot_window_max_id FROM activity_dialog_state WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone()
        assert call_log[dialog_id] == [(0, 11), (min(first_page), 11)]
        assert state["hot_cursor"] == max(first_page)
        assert resume == (None, None)


@pytest.mark.asyncio
async def test_hot_activity_adapter_access_skip_preserves_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_db() as conn:
        dialog_id = -100100000007
        prior_cursor = 999
        _enroll(conn, dialog_id, hot_cursor=prior_cursor)
        _patch_sweep(monkeypatch, {dialog_id: [_make_sweep_result([], skip_reason=SkipReason.ACCESS_SKIP)]})
        adapter = HotActivityDemandAdapter(_FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S)

        await adapter.run_slice(RpcAttemptBudget(limit=1))

        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == prior_cursor
        assert cast(int, state["hot_next_retry_at"]) > int(time.time())
        assert state["cold_status"] == "pending"
        assert state["cold_next_retry_at"] is None


@pytest.mark.asyncio
async def test_hot_activity_adapter_full_page_without_min_id_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_db() as conn:
        dialog_id = -100100000017
        prior_cursor = 500
        _enroll(conn, dialog_id, hot_cursor=prior_cursor)
        fetched_ids = list(range(501, 601))
        _patch_sweep(
            monkeypatch,
            {
                dialog_id: [
                    SweepResult(
                        fetched_ids=fetched_ids,
                        persisted=len(fetched_ids),
                        min_id=None,
                        max_id=max(fetched_ids),
                        skip_reason=SkipReason.NONE,
                        pages_fetched=1,
                        rpc_calls=1,
                        extracted=len(fetched_ids),
                        genuinely_new=len(fetched_ids),
                    )
                ]
            },
        )
        adapter = HotActivityDemandAdapter(_FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S)

        await adapter.run_slice(RpcAttemptBudget(limit=1))

        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == prior_cursor
        assert cast(int, state["hot_next_retry_at"]) > int(time.time())
        assert state["cold_status"] == "pending"
        assert state["cold_next_retry_at"] is None
