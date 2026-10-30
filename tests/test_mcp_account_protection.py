from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import MagicMock

import pytest
from jsonschema import validate

from mcp_telegram import server as mcp_server
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.daemon_client import daemon_connection
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.feedback_db import SQLiteFeedbackStore, ensure_feedback_schema
from mcp_telegram.feedback_service import FeedbackApplicationService
from mcp_telegram.flood import FloodWaitKillSwitchStatus
from mcp_telegram.fts import backfill_fts_index
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_rpc import TelegramRpcGate
from tests.daemon_api_policy import make_daemon_api_policy
from tests.demand_adapter_helpers import run_ready_slice
from tests.helpers import (
    LoudChannelProfilePort,
    LoudChatAvatarHistoryPort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
)
from tests.sqlite_writer_helpers import inline_writer
from tests.test_entity_profile_full_user_pair import _prepare
from tests.test_telegram_rpc import _CircuitStatus, _gate, _RawFutureSender, _set_sender, _TestRequest

_ACTIVE_NOTICE = (
    "Background Telegram acquisition is blocked by account protection. Retained local reads remain available; use this result's source and coverage to assess requested data. New incoming data and freshness may be limited. "
    "Recovery requires operator action."
)


class _SenderCallCount(Protocol):
    calls: int


def _protection_gate_client() -> tuple[TelegramRpcGate, _SenderCallCount, object]:
    gate: TelegramRpcGate = _gate(_CircuitStatus(open=True))
    sender = _RawFutureSender()
    _set_sender(gate, sender)

    class GateClient:
        async def get_entity(self, dialog_id: int) -> object:
            return await gate(_TestRequest(dialog_id))

    return gate, cast(_SenderCallCount, sender), GateClient()


def _install_mapping_swap(
    daemon: DaemonAPIServer,
    conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[int], list[dict[str, object]]]:
    natural_resolutions = [0]
    message_requests: list[dict[str, object]] = []
    original_list_topics = daemon._list_topics
    original_list_messages = daemon._list_messages

    async def list_topics_then_reassign(req: dict[str, object]) -> dict[str, object]:
        result = await original_list_topics(req)
        if req.get("dialog") == "Project":
            natural_resolutions[0] += 1
            conn.execute("UPDATE dialogs SET name='Former Project' WHERE dialog_id=123")
            conn.execute("UPDATE dialogs SET name='Project' WHERE dialog_id=456")
            conn.commit()
        return cast(dict[str, object], result)

    async def capture_message_request(req: dict[str, object]) -> dict[str, object]:
        message_requests.append(dict(req))
        return cast(dict[str, object], await original_list_messages(req))

    monkeypatch.setattr(daemon, "_list_topics", list_topics_then_reassign)
    monkeypatch.setattr(daemon, "_list_messages", capture_message_request)
    return natural_resolutions, message_requests


def _active_status() -> FloodWaitKillSwitchStatus:
    return FloodWaitKillSwitchStatus(
        open=True,
        reason="too_many_flood_wait_events",
        opened_at=1_700_000_000,
        events_in_window=5,
        wait_s_in_window=300,
        window_seconds=600,
        source="test",
    )


def _local_daemon(
    tmp_path: Path,
    client: object,
) -> tuple[DaemonAPIServer, sqlite3.Connection, sqlite3.Connection, Path]:
    sync_path = tmp_path / "sync.db"
    ensure_sync_schema(sync_path)
    sync_conn = sqlite3.connect(sync_path)
    feedback_path = tmp_path / "feedback.db"
    feedback_conn = ensure_feedback_schema(feedback_path)
    daemon = DaemonAPIServer(
        sync_conn,
        cast(DaemonClientLike, client),
        asyncio.Event(),
        FeedbackApplicationService(SQLiteFeedbackStore(feedback_conn)),
        writer=inline_writer(sync_conn),
        channel_profile_port=LoudChannelProfilePort(),
        group_profile_port=LoudGroupProfilePort(),
        user_profile_port=LoudUserProfilePort(),
        common_chats_port=LoudCommonChatsPort(),
        user_avatar_history_port=LoudUserAvatarHistoryPort(),
        chat_avatar_history_port=LoudChatAvatarHistoryPort(),
        policy=make_daemon_api_policy(),
    )
    daemon._ready = True
    return daemon, sync_conn, feedback_conn, feedback_path


def _install_daemon_socket(monkeypatch: pytest.MonkeyPatch, socket_path: Path) -> None:
    monkeypatch.setattr("mcp_telegram.daemon_client.get_daemon_socket_path", lambda _state_dir: socket_path)


def _seed_topic_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    name: str,
    *,
    message: str | None,
    topic_id: int = 7,
) -> None:
    conn.execute(
        "INSERT INTO dialogs "
        "(dialog_id,name,type,snapshot_at,hidden,identity_observed_at,identity_complete,identity_source) "
        "VALUES (?,?,?,1,0,1,1,'directory')",
        (dialog_id, name, "supergroup"),
    )
    conn.execute("INSERT INTO synced_dialogs (dialog_id,status) VALUES (?, 'synced')", (dialog_id,))
    conn.execute(
        "INSERT INTO topic_metadata "
        "(dialog_id,topic_id,title,is_general,is_deleted,updated_at,icon_emoji_id,icon_emoji) "
        "VALUES (?,?,?,0,0,1,42,NULL)",
        (dialog_id, topic_id, "General"),
    )
    if message is not None:
        conn.execute(
            "INSERT INTO messages (dialog_id,message_id,sent_at,text,forum_topic_id) VALUES (?,?,?,?,?)",
            (dialog_id, 1, 1_700_000_000, message, topic_id),
        )
    conn.commit()


@pytest.mark.asyncio
async def test_registered_local_tool_uses_real_ipc_protection_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, object())
    daemon._health_status = _active_status
    status_reads = 0
    get_status = daemon._get_account_protection

    def count_status_reads(req: dict[str, object]) -> dict[str, object]:
        nonlocal status_reads
        status_reads += 1
        return get_status(req)

    daemon._get_account_protection = count_status_reads  # type: ignore[method-assign]
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("get_sync_status", {"dialog_id": 123})
    finally:
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    protection = cast(dict[str, object], payload["account_protection"])
    assert protection == {
        "status": "active",
        "outbound_acquisition": "blocked",
        "recovery": "manual",
        "notice": _ACTIVE_NOTICE,
        "reason": "too_many_flood_wait_events",
        "opened_at": 1_700_000_000,
    }
    assert status_reads == 1
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["get_sync_status"].output_schema))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hold", "event_at", "since", "until", "expect_results", "interval"),
    [
        (True, None, "2023-11-14T22:12:00Z", "2023-11-14T22:14:00Z", True, "covered_as_of_horizon"),
        (False, 1_700_000_200, "2023-11-14T22:12:00Z", "2023-11-14T22:20:00Z", True, "beyond_horizon"),
        (False, None, "2023-11-14T22:12:00Z", "2023-11-14T22:14:00Z", True, "covered_as_of_horizon"),
        (True, None, "2023-11-14T22:16:00Z", "2023-11-14T22:20:00Z", False, "beyond_horizon"),
    ],
)
async def test_registered_assessments_follow_local_results_and_protection(  # noqa: PLR0913, PLR0914, PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hold: bool,
    event_at: int | None,
    since: str,
    until: str,
    expect_results: bool,
    interval: str,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    gate, sender, client = _protection_gate_client()
    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, client)
    _seed_topic_dialog(sync_conn, 123, "Project", message="needle retained")
    sync_conn.execute(
        "UPDATE synced_dialogs SET last_synced_at=?,last_event_at=? WHERE dialog_id=123",
        (1_700_000_100, event_at),
    )
    sync_conn.commit()
    backfill_fts_index(sync_conn)
    if hold:
        daemon._health_status = _active_status
    status_reads = 0
    original_status = daemon._get_account_protection

    def count_status(req: dict[str, object]) -> dict[str, object]:
        nonlocal status_reads
        status_reads += 1
        return original_status(req)

    daemon._get_account_protection = count_status  # type: ignore[method-assign]
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            status = await mcp_server.call_tool("get_sync_status", {"dialog_id": 123})
            messages = await mcp_server.call_tool(
                "list_messages",
                {"exact_dialog_id": 123, "message_state": "sent", "since_utc": since, "until_utc": until},
            )
            search = await mcp_server.call_tool(
                "search_messages", {"dialog": "123", "query": "needle", "since_utc": since, "until_utc": until}
            )
            extra = []
            if not hold and event_at is None:
                extra = [
                    await mcp_server.call_tool("list_messages", {"exact_dialog_id": 123, "message_state": "all"}),
                    await mcp_server.call_tool("search_messages", {"query": "needle"}),
                ]
    finally:
        sync_conn.close()
        feedback_conn.close()
        await gate.close_rpc_scheduler()

    assert status_reads == 3 + len(extra) and sender.calls == 0
    for result in extra:
        assert result.is_error is False
        payload = cast(dict[str, object], result.structured_content)
        summary = cast(str, cast(dict[str, object], payload["assessment"])["summary"])
        assert "Aggregate coverage is unverified" in summary
    expected_protection = "account_hold" if hold else "no_account_hold_observed"
    for name, result in (("get_sync_status", status), ("list_messages", messages), ("search_messages", search)):
        assert result.is_error is False and result.content == []
        payload = cast(dict[str, object], result.structured_content)
        assessment = cast(dict[str, object], payload["assessment"])
        assert assessment["local_read"] == "completed_without_telegram"
        assert assessment["background_acquisition"] == expected_protection
        validate(payload, cast(dict[str, object], mcp_server.tool_by_name[name].output_schema))
    status_payload = cast(dict[str, object], status.structured_content)
    assert status_payload["history_complete_at"] == "2023-11-14T22:15:00+00:00"
    summary = cast(str, cast(dict[str, object], status_payload["assessment"])["summary"])
    assert ("event was observed after" in summary) is (event_at is not None)
    if event_at is None:
        assert "silence alone does not indicate a fault" in summary
    for result, key in ((messages, "messages"), (search, "results")):
        payload = cast(dict[str, object], result.structured_content)
        assert bool(payload[key]) is expect_results
        coverage = cast(dict[str, object], payload["coverage"])
        read_summary = cast(str, cast(dict[str, object], payload["assessment"])["summary"])
        if key == "messages":
            assert coverage["interval_coverage"] == interval
            assert ("extends beyond" in read_summary) is (interval == "beyond_horizon")
        else:
            assert "interval_coverage" not in coverage
            assert "interval coverage is unknown" in read_summary


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_assessment_reports_unavailable_protection_without_losing_local_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, malformed: bool
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    gate, sender, client = _protection_gate_client()
    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, client)
    _seed_topic_dialog(sync_conn, 123, "Project", message="retained")
    calls = 0

    def unavailable(_req: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"ok": True, "data": {"account_protection": {"status": "active"}}} if malformed else {"ok": False}

    daemon._get_account_protection = unavailable  # type: ignore[method-assign]
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("get_sync_status", {"dialog_id": 123})
    finally:
        sync_conn.close()
        feedback_conn.close()
        await gate.close_rpc_scheduler()
    payload = cast(dict[str, object], result.structured_content)
    assert result.is_error is False and payload["saved_message_count"] == 1
    assert cast(dict[str, object], payload["assessment"])["background_acquisition"] == "unknown"
    assert cast(dict[str, object], payload["account_protection"])["status"] == "unavailable"
    assert calls == 1 and sender.calls == 0
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["get_sync_status"].output_schema))


def test_assessment_schema_only_on_three_registered_tools() -> None:
    expected = {"get_sync_status", "list_messages", "search_messages"}
    for name, tool in mcp_server.tool_by_name.items():
        schema = cast(dict[str, object], tool.output_schema)
        properties = cast(dict[str, object], schema["properties"])
        assert ("assessment" in properties) is (name in expected)
        if name in expected:
            assessment = cast(dict[str, object], properties["assessment"])
            assert assessment["required"] == ["local_read", "background_acquisition", "summary"]
            assert assessment["additionalProperties"] is False
            assert "assessment" not in cast(list[str], cast(dict[str, object], schema["else"])["required"])


@pytest.mark.asyncio
async def test_registered_unobserved_topic_catalog_stays_local_under_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    from tests.test_telegram_rpc import _CircuitStatus, _gate, _RawFutureSender, _set_sender, _TestRequest

    gate: TelegramRpcGate = _gate(_CircuitStatus(open=True))
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    circuit_checks = 0
    original_check = gate.check_circuit

    def count_circuit_checks() -> None:
        nonlocal circuit_checks
        circuit_checks += 1
        original_check()

    gate.check_circuit = count_circuit_checks  # type: ignore[method-assign]

    class GateClient:
        async def get_entity(self, dialog_id: int) -> object:
            return await gate(_TestRequest(dialog_id))

    daemon, sync_conn, feedback_conn, _ = _local_daemon(
        tmp_path,
        GateClient(),
    )
    daemon._health_status = _active_status
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("list_topics", {"exact_dialog_id": 123})
    finally:
        sync_conn.close()
        feedback_conn.close()
        await gate.close_rpc_scheduler()

    assert result.is_error is False
    assert result.content == []
    assert circuit_checks == 0
    assert sender.calls == 0
    payload = cast(dict[str, object], result.structured_content)
    assert payload["topics"] == []
    assert payload["empty_reason"] == "topic_catalog_not_observed"
    assert cast(dict[str, object], payload["catalog"])["coverage"] == "not_observed"
    assert cast(dict[str, object], payload["account_protection"]) == {
        "status": "active",
        "outbound_acquisition": "blocked",
        "recovery": "manual",
        "notice": _ACTIVE_NOTICE,
        "reason": "too_many_flood_wait_events",
        "opened_at": 1_700_000_000,
    }
    assert mcp_server.tool_by_name["list_topics"].output_schema is not None
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["list_topics"].output_schema))


@pytest.mark.asyncio
async def test_registered_entity_profile_pending_keeps_partial_identity_under_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, object())
    sync_conn.execute(
        "INSERT INTO entities(id,type,name,username,name_normalized,updated_at) VALUES (42,'user','Target',NULL,NULL,1)"
    )
    sync_conn.commit()
    daemon._policy = replace(
        daemon._policy,
        entity_profile=replace(daemon._policy.entity_profile, foreground_refresh_wait_seconds=0.01),
    )
    daemon._get_entity_info_service()._profiles.mark_pending(42, now=1)
    daemon.bind_demand_sink(MagicMock(offer=MagicMock(return_value=True)))
    daemon._health_status = _active_status
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("get_entity_info", {"exact_entity_id": 42})
    finally:
        await daemon.shutdown()
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    payload = cast(dict[str, object], result.structured_content)
    assert payload["completeness"] == "partial"
    assert payload["display_name"] == "Target"
    assert cast(dict[str, object], payload["account_protection"])["status"] == "active"
    assert cast(dict[str, object], payload["account_protection"]) == {
        "status": "active",
        "outbound_acquisition": "blocked",
        "recovery": "manual",
        "notice": _ACTIVE_NOTICE,
        "reason": "too_many_flood_wait_events",
        "opened_at": 1_700_000_000,
    }
    assert mcp_server.tool_by_name["get_entity_info"].output_schema is not None
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["get_entity_info"].output_schema))


@pytest.mark.asyncio
async def test_registered_entity_info_keeps_owned_partial_cache_success_under_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    sync_path = tmp_path / "sync.db"
    seed_conn, raw_seed_service = _prepare(sync_path, migrated=True)
    request.addfinalizer(seed_conn.close)
    seed_service = cast(DaemonEntityInfoService, raw_seed_service)
    coordinator = seed_service.refresh_coordinator
    assert coordinator is not None
    await run_ready_slice(EntityProfileDemandAdapter(coordinator))
    assert seed_conn.execute("SELECT profile_owner_account_id FROM entity_details WHERE entity_id=42").fetchone() == (
        42,
    )
    await seed_service.shutdown()
    seed_conn.close()

    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, object())
    daemon.bind_demand_sink(MagicMock(offer=MagicMock(return_value=True)))
    daemon._policy = replace(
        daemon._policy,
        entity_profile=replace(daemon._policy.entity_profile, foreground_refresh_wait_seconds=0.01),
    )
    await daemon._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
    daemon._health_status = _active_status
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("get_entity_info", {"exact_entity_id": 42})
    finally:
        await daemon.shutdown()
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    assert payload["display_name"] == "Target"
    common = cast(dict[str, object], payload["common"])
    assert common["name"] == "Target"
    assert cast(dict[str, object], cast(dict[str, object], common["about"])["content"])["text"] == "about"
    assert payload["completeness"] == "partial"
    assert cast(dict[str, object], payload["sections"])["common_chats"] == {
        "status": "pending",
        "reason": "profile_summary_changed",
    }
    assert cast(dict[str, object], payload["account_protection"])["status"] == "active"
    assert mcp_server.tool_by_name["get_entity_info"].output_schema is not None
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["get_entity_info"].output_schema))


@pytest.mark.asyncio
async def test_feedback_persists_when_only_following_status_operation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, feedback_path = _local_daemon(tmp_path, object())

    def fail_status(_req: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "error": "backend_error", "detail": "protection status unavailable"}

    daemon._get_account_protection = fail_status  # type: ignore[method-assign]
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("submit_feedback", {"message": "saved before status failure"})
    finally:
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    assert payload["accepted"] is True
    assert payload["account_protection"] == {
        "status": "unavailable",
        "notice": "Global background Telegram acquisition status is unavailable. This does not change the tool result; assess retained data using its source and coverage.",
    }
    readback = sqlite3.connect(feedback_path)
    try:
        row = cast(tuple[str] | None, readback.execute("SELECT message FROM feedback").fetchone())
    finally:
        readback.close()
    assert row == ("saved before status failure",)
    assert mcp_server.tool_by_name["submit_feedback"].output_schema is not None
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["submit_feedback"].output_schema))


@pytest.mark.asyncio
async def test_feedback_omits_status_only_after_explicit_inactive_status_over_real_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, feedback_path = _local_daemon(tmp_path, object())
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("submit_feedback", {"message": "known inactive status"})
    finally:
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    assert payload["accepted"] is True
    assert "account_protection" not in payload
    readback = sqlite3.connect(feedback_path)
    try:
        assert readback.execute("SELECT message FROM feedback").fetchone() == ("known inactive status",)
    finally:
        readback.close()
    schema = mcp_server.tool_by_name["submit_feedback"].output_schema
    assert schema is not None
    validate(payload, cast(dict[str, object], schema))


@pytest.mark.parametrize(
    "status_response",
    [
        {"ok": True, "data": {}},
        {"ok": True, "data": {"account_protection": {"status": "active"}}},
        {
            "ok": True,
            "data": {
                "account_protection": {
                    "status": "active",
                    "outbound_acquisition": "blocked",
                    "recovery": "manual",
                    "notice": 42,
                }
            },
        },
        {
            "ok": True,
            "data": {
                "account_protection": {
                    "status": "active",
                    "outbound_acquisition": "blocked",
                    "recovery": "manual",
                    "notice": "Active protection",
                    "opened_at": True,
                }
            },
        },
    ],
)
@pytest.mark.asyncio
async def test_feedback_status_missing_or_malformed_stays_unavailable_over_real_ipc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status_response: dict[str, object],
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, feedback_path = _local_daemon(tmp_path, object())
    daemon._get_account_protection = lambda _req: status_response  # type: ignore[method-assign]
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool("submit_feedback", {"message": "saved before bad status"})
    finally:
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    assert payload["accepted"] is True
    assert payload["account_protection"] == {
        "status": "unavailable",
        "notice": "Global background Telegram acquisition status is unavailable. This does not change the tool result; assess retained data using its source and coverage.",
    }
    readback = sqlite3.connect(feedback_path)
    try:
        assert readback.execute("SELECT message FROM feedback").fetchone() == ("saved before bad status",)
    finally:
        readback.close()
    schema = mcp_server.tool_by_name["submit_feedback"].output_schema
    assert schema is not None
    validate(payload, cast(dict[str, object], schema))


@pytest.mark.asyncio
async def test_registered_list_messages_pins_real_topic_lookup_identity_under_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    gate, sender, client = _protection_gate_client()

    daemon, sync_conn, feedback_conn, _ = _local_daemon(
        tmp_path,
        client,
    )
    _seed_topic_dialog(sync_conn, 123, "Project", message="A content")
    _seed_topic_dialog(sync_conn, 456, "Archive", message="B content")
    daemon._health_status = _active_status
    natural_resolutions, message_requests = _install_mapping_swap(daemon, sync_conn, monkeypatch)
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool(
                "list_messages",
                {"dialog": "Project", "topic": "General", "message_state": "sent"},
            )
            assert natural_resolutions == [1]
            async with daemon_connection() as conn:
                assert cast(dict[str, object], (await conn.list_topics(dialog="Project"))["data"])["dialog_id"] == 456
    finally:
        sync_conn.close()
        feedback_conn.close()
        await gate.close_rpc_scheduler()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    assert cast(dict[str, object], payload["account_protection"])["status"] == "active"
    messages = cast(list[dict[str, object]], payload["messages"])
    assert [cast(dict[str, object], message["content"])["text"] for message in messages] == ["A content"]
    assert payload["dialog_id"] == 123
    assert natural_resolutions == [2]
    assert len(message_requests) == 1
    assert message_requests[0]["dialog_id"] == 123
    assert message_requests[0]["topic_id"] == 7
    assert "dialog" not in message_requests[0]
    assert sender.calls == 0
    schema = mcp_server.tool_by_name["list_messages"].output_schema
    assert schema is not None
    validate(payload, cast(dict[str, object], schema))


@pytest.mark.asyncio
async def test_registered_list_messages_marks_empty_saved_messages_in_structured_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, object())
    daemon.self_id = 123
    _seed_topic_dialog(sync_conn, 123, "Saved Messages", message=None)
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool(
                "list_messages",
                {"exact_dialog_id": 123, "message_state": "sent"},
            )
    finally:
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is False
    assert result.content == []
    payload = cast(dict[str, object], result.structured_content)
    assert payload["messages"] == []
    assert cast(dict[str, object], payload["dialog"])["is_self"] is True
    schema = mcp_server.tool_by_name["list_messages"].output_schema
    assert schema is not None
    validate(payload, cast(dict[str, object], schema))


@pytest.mark.asyncio
async def test_registered_list_messages_unobserved_topic_catalog_is_explicit_under_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    gate, sender, client = _protection_gate_client()

    daemon, sync_conn, feedback_conn, _ = _local_daemon(
        tmp_path,
        client,
    )
    sync_conn.execute(
        "INSERT INTO dialogs "
        "(dialog_id,name,type,snapshot_at,hidden,identity_observed_at,identity_complete,identity_source) "
        "VALUES (123,'Project','supergroup',1,0,1,1,'directory')"
    )
    sync_conn.commit()
    daemon._health_status = _active_status
    message_requests: list[dict[str, object]] = []
    original_list_messages = daemon._list_messages

    async def capture_message_request(req: dict[str, object]) -> dict[str, object]:
        message_requests.append(dict(req))
        return cast(dict[str, object], await original_list_messages(req))

    monkeypatch.setattr(daemon, "_list_messages", capture_message_request)
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool(
                "list_messages",
                {"dialog": "Project", "topic": "General", "message_state": "sent"},
            )
    finally:
        sync_conn.close()
        feedback_conn.close()
        await gate.close_rpc_scheduler()

    assert result.is_error is True
    assert result.content
    payload = cast(dict[str, object], result.structured_content)
    error = cast(dict[str, object], payload["error"])
    assert "assessment" not in payload
    details = cast(dict[str, object], error["details"])
    assert error["code"] == "topic_catalog_incomplete"
    assert "mark_dialog_for_sync" in cast(str, error["action"])
    assert details["dialog_id"] == 123
    assert cast(dict[str, object], details["catalog"])["coverage"] == "not_observed"
    assert cast(dict[str, object], payload["account_protection"])["status"] == "active"
    assert sender.calls == 0
    assert message_requests == []
    schema = mcp_server.tool_by_name["list_messages"].output_schema
    assert schema is not None
    validate(payload, cast(dict[str, object], schema))


@pytest.mark.parametrize(("corrupt_field", "corrupt_value"), [("dialog_id", 0), ("id", None)])
@pytest.mark.asyncio
async def test_registered_list_messages_rejects_malformed_topic_ipc_identity_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_field: str,
    corrupt_value: object,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    daemon, sync_conn, feedback_conn, _ = _local_daemon(
        tmp_path,
        object(),
    )
    _seed_topic_dialog(sync_conn, 123, "Project", message="must not be read")
    daemon._health_status = _active_status

    original_list_topics = daemon._list_topics
    message_requests: list[dict[str, object]] = []

    async def corrupt_topic_identity(req: dict[str, object]) -> dict[str, object]:
        response = await original_list_topics(req)
        data = cast(dict[str, object], response["data"])
        if corrupt_field == "dialog_id":
            data[corrupt_field] = corrupt_value
        else:
            first_topic = cast(dict[str, object], cast(list[object], data["topics"])[0])
            first_topic[corrupt_field] = corrupt_value
        return cast(dict[str, object], response)

    async def capture_message_request(req: dict[str, object]) -> dict[str, object]:
        message_requests.append(dict(req))
        return {"ok": True, "data": {"messages": []}}

    monkeypatch.setattr(daemon, "_list_topics", corrupt_topic_identity)
    monkeypatch.setattr(daemon, "_list_messages", capture_message_request)
    socket_path = tmp_path / "daemon.sock"
    _install_daemon_socket(monkeypatch, socket_path)
    try:
        async with await asyncio.start_unix_server(daemon.handle_client, path=socket_path):
            result = await mcp_server.call_tool(
                "list_messages",
                {"dialog": "Project", "topic": "General", "message_state": "sent"},
            )
    finally:
        sync_conn.close()
        feedback_conn.close()

    assert result.is_error is True
    payload = cast(dict[str, object], result.structured_content)
    error = cast(dict[str, object], payload["error"])
    assert error["code"] == "invalid_topic_lookup_response"
    assert cast(dict[str, object], payload["account_protection"])["status"] == "active"
    assert message_requests == []
    schema = mcp_server.tool_by_name["list_messages"].output_schema
    assert schema is not None
    validate(payload, cast(dict[str, object], schema))
