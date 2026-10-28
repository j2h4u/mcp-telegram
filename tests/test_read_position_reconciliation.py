# pyright: reportAny=false, reportOperatorIssue=false, reportOptionalSubscript=false, reportIndexIssue=false
"""Focused coverage for durable read-position reconciliation and projection."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from mcp_telegram.daemon import _DaemonClient
from mcp_telegram.daemon import _initialize_read_positions as _initialize_read_positions_owner
from mcp_telegram.message_fact_refresh import ReadReceiptDemandAdapter
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkRevision
from tests.history_enrollment_helpers import seed_full_history_enrollment
from tests.sqlite_writer_helpers import inline_writer


def _connection() -> sqlite3.Connection:
    from mcp_telegram.sync_db import _apply_migrations

    conn = sqlite3.connect(":memory:")
    _apply_migrations(conn)
    return conn


async def _initialize_read_positions(  # noqa: PLR0913 - mirrors the owner boundary for repeated exact slices
    client: object,
    conn: sqlite3.Connection,
    shutdown: asyncio.Event,
    *,
    writer: SQLiteWriter | None = None,
    max_dialogs: int | None = None,
    failure_cooldown_seconds: float | None = None,
    batch_size: int | None = None,
    batch_pause_seconds: float | None = None,
    success_recheck_seconds: float | None = None,
) -> None:
    """Exercise exact single-dialog owner slices as the coordinator would."""
    adapter = ReadReceiptDemandAdapter(conn, lambda _selected: None, account_id=1)  # type: ignore[arg-type]
    limit = (
        int(max_dialogs)
        if max_dialogs is not None
        else int(
            conn.execute(
                "SELECT COUNT(*) FROM synced_dialogs WHERE read_inbox_max_id IS NULL OR read_outbox_max_id IS NULL"
            ).fetchone()[0]
        )
    )
    for _ in range(limit):
        status = adapter.status(time.time())
        if status is None or status.selected is None:
            break
        before = conn.execute(
            "SELECT read_inbox_max_id, read_outbox_max_id, COALESCE((SELECT unread_count FROM dialogs WHERE dialog_id=?),0) "
            "FROM synced_dialogs WHERE dialog_id=?",
            (
                int(status.selected.key.target.removeprefix("dialog:")),
                int(status.selected.key.target.removeprefix("dialog:")),
            ),
        ).fetchone()
        outcome = await _initialize_read_positions_owner(
            cast(_DaemonClient, client),
            conn,
            shutdown,
            writer=cast(SQLiteWriter, writer or inline_writer(conn)),
            max_dialogs=max_dialogs,
            failure_cooldown_seconds=failure_cooldown_seconds,
            batch_size=batch_size,
            batch_pause_seconds=batch_pause_seconds,
            success_recheck_seconds=success_recheck_seconds,
            selected=status.selected,
        )
        if outcome.kind is not SliceOutcomeKind.PROGRESSED:
            break
        dialog_id = int(status.selected.key.target.removeprefix("dialog:"))
        after = conn.execute(
            "SELECT read_inbox_max_id, read_outbox_max_id, COALESCE((SELECT unread_count FROM dialogs WHERE dialog_id=?),0) "
            "FROM synced_dialogs WHERE dialog_id=?",
            (dialog_id, dialog_id),
        ).fetchone()
        if after == before:
            break


def _seed_pending(conn: sqlite3.Connection, dialog_id: int, *, status: str = "synced") -> None:
    conn.execute(
        "INSERT INTO synced_dialogs(dialog_id, status, read_inbox_max_id) VALUES (?, ?, NULL)",
        (dialog_id, status),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=status == "synced")
    conn.commit()


def _seed_stale_unread(conn: sqlite3.Connection, dialog_id: int, *, cursor: int = 10) -> None:
    conn.execute(
        "INSERT INTO synced_dialogs"
        "(dialog_id, status, read_inbox_max_id, read_outbox_max_id) VALUES (?, 'synced', ?, ?)",
        (dialog_id, cursor, cursor),
    )
    seed_full_history_enrollment(conn, dialog_id, enabled=True)
    conn.execute(
        "INSERT INTO dialogs(dialog_id, name, type, unread_count, unread_count_observed_at) "
        "VALUES (?, 'Test', 'user', 1, 100)",
        (dialog_id,),
    )
    conn.execute(
        "INSERT INTO messages(dialog_id, message_id, sent_at, text, out, is_service, is_deleted) "
        "VALUES (?, ?, 100, 'unread', 0, 0, 0)",
        (dialog_id, cursor + 1),
    )
    conn.commit()


@pytest.mark.asyncio
async def test_selected_read_receipt_sibling_progresses_and_stale_revision_is_rejected() -> None:
    conn = _connection()
    _seed_pending(conn, 1001)
    _seed_pending(conn, 1002)
    calls: list[str] = []

    async def run_batch(selected: WorkRevision) -> SliceOutcome:
        calls.append(str(selected.key.target))
        dialog_id = int(selected.key.target.removeprefix("dialog:"))
        conn.execute(
            "UPDATE synced_dialogs SET read_inbox_max_id=10,read_outbox_max_id=10 WHERE dialog_id=?",
            (dialog_id,),
        )
        conn.commit()
        return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, "accepted")

    adapter = ReadReceiptDemandAdapter(conn, run_batch, account_id=1)
    blocked = adapter.status(100)
    assert blocked is not None and blocked.selected is not None
    selected = adapter.status(100, excluded=frozenset({blocked.selected}))
    assert selected is not None and selected.selected is not None
    assert selected.selected.key.target == "dialog:1002"

    outcome = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=selected.selected))
    assert outcome.kind is SliceOutcomeKind.PROGRESSED
    assert calls == ["dialog:1002"]
    stale = await adapter.run_slice(RpcAttemptBudget(limit=1, selected=selected.selected))
    assert stale.kind is SliceOutcomeKind.STALE
    assert calls == ["dialog:1002"]
    conn.close()


@pytest.mark.asyncio
async def test_reconciliation_pass_caps_selected_dialogs() -> None:
    conn = _connection()
    try:
        for dialog_id in range(1001, 1006):
            _seed_pending(conn, dialog_id)
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace()
        client.return_value = SimpleNamespace(dialogs=[])
        with patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: 1001):
            await _initialize_read_positions(client, conn, asyncio.Event(), writer=inline_writer(conn), max_dialogs=2)
        assert client.get_input_entity.await_count == 1
        assert client.await_count == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconciliation_propagates_latched_rpc_circuit() -> None:
    from mcp_telegram.flood import TelegramRpcThrottled

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        client = AsyncMock()
        client.get_input_entity.side_effect = TelegramRpcThrottled(latched=True, detail="open-for-test")
        with pytest.raises(TelegramRpcThrottled, match="open-for-test"):
            await _initialize_read_positions(client, conn, asyncio.Event(), writer=inline_writer(conn), max_dialogs=1)
        assert conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id = 1001").fetchone() == (None,)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("seconds", [5, 120])
async def test_resolution_floodwait_uses_canonical_shutdown_aware_sleep(seconds: int) -> None:
    from mcp_telegram.flood import TelegramRpcThrottled

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        error = TelegramRpcThrottled(retry_after_seconds=seconds)
        client = AsyncMock()
        client.get_input_entity.side_effect = error
        with pytest.raises(TelegramRpcThrottled):
            await _initialize_read_positions(
                client, conn, asyncio.Event(), writer=inline_writer(conn), max_dialogs=1, failure_cooldown_seconds=3600
            )
        assert conn.execute(
            "SELECT read_position_attempt_count FROM synced_dialogs WHERE dialog_id=1001"
        ).fetchone() == (0,)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_resolution_floodwait_aborts_remaining_batches() -> None:
    from mcp_telegram.flood import TelegramRpcThrottled

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        _seed_pending(conn, 1002)
        error = TelegramRpcThrottled(retry_after_seconds=5)
        client = AsyncMock()
        client.get_input_entity.side_effect = error
        with pytest.raises(TelegramRpcThrottled):
            await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=2,
                batch_size=1,
                failure_cooldown_seconds=3600,
            )
        assert client.get_input_entity.await_count == 1
        client.assert_not_awaited()
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_request_floodwait_aborts_remaining_batches() -> None:
    from mcp_telegram.flood import TelegramRpcThrottled

    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        _seed_pending(conn, 1002)
        error = TelegramRpcThrottled(retry_after_seconds=5)
        client = AsyncMock()
        client.get_input_entity.side_effect = lambda dialog_id: SimpleNamespace(_did=dialog_id)
        client.side_effect = error
        with (
            pytest.raises(TelegramRpcThrottled),
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did),
        ):
            await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=2,
                batch_size=1,
                failure_cooldown_seconds=3600,
            )
        assert client.get_input_entity.await_count == 1
        assert client.await_count == 1
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_none_input_peer_is_not_sent_and_other_dialog_waits_for_its_slice() -> None:
    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        _seed_pending(conn, 1002)
        client = AsyncMock()

        async def resolve(dialog_id: int) -> object | None:
            return None if dialog_id == 1001 else SimpleNamespace(_did=dialog_id)

        client.get_input_entity.side_effect = resolve
        client.side_effect = lambda _request: SimpleNamespace(
            dialogs=[SimpleNamespace(peer=SimpleNamespace(_did=1002), read_inbox_max_id=8, read_outbox_max_id=9)]
        )
        with (
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did),
        ):
            await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=2,
                failure_cooldown_seconds=3600,
                batch_size=2,
            )
        assert client.await_count == 0
        assert conn.execute(
            "SELECT read_position_attempt_count FROM synced_dialogs WHERE dialog_id=1001"
        ).fetchone() == (0,)
        success_state = conn.execute(
            "SELECT read_position_next_attempt_at, read_position_attempt_count FROM synced_dialogs WHERE dialog_id=1002"
        ).fetchone()
        assert success_state[0] is None
        assert success_state[1] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_reconciliation_repairs_non_null_stale_unread_state() -> None:
    """A missed realtime read update is repaired by the bounded durable sweep."""
    conn = _connection()
    try:
        _seed_stale_unread(conn, 1001)
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace(_did=1001)
        client.return_value = SimpleNamespace(
            dialogs=[
                SimpleNamespace(
                    peer=SimpleNamespace(_did=1001),
                    read_inbox_max_id=11,
                    read_outbox_max_id=10,
                    unread_count=0,
                )
            ]
        )
        with (
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did),
        ):
            checked = await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=10,
                success_recheck_seconds=900,
            )

        assert checked is None
        assert (
            conn.execute(
                "SELECT read_inbox_max_id, read_position_next_attempt_at FROM synced_dialogs WHERE dialog_id=1001"
            ).fetchone()[0]
            == 11
        )
        assert conn.execute("SELECT unread_count FROM dialogs WHERE dialog_id=1001").fetchone() == (0,)
    finally:
        conn.close()


def test_reconciliation_snapshot_cannot_overwrite_same_second_realtime_count() -> None:
    """A snapshot racing a realtime fact advances the cursor but preserves the newer count."""
    from mcp_telegram.daemon import _apply_read_position_dialog, _ReadPositionDialogLike

    conn = _connection()
    try:
        _seed_stale_unread(conn, 1001)
        conn.execute("UPDATE dialogs SET unread_count=2, unread_count_observed_at=200 WHERE dialog_id=1001")
        conn.commit()
        dialog = SimpleNamespace(
            peer=SimpleNamespace(),
            read_inbox_max_id=11,
            read_outbox_max_id=10,
            unread_count=0,
        )
        with (
            write_transaction(conn),
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", return_value=1001),
        ):
            assert _apply_read_position_dialog(
                conn,
                cast(_ReadPositionDialogLike, dialog),
                request_started_at=200,
                success_due_at=1100,
            )
        assert conn.execute("SELECT read_inbox_max_id FROM synced_dialogs WHERE dialog_id=1001").fetchone() == (11,)
        assert conn.execute(
            "SELECT unread_count, unread_count_observed_at FROM dialogs WHERE dialog_id=1001"
        ).fetchone() == (2, 200)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_success_due_time_keeps_bounded_queue_fair() -> None:
    conn = _connection()
    try:
        _seed_stale_unread(conn, 1001)
        _seed_stale_unread(conn, 1002)
        client = AsyncMock()
        client.get_input_entity.side_effect = lambda dialog_id: SimpleNamespace(_did=dialog_id)
        client.side_effect = [
            SimpleNamespace(
                dialogs=[
                    SimpleNamespace(
                        peer=SimpleNamespace(_did=1001),
                        read_inbox_max_id=10,
                        read_outbox_max_id=10,
                        unread_count=1,
                    )
                ]
            ),
            SimpleNamespace(
                dialogs=[
                    SimpleNamespace(
                        peer=SimpleNamespace(_did=1002),
                        read_inbox_max_id=10,
                        read_outbox_max_id=10,
                        unread_count=1,
                    )
                ]
            ),
        ]
        with (
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did),
        ):
            await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=1,
                success_recheck_seconds=900,
            )
            await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=1,
                success_recheck_seconds=900,
            )

        assert [call.args[0] for call in client.get_input_entity.await_args_list] == [1001, 1002]
        assert conn.execute("SELECT read_position_next_attempt_at FROM synced_dialogs WHERE dialog_id=1001").fetchone()[
            0
        ] > int(time.time())
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_partial_response_preserves_failure_history() -> None:
    conn = _connection()
    try:
        _seed_stale_unread(conn, 1001)
        conn.execute("UPDATE synced_dialogs SET read_position_attempt_count=5 WHERE dialog_id=1001")
        conn.commit()
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace(_did=1001)
        client.return_value = SimpleNamespace(
            dialogs=[
                SimpleNamespace(
                    peer=SimpleNamespace(_did=1001),
                    read_inbox_max_id=11,
                    read_outbox_max_id=None,
                    unread_count=0,
                )
            ]
        )
        with (
            patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did),
        ):
            await _initialize_read_positions(
                client,
                conn,
                asyncio.Event(),
                writer=inline_writer(conn),
                max_dialogs=1,
                failure_cooldown_seconds=3600,
                success_recheck_seconds=900,
            )

        assert conn.execute(
            "SELECT read_position_attempt_count FROM synced_dialogs WHERE dialog_id=1001"
        ).fetchone() == (5,)
    finally:
        conn.close()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_telegram_none_cursor_is_cooled_down() -> None:
    conn = _connection()
    try:
        _seed_pending(conn, 1001)
        client = AsyncMock()
        client.get_input_entity.return_value = SimpleNamespace(_did=1001)
        client.return_value = SimpleNamespace(
            dialogs=[SimpleNamespace(peer=SimpleNamespace(_did=1001), read_inbox_max_id=None, read_outbox_max_id=4)]
        )
        with patch("mcp_telegram.daemon.telethon_utils.get_peer_id", side_effect=lambda peer: peer._did):
            await _initialize_read_positions(
                client, conn, asyncio.Event(), writer=inline_writer(conn), max_dialogs=1, failure_cooldown_seconds=3600
            )
            calls_after_none = client.get_input_entity.await_count
            await _initialize_read_positions(
                client, conn, asyncio.Event(), writer=inline_writer(conn), max_dialogs=1, failure_cooldown_seconds=3600
            )
        assert client.get_input_entity.await_count == calls_after_none + 1
        assert conn.execute(
            "SELECT read_inbox_max_id, read_outbox_max_id FROM synced_dialogs WHERE dialog_id = 1001"
        ).fetchone() == (None, 4)
        assert (
            conn.execute("SELECT read_position_next_attempt_at FROM synced_dialogs WHERE dialog_id = 1001").fetchone()[
                0
            ]
            is None
        )
    finally:
        conn.close()


def test_inbox_projection_uses_read_position_identity_contract() -> None:
    from mcp_telegram.tools.unread import _project_inbox_response

    response = {
        "ok": True,
        "data": {
            "groups": [],
            "read_position_pending_count": 3,
            "read_position_pending_entities": [
                {
                    "dialog_id": 10,
                    "display_name": "Alice",
                    "username": "alice",
                    "display_name_source": "name",
                    "name_unavailable_reason": None,
                },
                {
                    "dialog_id": 11,
                    "display_name": "Alias",
                    "username": "@alice",
                    "display_name_source": "name",
                    "name_unavailable_reason": None,
                },
                {
                    "dialog_id": 12,
                    "display_name": None,
                    "username": None,
                    "display_name_source": "numeric",
                    "name_unavailable_reason": "identity_not_observed",
                },
                {"dialog_id": None, "display_name": "invalid", "username": None},
            ],
        },
    }
    from mcp_telegram.tools.unread import GetInbox

    result = _project_inbox_response(GetInbox(), response, applied_since_utc=None, has_inbox_filter=False)
    payload = result.structured_content
    assert "bootstrap_pending" not in payload
    assert payload["read_position_pending_count"] == 3
    assert payload["coverage"]["read_position_pending_count"] == 3
    identities = payload["read_position_pending_entities"]
    assert identities == [
        {
            "entity": {"display_name": "Alice", "username": "@alice"},
            "display_name_source": "name",
            "name_unavailable_reason": None,
        },
        {
            "entity": {"display_name": "12", "telegram_id": 12},
            "display_name_source": "numeric",
            "name_unavailable_reason": "identity_not_observed",
        },
    ]

    assert payload["coverage"]["read_position_pending_entities"] == identities


def test_inbox_projection_rejects_missing_read_position_contract() -> None:
    from mcp_telegram.tools.unread import GetInbox, _project_inbox_response

    with pytest.raises(KeyError, match="read_position_pending_count"):
        _project_inbox_response(
            GetInbox(),
            {"ok": True, "data": {"groups": []}},
            applied_since_utc=None,
            has_inbox_filter=False,
        )
