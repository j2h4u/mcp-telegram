import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from jsonschema import ValidationError, validate

from mcp_telegram import server
from mcp_telegram.daemon_dialog_queries import _LATEST_RETAINED_MESSAGE_SQL
from mcp_telegram.sync_db import _SCHEDULED_MESSAGES_DDL, ensure_sync_schema
from mcp_telegram.sync_read_model import SyncReadModelContractError, decode_latest_retained_message
from tests.test_daemon_api import (
    _insert_message,
    _insert_synced_dialog,
    _make_db_with_dialogs,
    _seed_dialog_row,
    make_server,
)


@pytest.mark.parametrize("latest", [None, {"message_id": 9, "sent_at": 100}])
def test_latest_message_decoder_accepts_only_canonical_shape(latest: dict[str, int] | None) -> None:
    assert decode_latest_retained_message(latest) == latest


@pytest.mark.parametrize(
    "latest",
    [
        {},
        {"message_id": True, "sent_at": 1},
        {"message_id": 1, "sent_at": None},
        {"message_id": 1, "sent_at": 1, "text": "extra"},
        "bad",
    ],
)
def test_latest_message_decoder_rejects_malformed(latest: object) -> None:
    with pytest.raises(SyncReadModelContractError):
        decode_latest_retained_message(latest)


def _assert_required_null_schema(payload: dict[str, object]) -> None:
    schema = cast(dict[str, object], server.tool_by_name["get_sync_status"].output_schema)
    missing_latest = dict(payload)
    del missing_latest["latest_retained_message"]
    with pytest.raises(ValidationError):
        validate(missing_latest, schema)
    missing_lag = dict(payload)
    missing_lag["message_delivery"] = {
        "state": "unverified",
        "gap_status": "unknown",
        "reason": "no_authoritative_delivery_diagnostics",
    }
    with pytest.raises(ValidationError):
        validate(missing_lag, schema)


def test_latest_query_uses_index_on_migrated_database(tmp_path: Path) -> None:
    path = tmp_path / "sync.db"
    ensure_sync_schema(path)
    with closing(sqlite3.connect(path)) as conn:
        plan = cast(
            list[tuple[int, int, int, str]],
            conn.execute("EXPLAIN QUERY PLAN " + _LATEST_RETAINED_MESSAGE_SQL, (123,)).fetchall(),
        )
    assert any("idx_messages_dialog_sent" in str(row[3]) for row in plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("unavailable", [False, True])
async def test_registered_sync_status_projects_local_rows_and_unknown_delivery(
    monkeypatch: pytest.MonkeyPatch, empty: bool, unavailable: bool
) -> None:
    conn = _make_db_with_dialogs()
    dialog_id = 123
    plan = cast(
        list[tuple[int, int, int, str]],
        conn.execute("EXPLAIN QUERY PLAN " + _LATEST_RETAINED_MESSAGE_SQL, (dialog_id,)).fetchall(),
    )
    assert any("idx_messages_dialog_sent" in str(row[3]) for row in plan)
    _insert_synced_dialog(
        conn,
        dialog_id,
        status="synced",
        enrollment_enabled=True,
        last_synced_at=1700000000,
        last_event_at=None if empty else 1700000100,
    )
    _seed_dialog_row(conn, dialog_id, last_message_at=1900000000)
    if not empty:
        # A reply known only outside this database has no local gap receipt.
        _insert_message(conn, dialog_id, 1, sent_at=1700000200)
        _insert_message(conn, dialog_id, 2, sent_at=1700000200)
        _insert_message(conn, dialog_id, 3, sent_at=1700000300)
        conn.execute("UPDATE messages SET is_deleted=1 WHERE dialog_id=? AND message_id=3", (dialog_id,))
        conn.execute("UPDATE messages SET out=1, is_service=1 WHERE dialog_id=? AND message_id=2", (dialog_id,))
        conn.commit()
    conn.execute(_SCHEDULED_MESSAGES_DDL)
    conn.execute(
        "INSERT INTO scheduled_messages(dialog_id,message_id,scheduled_at,first_seen_at,updated_at) VALUES (?,?,?,?,?)",
        (dialog_id, 999, 2000000000, 1700000000, 1700000000),
    )
    conn.commit()
    daemon = make_server(conn)

    class LocalConnection:
        async def get_sync_status(self, *, dialog_id: int) -> dict:
            return await daemon._dispatch({"method": "get_sync_status", "dialog_id": dialog_id})

    @asynccontextmanager
    async def local_connection() -> AsyncIterator[object]:
        yield LocalConnection()

    monkeypatch.setattr("mcp_telegram.tools.sync.daemon_connection", local_connection)
    protection = server._account_protection_unavailable() if unavailable else {}
    monkeypatch.setattr(server, "_account_protection_status", AsyncMock(return_value=protection))
    monkeypatch.setattr(server, "_schedule_telemetry", lambda _event: None)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    result = await server.call_tool("get_sync_status", {"dialog_id": dialog_id})
    conn.set_trace_callback(None)
    assert result.is_error is False
    payload = cast(dict[str, object], result.structured_content)
    validate(payload, cast(dict[str, object], server.tool_by_name["get_sync_status"].output_schema))
    assert payload["coverage_status"] == "synced" and payload["realtime_history"] == "full"
    assert payload["message_retention_policy"] == "all_authorized"
    assert payload["history_complete_at"] == "2023-11-14T22:13:20+00:00"
    assert payload["latest_retained_message"] == (
        None if empty else {"message_id": 2, "sent_at": "2023-11-14T22:16:40+00:00"}
    )
    assert payload["message_delivery"] == {
        "state": "unverified",
        "lag_seconds": None,
        "gap_status": "unknown",
        "reason": "no_authoritative_delivery_diagnostics",
    }
    assessment = cast(dict[str, object], payload["assessment"])
    assert assessment["background_acquisition"] == ("unknown" if unavailable else "no_account_hold_observed")
    assert "retention policy does not assure delivery" in cast(str, assessment["summary"])
    if empty:
        assert "silence alone does not indicate a fault" in cast(str, assessment["summary"])
    _assert_required_null_schema(payload)
    assert all(not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)


@pytest.mark.asyncio
async def test_registered_status_reports_migration_retry_without_acquisition(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = _make_db_with_dialogs()
    _seed_dialog_row(conn, 123, type_="group", members=12, created=1_700_000_000)
    _insert_synced_dialog(conn, 123, status="own_only")
    conn.execute(
        "INSERT OR REPLACE INTO full_history_enrollment(dialog_id,enabled,source,updated_at) VALUES (123,0,'migration',1)"
    )
    conn.execute("INSERT INTO daemon_state(key,value) VALUES ('automatic_group_history_retry:123','1700000100')")
    conn.commit()
    daemon = make_server(conn)
    monkeypatch.setattr("mcp_telegram.daemon_api.time.time", lambda: 1_700_000_000)

    class LocalConnection:
        async def get_sync_status(self, *, dialog_id: int) -> dict:
            return await daemon._dispatch({"method": "get_sync_status", "dialog_id": dialog_id})

    @asynccontextmanager
    async def local_connection() -> AsyncIterator[object]:
        yield LocalConnection()

    monkeypatch.setattr("mcp_telegram.tools.sync.daemon_connection", local_connection)
    monkeypatch.setattr(server, "_account_protection_status", AsyncMock(return_value={}))
    monkeypatch.setattr(server, "_schedule_telemetry", lambda _event: None)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    result = await server.call_tool("get_sync_status", {"dialog_id": 123})
    conn.set_trace_callback(None)
    assert result.is_error is False
    payload = cast(dict[str, object], result.structured_content)
    group = cast(dict[str, object], payload["automatic_group_sync"])
    assert group["enrollment_interpretation"] == "migration_unknown"
    assert group["eligibility"] == "blocked" and group["reason"] == "retry_wait"
    assert group["fresh_telegram_total"] == "unknown" and "retry_at" in group
    assert payload["enrollment_source"] == "migration" and payload["coverage_status"] == "own_only"
    validate(payload, cast(dict[str, object], server.tool_by_name["get_sync_status"].output_schema))
    assert all(not statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for statement in statements)
