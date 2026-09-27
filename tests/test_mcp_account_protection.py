from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from jsonschema import validate
from mcp.types import TextContent

from mcp_telegram import server as mcp_server
from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
from mcp_telegram.daemon_api import DaemonAPIServer, DaemonClientLike
from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.feedback_db import SQLiteFeedbackStore, ensure_feedback_schema
from mcp_telegram.feedback_service import FeedbackApplicationService
from mcp_telegram.flood import FloodWaitKillSwitchStatus
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_rpc import TelegramRpcGate, _MainSender, _MainSenderAdapter
from mcp_telegram.topics.refresh import TopicRefresher
from tests.daemon_api_policy import make_daemon_api_policy
from tests.helpers import (
    LoudChannelProfilePort,
    LoudChatAvatarHistoryPort,
    LoudCommonChatsPort,
    LoudGroupProfilePort,
    LoudUserAvatarHistoryPort,
    LoudUserProfilePort,
)
from tests.test_entity_profile_full_user_pair import _prepare

_ACTIVE_NOTICE = (
    "Telegram acquisition is blocked by account protection. Freshness and incoming coverage may be limited. "
    "Recovery requires operator action."
)


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
    *,
    topic_refresher: TopicRefresher | None = None,
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
        topic_refresher=topic_refresher,
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
async def test_registered_remote_topic_miss_crosses_gate_ipc_client_and_server(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    from tests.test_telegram_rpc import _CircuitStatus, _gate, _RawFutureSender, _TestRequest

    gate: TelegramRpcGate = _gate(_CircuitStatus(open=True))
    sender = _RawFutureSender()
    gate._main_sender = cast(_MainSender, sender)
    gate._sender = _MainSenderAdapter(gate)
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
        topic_refresher=cast(TopicRefresher, object()),
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

    assert result.is_error is True
    assert circuit_checks > 0
    assert sender.calls == 0
    payload = cast(dict[str, object], result.structured_content)
    error = cast(dict[str, object], payload["error"])
    details = cast(dict[str, object], error["details"])
    assert error["code"] == "flood_wait_kill_switch_open"
    assert error["message"] == "Telegram acquisition is blocked by account protection."
    assert error["action"] == "Recovery requires operator action."
    assert details["required_action"] == "manual_operator_recovery"
    assert details["retryable"] is False
    assert "retry_after" not in details
    assert "retry" not in cast(str, error["action"]).lower()
    assert isinstance(result.content[0], TextContent)
    assert "Fix the arguments" not in result.content[0].text
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
async def test_registered_entity_profile_pending_projects_manual_protection_error_over_ipc(
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

    assert result.is_error is True
    payload = cast(dict[str, object], result.structured_content)
    error = cast(dict[str, object], payload["error"])
    details = cast(dict[str, object], error["details"])
    assert error["code"] == "flood_wait_kill_switch_open"
    assert error["message"] == "Telegram acquisition is blocked by account protection."
    assert error["action"] == "Recovery requires operator action."
    assert "retry" not in cast(str, error["action"]).lower()
    assert details["required_action"] == "manual_operator_recovery"
    assert details["retryable"] is False
    assert "retry_after" not in details
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
) -> None:
    monkeypatch.setattr(mcp_server, "_schedule_telemetry", lambda _event: None)
    sync_path = tmp_path / "sync.db"
    seed_conn, raw_seed_service = _prepare(sync_path, migrated=True)
    seed_service = cast(DaemonEntityInfoService, raw_seed_service)
    coordinator = seed_service.refresh_coordinator
    assert coordinator is not None
    await EntityProfileDemandAdapter(coordinator).run_slice(RpcAttemptBudget(limit=1))
    await seed_service.shutdown()
    seed_conn.close()

    daemon, sync_conn, feedback_conn, _ = _local_daemon(tmp_path, object())
    daemon.bind_demand_sink(MagicMock(offer=MagicMock(return_value=True)))
    daemon._policy = replace(
        daemon._policy,
        entity_profile=replace(daemon._policy.entity_profile, foreground_refresh_wait_seconds=0.01),
    )
    daemon._publish_auth_scope(TelegramAuthScope(AUTH_SCOPE_VERSION, 42, 2, 99))
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
        "reason": "refresh_queued",
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
        "notice": "Account protection status is unavailable; Telegram acquisition state could not be confirmed.",
    }
    readback = sqlite3.connect(feedback_path)
    try:
        row = cast(tuple[str] | None, readback.execute("SELECT message FROM feedback").fetchone())
    finally:
        readback.close()
    assert row == ("saved before status failure",)
    assert mcp_server.tool_by_name["submit_feedback"].output_schema is not None
    validate(payload, cast(dict[str, object], mcp_server.tool_by_name["submit_feedback"].output_schema))
