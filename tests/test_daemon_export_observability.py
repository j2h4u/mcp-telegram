"""Content-free export outcomes and bounded event-loop stall diagnostics."""
# pyright: reportAny=false

import asyncio
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_telegram.daemon_api import DaemonAPIServer, _export_loop_stack, _export_observation_payload
from mcp_telegram.request_timing import DaemonRequestTiming, timing_phase
from mcp_telegram.runtime_observations import encode_payload
from tests.test_daemon_api import make_server


@pytest.fixture
def server() -> Iterator[DaemonAPIServer]:
    feedback = sqlite3.connect(":memory:")
    instance = make_server(feedback_conn=feedback)
    instance.bind_runtime_observation_sink(MagicMock())
    try:
        yield instance
    finally:
        instance._conn.close()
        feedback.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"ok": True, "data": {"items": [{"text": "PRIVATE"}]}}, "success"),
        ({"ok": False, "error": "export_deferred", "reason": "admission", "retry_after": 5}, "failed"),
        ({"ok": False, "error": "export_failed", "reason": "TimeoutError"}, "failed"),
        ({"ok": True, "data": {"status": "unavailable", "reason": "ChannelPrivateError"}}, "success"),
    ],
)
async def test_export_outcome_correlates_safe_control_metadata(
    server: DaemonAPIServer, response: dict[str, object], expected: str, caplog: pytest.LogCaptureFixture
) -> None:
    req = {"operation": "history", "dialog_id": -123, "before_id": 50, "selector": "PRIVATE"}
    caplog.set_level("INFO")

    async def dispatch(*_args: object, **_kwargs: object) -> dict[str, object]:
        with timing_phase("rpc_admission"):
            await asyncio.sleep(0.001)
        with timing_phase("rpc_execution"):
            await asyncio.sleep(0.001)
        return response

    with patch.object(server, "_dispatch_with_error_projection", AsyncMock(side_effect=dispatch)):
        result = await server._dispatch_request_with_timing(req, "export_chat", "1234abcd", "a" * 32)
    assert result == response
    sink = server._runtime_observation_sink
    assert isinstance(sink, MagicMock)
    observations = [call.kwargs for call in sink.record.call_args_list]
    assert [item["outcome"] for item in observations] == ["started", expected]
    payload = observations[-1]["payload"]
    assert payload["request_id"] == "1234abcd"
    assert payload["operation_id"] == "a" * 32
    assert payload["dialog_id"] == -123
    assert payload["before_id"] == 50
    assert payload["rpc_admission_ms"] > 0
    assert payload["rpc_execution_ms"] > 0
    assert "PRIVATE" not in json.dumps(observations) + caplog.text
    data = response.get("data")
    expected_reason = data.get("reason") if isinstance(data, dict) else None
    assert payload.get("reason") == response.get("reason", expected_reason)
    await asyncio.sleep(0.01)
    assert not any(thread.name == "export-loop-watchdog" for thread in threading.enumerate())


@pytest.mark.asyncio
async def test_export_cancellation_is_recorded_and_watchdog_stops(server: DaemonAPIServer) -> None:
    with patch.object(server, "_dispatch_with_error_projection", AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await server._dispatch_request_with_timing({"operation": "history"}, "export_chat", None, "a" * 32)
    sink = server._runtime_observation_sink
    assert isinstance(sink, MagicMock)
    assert sink.record.call_args.kwargs["outcome"] == "cancelled"
    await asyncio.sleep(0.01)
    assert not any(thread.name == "export-loop-watchdog" for thread in threading.enumerate())


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_watchdog_distinguishes_loop_block_from_async_wait(
    server: DaemonAPIServer, blocked: bool, caplog: pytest.LogCaptureFixture
) -> None:
    timing = DaemonRequestTiming(operation_id="a" * 32, request_id="1234abcd")
    with patch("mcp_telegram.daemon_api._EXPORT_LOOP_STALL_SECONDS", 0.02):
        with server._observe_export_loop({"operation": "history"}, timing):
            if blocked:
                time.sleep(0.08)
            else:
                await asyncio.sleep(0.08)
    await asyncio.sleep(0.01)
    sink = server._runtime_observation_sink
    assert isinstance(sink, MagicMock)
    stalled = [call.kwargs for call in sink.record.call_args_list if call.kwargs["outcome"] == "loop_stalled"]
    assert len(stalled) == int(blocked)
    if blocked:
        assert "test_watchdog_distinguishes_loop_block_from_async_wait" in caplog.text
        assert "test_daemon_export_observability.py" in stalled[0]["payload"]["stack"]
    assert not any(thread.name == "export-loop-watchdog" for thread in threading.enumerate())


def test_export_late_completion_and_maximum_metadata_fit_sink_budget() -> None:
    timing = DaemonRequestTiming(operation_id="a" * 64, request_id="1234abcd", started_at=time.monotonic() - 143)
    req: dict[str, object] = dict.fromkeys(
        ("dialog_id", "message_id", "user_id", "topic_id", "before_id", "min_id", "upper_id"), 2**63 - 1
    )
    req["operation"] = "history"
    payload = _export_observation_payload(req, timing, {"ok": True}, cancelled=False)
    assert payload["deadline_exceeded"] is True
    assert payload["outcome"] == "success"
    encode_payload(payload)
    payload.update({"outcome": "loop_stalled", "reason": "loop_stalled", "loop_delay_ms": 15000, "stack": "x" * 200})
    encode_payload(payload)
    assert "PRIVATE_LOCAL_VALUE" not in _export_loop_stack(threading.get_ident())
