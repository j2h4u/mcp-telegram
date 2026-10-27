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
from sqlite_writer_helpers import inline_writer

_TEST_TIMEOUT_S = 120.0
_POLICY = ActivityHotSweepConfig(jitter_max_seconds=0)


def _selected_budget(adapter: HotActivityDemandAdapter) -> RpcAttemptBudget:
    status = adapter.status(time.time())
    assert status is not None and status.selected is not None
    return RpcAttemptBudget(limit=1, selected=status.selected)


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

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
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
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )

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
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )

        await adapter.run_slice(_selected_budget(adapter))

        state = _get_state(conn, dialog_id)
        resume = cast(
            tuple[int | None, int | None] | None,
            conn.execute(
                "SELECT hot_page_offset_id, hot_window_max_id FROM activity_dialog_state WHERE dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        assert state["hot_cursor"] == 10
        assert resume == (min(first_page), max(first_page))

        restarted = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )
        await restarted.run_slice(_selected_budget(restarted))

        state = _get_state(conn, dialog_id)
        resume = cast(
            tuple[int | None, int | None] | None,
            conn.execute(
                "SELECT hot_page_offset_id, hot_window_max_id FROM activity_dialog_state WHERE dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        assert call_log[dialog_id] == [(0, 10), (min(first_page), 10)]
        assert state["hot_cursor"] == max(first_page)
        assert resume == (None, None)


@pytest.mark.asyncio
async def test_hot_adapter_runs_exact_excluded_page_when_enrollment_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _make_db() as conn:
        first_id, second_id = 100120, 100121
        _enroll(conn, first_id, hot_cursor=10)
        _enroll(conn, second_id, hot_cursor=20)
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES('activity_working_set_phase','supergroups')"
        )
        conn.execute("INSERT OR REPLACE INTO activity_sync_state(key,value) VALUES('activity_working_set_cursor','')")
        conn.commit()
        call_log = _patch_sweep(monkeypatch, {second_id: [_make_sweep_result([21])]})
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )

        enrollment = adapter.status(float(time.time()))
        assert enrollment is not None and enrollment.selected is not None
        assert enrollment.selected.key.target == "enrollment"
        first_page = adapter.status(float(time.time()), excluded=frozenset({enrollment.selected}))
        assert first_page is not None and first_page.selected is not None
        assert first_page.selected.key.target == f"peer:{first_id}"
        second_page = adapter.status(float(time.time()), excluded=frozenset({enrollment.selected, first_page.selected}))
        assert second_page is not None and second_page.selected is not None
        assert second_page.selected.key.target == f"peer:{second_id}"

        await adapter.run_slice(RpcAttemptBudget(limit=1, selected=second_page.selected))

        assert call_log == {second_id: [(0, 20)]}


@pytest.mark.asyncio
async def test_empty_hot_cycles_receive_new_business_revisions(monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_db() as conn:
        dialog_id = 100130
        _enroll(conn, dialog_id, hot_cursor=10)
        conn.execute("UPDATE activity_dialog_state SET hot_next_due_at=100 WHERE dialog_id=?", (dialog_id,))
        conn.commit()
        calls = _patch_sweep(monkeypatch, {dialog_id: [_make_sweep_result([]), _make_sweep_result([])]})
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr("mcp_telegram.activity_hot_sweep.time.time", lambda: 100)
            first = adapter.status(100.0)
            assert first is not None and first.selected is not None
            await adapter.run_slice(RpcAttemptBudget(limit=1, selected=first.selected))
        conn.execute("UPDATE activity_dialog_state SET hot_next_due_at=90 WHERE dialog_id=?", (dialog_id,))
        conn.commit()

        with pytest.MonkeyPatch.context() as patcher:
            patcher.setattr("mcp_telegram.activity_hot_sweep.time.time", lambda: 90)
            second = adapter.status(90.0, excluded=frozenset({first.selected}))
            assert second is not None and second.selected is not None
            assert second.selected.revision != first.selected.revision
            await adapter.run_slice(RpcAttemptBudget(limit=1, selected=second.selected))

        assert conn.execute(
            "SELECT hot_last_sync_at,hot_empty_streak FROM activity_dialog_state WHERE dialog_id=?", (dialog_id,)
        ).fetchone() == (90, 2)
        assert len(calls[dialog_id]) == 2


@pytest.mark.asyncio
async def test_hot_activity_adapter_access_skip_preserves_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    with _make_db() as conn:
        dialog_id = -100100000007
        prior_cursor = 999
        _enroll(conn, dialog_id, hot_cursor=prior_cursor)
        _patch_sweep(monkeypatch, {dialog_id: [_make_sweep_result([], skip_reason=SkipReason.ACCESS_SKIP)]})
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )

        await adapter.run_slice(_selected_budget(adapter))

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
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )

        await adapter.run_slice(_selected_budget(adapter))

        state = _get_state(conn, dialog_id)
        assert state["hot_cursor"] == prior_cursor
        assert cast(int, state["hot_next_retry_at"]) > int(time.time())
        assert state["cold_status"] == "pending"
        assert state["cold_next_retry_at"] is None


@pytest.mark.asyncio
async def test_hot_sweep_includes_adjacent_id_with_exclusive_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched: list[int] = []

    async def exclusive_sweep(*args: object, min_id: int, **kwargs: object) -> SweepResult:
        del args, kwargs
        page = [message_id for message_id in [11] if message_id > min_id]
        fetched.extend(page)
        return _make_sweep_result(page)

    monkeypatch.setattr("mcp_telegram.activity_hot_sweep.sweep_peer_once", exclusive_sweep)
    with _make_db() as conn:
        dialog_id = -100100000088
        _enroll(conn, dialog_id, hot_cursor=10)
        adapter = HotActivityDemandAdapter(
            _FakeClient(), conn, asyncio.Event(), _POLICY, _TEST_TIMEOUT_S, writer=inline_writer(conn), account_id=123
        )
        await adapter.run_slice(_selected_budget(adapter))
        assert fetched == [11]
        assert _get_state(conn, dialog_id)["hot_cursor"] == 11


def test_hot_completed_cycle_advances_identity_when_new_count_does_not_advance_cursor() -> None:
    from mcp_telegram.activity_hot_sweep import _save_hot_completed_state

    with _make_db() as conn:
        dialog_id = -100100000019
        _enroll(conn, dialog_id, hot_cursor=20)
        for expected_streak in (1, 2):
            _save_hot_completed_state(
                conn, dialog_id, hot_cursor=20, completion_at=100, genuinely_new=1, policy=_POLICY
            )
            assert conn.execute(
                "SELECT hot_empty_streak,hot_last_sync_at FROM activity_dialog_state WHERE dialog_id=?", (dialog_id,)
            ).fetchone() == (expected_streak, 100)
