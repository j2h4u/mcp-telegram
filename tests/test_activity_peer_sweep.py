"""Tests for activity_peer_sweep.py.

It covers the per-peer self-search and durable enrollment substrate.

Note: Phase-53 durable backoff tests and helpers were removed in Phase 54
(plan 04). The new event-driven resolver model is tested in the
Phase 54 plan 02–04 test suite.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from typing import cast

import pytest

from mcp_telegram.activity_peer_sweep import (
    _DIALOG_STATE_COLUMNS,
    _PACING,
    PeerSweepRequest,
    SkipReason,
    SweepResult,
    _load_dialog_state,
    _save_dialog_state,
    enroll_activity_dialog,
    sweep_peer_once,
)
from mcp_telegram.sync_db import _apply_migrations

_TEST_TIMEOUT_S = 120.0

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


class _FakeClient:
    """Minimal fake client for sweep_peer_once tests."""

    async def get_input_entity(self, dialog_id: int) -> object:
        del dialog_id
        return object()

    async def __call__(self, request: object) -> object:
        del request
        raise AssertionError("_FakeClient.__call__ should not be invoked by the sweep test")


class _FakeSweepMessage:
    def __init__(self, msg_id: int, peer_id: object | None) -> None:
        self.id = msg_id
        self.peer_id = peer_id


class _FakeSweepResult:
    def __init__(self, messages: object) -> None:
        self.messages = messages


# ---------------------------------------------------------------------------
# sweep_peer_once: exit-path coverage and persistence gating
# ---------------------------------------------------------------------------


def test_sweep_peer_once_resolve_none_returns_access_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing peer resolution returns ACCESS_SKIP and never calls SearchRequest."""
    with closing(_make_db()) as conn:
        calls: list[object] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object | None:
            del client, dialog_id
            return None

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> object:
            del client, request, timeout_s
            calls.append(object())
            raise AssertionError("call_with_timeout must not be called when resolve_input_peer returns None")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                PeerSweepRequest(
                    client=_FakeClient(),
                    conn=conn,
                    dialog_id=123,
                    offset_id=7,
                    min_id=3,
                    limit=25,
                    timeout_s=_TEST_TIMEOUT_S,
                )
            )
        )

        assert calls == []
        assert result.rpc_calls == 1, "resolution attempt is the only governed RPC"
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )


def test_sweep_peer_once_resolution_error_reports_rpc_and_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(_make_db()) as conn:

        async def failed_resolution(client: object, dialog_id: int) -> object:
            del client, dialog_id
            raise RuntimeError("resolver unavailable")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", failed_resolution)
        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(), conn=conn, dialog_id=123, offset_id=0, min_id=0, limit=10, timeout_s=1
            )
        )
        assert result.skip_reason is SkipReason.ACCESS_SKIP
        assert result.rpc_calls == 1
        assert result.pages_fetched == 0
        assert not result.completed


def test_sweep_peer_once_search_error_reports_two_rpcs(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(_make_db()) as conn:

        async def resolved(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def failed_search(client: object, request: object, *, timeout_s: float) -> object:
            del client, request, timeout_s
            raise RuntimeError("search unavailable")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", resolved)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", failed_search)
        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(), conn=conn, dialog_id=123, offset_id=0, min_id=0, limit=10, timeout_s=1
            )
        )
        assert result.rpc_calls == 2
        assert result.pages_fetched == 0
        assert not result.completed


def test_sweep_peer_once_extraction_error_reports_result_page(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(_make_db()) as conn:

        async def resolved(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def searched(client: object, request: object, *, timeout_s: float) -> _FakeSweepResult:
            del client, request, timeout_s
            return _FakeSweepResult(messages=[_FakeSweepMessage(8, peer_id="keep")])

        def failed_extract(request: object, batch: object) -> tuple[list[object], frozenset[tuple[int, int]]]:
            del request, batch
            raise RuntimeError("bad message")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", resolved)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", searched)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep._extract_sweep_messages", failed_extract)
        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(), conn=conn, dialog_id=123, offset_id=0, min_id=0, limit=10, timeout_s=1
            )
        )
        assert result.rpc_calls == 2
        assert result.pages_fetched == 1
        assert not result.completed


def test_sweep_peer_once_persistence_error_reports_result_page(monkeypatch: pytest.MonkeyPatch) -> None:
    with closing(_make_db()) as conn:

        async def resolved(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def searched(client: object, request: object, *, timeout_s: float) -> _FakeSweepResult:
            del client, request, timeout_s
            return _FakeSweepResult(messages=[_FakeSweepMessage(8, peer_id="keep")])

        def extracted(request: object, batch: object) -> tuple[list[tuple[int, str]], frozenset[tuple[int, int]]]:
            del request, batch
            return [(101, "msg")], frozenset({(101, 8)})

        def failed_insert(conn: sqlite3.Connection, rows: list[tuple[int, str]], **_kwargs: object) -> None:
            del conn, rows
            raise RuntimeError("database unavailable")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", resolved)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", searched)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep._extract_sweep_messages", extracted)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.insert_messages_with_fts", failed_insert)
        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(), conn=conn, dialog_id=123, offset_id=0, min_id=0, limit=10, timeout_s=1
            )
        )
        assert result.rpc_calls == 2
        assert result.pages_fetched == 1
        assert not result.completed


def test_sweep_peer_once_floodwait_reports_seconds(monkeypatch: pytest.MonkeyPatch) -> None:
    """FloodWait becomes a non-sleeping FLOOD_WAIT result with seconds preserved."""
    from telethon.tl.functions.messages import SearchRequest

    from mcp_telegram.flood import TelegramRpcThrottled

    with closing(_make_db()) as conn:
        captured: dict[str, object] = {}

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> object:
            del client
            captured["timeout_s"] = timeout_s
            captured["request"] = request
            raise TelegramRpcThrottled(retry_after_seconds=37)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=456,
                offset_id=11,
                min_id=5,
                limit=50,
                timeout_s=_TEST_TIMEOUT_S,
            )
        )

        assert "request" in captured
        assert captured["timeout_s"] == _TEST_TIMEOUT_S
        request = cast(SearchRequest, captured["request"])
        assert request.offset_id == 11
        assert request.min_id == 5
        assert request.limit == 50
        assert result.rpc_calls == 2, "resolution and reached search each count once"
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.FLOOD_WAIT,
            flood_wait_seconds=37,
        )


def test_sweep_peer_once_success_invokes_pacing_sleep(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A successful SearchRequest should apply the fixed post-RPC pause."""
    with closing(_make_db()) as conn:
        sleep_calls: list[float] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> _FakeSweepResult:
            del client, request, timeout_s
            return _FakeSweepResult(messages=[_FakeSweepMessage(8, peer_id="keep")])

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        def fake_extract_dialog_id(message: _FakeSweepMessage) -> int | None:
            return 101 if message.peer_id == "keep" else None

        def fake_extract_message_row(dialog_id: int, message: _FakeSweepMessage) -> tuple[int, str]:
            return (dialog_id, f"msg-{message.id}")

        def fake_insert_messages_with_fts(
            conn: sqlite3.Connection, rows: list[tuple[int, str]], **_kwargs: object
        ) -> None:
            del conn, rows

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_dialog_id", fake_extract_dialog_id)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_message_row", fake_extract_message_row)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.insert_messages_with_fts", fake_insert_messages_with_fts)

        with caplog.at_level(logging.DEBUG, logger="mcp_telegram.activity_peer_sweep"):
            result = asyncio.run(
                sweep_peer_once(
                    client=_FakeClient(),
                    conn=conn,
                    dialog_id=333,
                    offset_id=13,
                    min_id=6,
                    limit=30,
                    timeout_s=_TEST_TIMEOUT_S,
                )
            )

        assert sleep_calls == [_PACING.search.success_s]
        assert result.rpc_calls == 2
        assert any(
            "sweep_peer_once_done" in record.message
            and "rpc_duration_s=" in record.message
            and f"pacing_s={_PACING.search.success_s:.3f}" in record.message
            for record in caplog.records
        )
        assert result == SweepResult(
            fetched_ids=[8],
            persisted=1,
            min_id=8,
            max_id=8,
            skip_reason=SkipReason.NONE,
        )


def test_sweep_peer_once_floodwait_does_not_invoke_pacing_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """FloodWait should return immediately without the success pacing sleep."""
    from mcp_telegram.flood import TelegramRpcThrottled

    with closing(_make_db()) as conn:
        sleep_calls: list[float] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> object:
            del client, request, timeout_s
            raise TelegramRpcThrottled(retry_after_seconds=37)

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.asyncio.sleep", fake_sleep)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=444,
                offset_id=11,
                min_id=5,
                limit=50,
                timeout_s=_TEST_TIMEOUT_S,
            )
        )

        assert sleep_calls == []
        assert result.rpc_calls == 2, "a SearchRequest FloodWait still counts its attempt"
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.FLOOD_WAIT,
            flood_wait_seconds=37,
        )


def test_sweep_peer_once_latched_throttling_propagates_without_retry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A latched throttle stops account-wide work instead of becoming ACCESS_SKIP."""
    from mcp_telegram.flood import TelegramRpcThrottled

    with closing(_make_db()) as conn:
        dialog_id = 445
        enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=1000)

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> object:
            del client, request, timeout_s
            raise TelegramRpcThrottled(latched=True, detail="account gate latched")

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        with pytest.raises(TelegramRpcThrottled, match="account gate latched"):
            asyncio.run(
                sweep_peer_once(
                    client=_FakeClient(),
                    conn=conn,
                    dialog_id=dialog_id,
                    offset_id=0,
                    min_id=0,
                    limit=50,
                    timeout_s=_TEST_TIMEOUT_S,
                )
            )

        state = cast(
            tuple[int | None, int | None] | None,
            conn.execute(
                "SELECT hot_next_retry_at, cold_next_retry_at FROM activity_dialog_state WHERE dialog_id = ?",
                (dialog_id,),
            ).fetchone(),
        )
        assert state == (None, None)


def test_sweep_peer_once_timeout_returns_access_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    """TimeoutError is treated as ACCESS_SKIP, not history-floor completion."""
    with closing(_make_db()) as conn:
        called = False

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> object:
            nonlocal called
            del client, request, timeout_s
            called = True
            raise TimeoutError

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=789,
                offset_id=4,
                min_id=2,
                limit=10,
                timeout_s=_TEST_TIMEOUT_S,
            )
        )

        assert called is True
        assert result.rpc_calls == 2, "a timed out SearchRequest still counts its attempt"
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )


def test_sweep_peer_once_access_lost_marks_dialog_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Permanent Telegram access loss is a structured ACCESS_SKIP, not a loop traceback."""
    from telethon.errors import ChannelPrivateError

    with closing(_make_db()) as conn:
        dialog_id = -100789000001
        enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=int(time.time()))
        sleep_calls: list[float] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> object:
            del client, request, timeout_s
            raise ChannelPrivateError(request=None)

        async def fake_sleep(seconds: float) -> None:
            sleep_calls.append(seconds)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.asyncio.sleep", fake_sleep)

        with caplog.at_level(logging.INFO, logger="mcp_telegram.access_lifecycle"):
            result = asyncio.run(
                sweep_peer_once(
                    client=_FakeClient(),
                    conn=conn,
                    dialog_id=dialog_id,
                    offset_id=4,
                    min_id=2,
                    limit=10,
                    timeout_s=_TEST_TIMEOUT_S,
                )
            )

        synced_row = cast(
            tuple[str, int | None] | None,
            conn.execute(
                "SELECT status, access_lost_at FROM synced_dialogs WHERE dialog_id = ?",
                (dialog_id,),
            ).fetchone(),
        )
        dialog_row = cast(
            tuple[int] | None,
            conn.execute("SELECT hidden FROM dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
        )
        access_lost_logs = [record for record in caplog.records if record.message.startswith("access_lost ")]

        assert sleep_calls == []
        assert result.rpc_calls == 2, "an access-lost SearchRequest still counts its attempt"
        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )
        assert synced_row is not None
        assert synced_row[0] == "access_lost"
        assert synced_row[1] is not None
        assert dialog_row == (1,)
        assert access_lost_logs
        assert all(record.exc_info is None for record in access_lost_logs)


def test_sweep_peer_once_empty_batch_is_history_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable peer with no messages returns HISTORY_FLOOR."""
    with closing(_make_db()) as conn:

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> _FakeSweepResult:
            del client, request, timeout_s
            return _FakeSweepResult(messages=[])

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=111,
                offset_id=9,
                min_id=1,
                limit=20,
                timeout_s=_TEST_TIMEOUT_S,
            )
        )

        assert result == SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.HISTORY_FLOOR,
        )
        assert result.rpc_calls == 2


def test_sweep_peer_once_persists_only_extractable_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only messages with a resolvable dialog_id are extracted and persisted."""
    with closing(_make_db()) as conn:
        inserted: list[list[tuple[int, str]]] = []

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> _FakeSweepResult:
            del client, request, timeout_s
            return _FakeSweepResult(
                messages=[
                    _FakeSweepMessage(8, peer_id="keep"),
                    _FakeSweepMessage(3, peer_id="drop"),
                    _FakeSweepMessage(5, peer_id="keep"),
                ]
            )

        def fake_extract_dialog_id(message: _FakeSweepMessage) -> int | None:
            return 101 if message.peer_id == "keep" else None

        def fake_extract_message_row(dialog_id: int, message: _FakeSweepMessage) -> tuple[int, str]:
            return (dialog_id, f"msg-{message.id}")

        def fake_insert_messages_with_fts(
            conn: sqlite3.Connection, rows: list[tuple[int, str]], **_kwargs: object
        ) -> None:
            del conn
            inserted.append(rows)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_dialog_id", fake_extract_dialog_id)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_message_row", fake_extract_message_row)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.insert_messages_with_fts", fake_insert_messages_with_fts)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=222,
                offset_id=13,
                min_id=6,
                limit=30,
                timeout_s=_TEST_TIMEOUT_S,
            )
        )

        assert inserted == [[(101, "msg-8"), (101, "msg-5")]]
        assert result == SweepResult(
            fetched_ids=[8, 3, 5],
            persisted=2,
            min_id=3,
            max_id=8,
            skip_reason=SkipReason.NONE,
        )


def test_sweep_peer_once_counts_unique_genuinely_new_keys_before_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Existing rows are replacements, while each missing key counts only once."""
    with closing(_make_db()) as conn:
        inserted: list[list[tuple[int, str]]] = []
        conn.execute("INSERT INTO messages(dialog_id, message_id, sent_at) VALUES (101, 8, 1)")
        conn.commit()

        async def fake_resolve_input_peer(client: object, dialog_id: int) -> object:
            del client, dialog_id
            return object()

        async def fake_call_with_timeout(client: object, request: object, *, timeout_s: float) -> _FakeSweepResult:
            del client, request, timeout_s
            return _FakeSweepResult(
                messages=[
                    _FakeSweepMessage(8, peer_id="keep"),
                    _FakeSweepMessage(8, peer_id="keep"),
                    _FakeSweepMessage(5, peer_id="keep"),
                ]
            )

        def fake_extract_dialog_id(message: _FakeSweepMessage) -> int | None:
            return 101 if message.peer_id == "keep" else None

        def fake_extract_message_row(dialog_id: int, message: _FakeSweepMessage) -> tuple[int, str]:
            return (dialog_id, f"msg-{message.id}")

        def fake_insert_messages_with_fts(
            conn: sqlite3.Connection, rows: list[tuple[int, str]], **_kwargs: object
        ) -> None:
            del conn
            inserted.append(rows)

        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.resolve_input_peer", fake_resolve_input_peer)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.call_with_timeout", fake_call_with_timeout)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_dialog_id", fake_extract_dialog_id)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.extract_message_row", fake_extract_message_row)
        monkeypatch.setattr("mcp_telegram.activity_peer_sweep.insert_messages_with_fts", fake_insert_messages_with_fts)

        result = asyncio.run(
            sweep_peer_once(
                client=_FakeClient(),
                conn=conn,
                dialog_id=222,
                offset_id=13,
                min_id=6,
                limit=30,
                timeout_s=_TEST_TIMEOUT_S,
            )
        )

        assert inserted == [[(101, "msg-8"), (101, "msg-5")]]
        assert result.genuinely_new == 1
        assert result.genuinely_new_keys == frozenset({(101, 5)})
        assert result.pages_fetched == 1
        assert result.rpc_calls == 2


# ---------------------------------------------------------------------------
# enroll_activity_dialog: ON CONFLICT doesn't overwrite cursor columns
# ---------------------------------------------------------------------------


def test_enroll_does_not_overwrite_cursors():
    """enroll_activity_dialog ON CONFLICT must not clobber per-tier cursor state."""
    with closing(_make_db()) as conn:
        peer_id = -100000000001

        # First enrollment
        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        # Simulate scheduler setting per-tier cursor state
        conn.execute(
            "UPDATE activity_dialog_state SET hot_cursor = 999, cold_status = 'running' WHERE dialog_id = ?",
            (peer_id,),
        )
        conn.commit()

        # Re-enroll while preserving the active peer's durable cursors.
        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=2000)

        row = cast(
            tuple[int, str] | None,
            conn.execute(
                "SELECT hot_cursor, cold_status FROM activity_dialog_state WHERE dialog_id = ?",
                (peer_id,),
            ).fetchone(),
        )
        assert row is not None
        assert row[0] == 999, "hot_cursor must be preserved across re-enrollment"
        assert row[1] == "running", "cold_status must be preserved across re-enrollment"


# ---------------------------------------------------------------------------
# enroll_activity_dialog: synced_dialogs INSERT OR IGNORE never downgrades
# ---------------------------------------------------------------------------


def test_enroll_never_downgrades_synced_dialogs():
    """enroll_activity_dialog must not downgrade an existing higher-status synced_dialogs row."""
    with closing(_make_db()) as conn:
        peer_id = -100000000002

        # Pre-insert with a higher status
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status) VALUES (?, 'synced')",
            (peer_id,),
        )
        conn.commit()

        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        row = cast(
            tuple[str] | None,
            conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (peer_id,)).fetchone(),
        )
        assert row is not None
        assert row[0] == "synced", f"Status must not be downgraded from 'synced' to 'own_only', got {row[0]!r}"


# ---------------------------------------------------------------------------
# _load_dialog_state / _save_dialog_state
# ---------------------------------------------------------------------------


def test_save_and_load_dialog_state():
    """_save_dialog_state writes whitelisted columns; _load_dialog_state reads them back."""
    with closing(_make_db()) as conn:
        peer_id = -100000000003
        enroll_activity_dialog(conn, peer_id, "supergroup")

        _save_dialog_state(conn, peer_id, hot_cursor=42, cold_status="running")
        state = _load_dialog_state(conn, peer_id)

        assert state["hot_cursor"] == 42
        assert state["cold_status"] == "running"


def test_save_dialog_state_rejects_unknown_columns():
    """_save_dialog_state raises ValueError for unknown column names."""
    with closing(_make_db()) as conn:
        peer_id = -100000000004
        enroll_activity_dialog(conn, peer_id, "supergroup")

        with pytest.raises(ValueError, match="unknown fields"):
            _save_dialog_state(conn, peer_id, nonexistent_col=1)


# ---------------------------------------------------------------------------
# SweepResult.hit_floor contract
# ---------------------------------------------------------------------------


def test_hit_floor_only_for_history_floor():
    """hit_floor is True ONLY for HISTORY_FLOOR, False for all other SkipReasons."""
    for reason in SkipReason:
        r = SweepResult(fetched_ids=[], persisted=0, min_id=None, max_id=None, skip_reason=reason)
        expected = reason is SkipReason.HISTORY_FLOOR
        assert r.hit_floor == expected, f"hit_floor expected {expected} for {reason!r}, got {r.hit_floor}"


# ---------------------------------------------------------------------------
# WR-01: allowlist/DDL drift guard for _save_dialog_state
# ---------------------------------------------------------------------------


def test_dialog_state_column_allowlist_matches_table():
    """_DIALOG_STATE_COLUMNS must stay in sync with the real activity_dialog_state
    columns. _save_dialog_state interpolates these names into SQL, so a drifted
    allowlist either fails at runtime (name not in table) or silently permits
    updating an identity/bookkeeping column. Guard both directions.
    """
    with closing(_make_db()) as conn:
        real_cols = {
            row[1]
            for row in cast(
                list[tuple[int, str, str, int, str | None, int]],
                conn.execute("PRAGMA table_info(activity_dialog_state)").fetchall(),
            )
        }
        # Every allowlisted column must exist in the table.
        missing = _DIALOG_STATE_COLUMNS - real_cols
        assert not missing, f"allowlist references non-existent columns: {missing}"
        # The allowlist must NOT include identity / bookkeeping columns — those are
        # never updated through _save_dialog_state.
        forbidden = {"dialog_id", "source", "created_at", "updated_at", "last_activity_at"}
        leaked = _DIALOG_STATE_COLUMNS & forbidden
        assert not leaked, f"allowlist must not expose identity/bookkeeping columns: {leaked}"


# ---------------------------------------------------------------------------
# WR-03: enrollment provenance precedence (no supergroup → linked_chat downgrade)
# ---------------------------------------------------------------------------


def test_enroll_does_not_downgrade_supergroup_source():
    """A peer enrolled as 'supergroup' keeps that provenance even if a later
    trace-driven call tries to enroll it as 'linked_chat'. Other sources refresh
    normally (including linked_chat -> supergroup upgrade).
    """
    with closing(_make_db()) as conn:
        peer = -100123123123

        # Direct supergroup membership first.
        enroll_activity_dialog(conn, peer, "supergroup", last_activity_at=1000)
        assert _source_of(conn, peer) == "supergroup"

        # A trace later resolves the same peer as a channel's linked discussion group.
        enroll_activity_dialog(conn, peer, "linked_chat", last_activity_at=2000)
        assert _source_of(conn, peer) == "supergroup", "must not downgrade supergroup → linked_chat"

        # Upgrade path still works: a linked_chat peer found to be a direct supergroup.
        other = -100456456456
        enroll_activity_dialog(conn, other, "linked_chat", last_activity_at=1000)
        assert _source_of(conn, other) == "linked_chat"
        enroll_activity_dialog(conn, other, "supergroup", last_activity_at=2000)
        assert _source_of(conn, other) == "supergroup", "linked_chat → supergroup upgrade must apply"


def _source_of(conn: sqlite3.Connection, dialog_id: int) -> str | None:
    row = cast(
        tuple[str] | None,
        conn.execute("SELECT source FROM activity_dialog_state WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    return row[0] if row else None


# ---------------------------------------------------------------------------
# enroll_activity_dialog: thin dialogs row (needs_refresh=1) — Bug #1 fix
# ---------------------------------------------------------------------------


def test_enroll_creates_thin_dialogs_row():
    """enroll_activity_dialog must create a thin dialogs row with needs_refresh=1, hidden=0,
    name IS NULL for a peer that has no prior dialogs entry."""
    with closing(_make_db()) as conn:
        peer_id = -100777000001

        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        row = cast(
            tuple[int, int, str | None] | None,
            conn.execute(
                "SELECT needs_refresh, hidden, name FROM dialogs WHERE dialog_id = ?",
                (peer_id,),
            ).fetchone(),
        )
        assert row is not None, "enroll_activity_dialog must create a dialogs row"
        assert row[0] == 1, f"needs_refresh must be 1, got {row[0]!r}"
        assert row[1] == 0, f"hidden must be 0, got {row[1]!r}"
        assert row[2] is None, f"name must be NULL until reconciliation fills it, got {row[2]!r}"


def test_enroll_does_not_clobber_resolved_dialog():
    """enroll_activity_dialog must NOT overwrite an already-resolved dialogs row.
    INSERT OR IGNORE means the existing row (name, type, needs_refresh) is unchanged."""
    with closing(_make_db()) as conn:
        peer_id = -100777000002

        # Pre-insert a fully resolved dialogs row
        conn.execute(
            "INSERT INTO dialogs (dialog_id, name, type, needs_refresh, snapshot_at,"
            " archived, pinned, hidden, unread_mentions_count, unread_reactions_count)"
            " VALUES (?, 'Resolved Chat', 'user', 0, 1700000000, 0, 0, 0, 0, 0)",
            (peer_id,),
        )
        conn.commit()

        enroll_activity_dialog(conn, peer_id, "supergroup", last_activity_at=1000)

        row = cast(
            tuple[str, str, int] | None,
            conn.execute(
                "SELECT name, type, needs_refresh FROM dialogs WHERE dialog_id = ?",
                (peer_id,),
            ).fetchone(),
        )
        assert row is not None
        assert row[0] == "Resolved Chat", f"name must not be clobbered, got {row[0]!r}"
        assert row[1] == "user", f"type must not be clobbered, got {row[1]!r}"
        assert row[2] == 0, f"needs_refresh must stay 0 (not reset to 1), got {row[2]!r}"
