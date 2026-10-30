"""Real profile work crosses the production gate only after physical admission."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon import types

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare
from tests.test_profile_slice_deadline import _prepare_self
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["success", "timeout"])
async def test_profile_waits_for_physical_claim_then_commits_self_pair(
    tmp_path: Path, wire: _WireHarness, response: str
) -> None:
    gate, sender, connection, sink = wire
    conn, prior = _prepare(tmp_path / "profile.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    _prepare_self(conn)
    writer = await SQLiteWriter.open(tmp_path / "profile.sqlite")
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(), enabled=True))
    service._deps = replace(
        service._deps,
        writer=writer,
        self_id=42,
        user_profile_port=TelethonUserProfileGateway(gate, now_provider=lambda: 100),
        refresh_limits=replace(
            service._deps.refresh_limits,
            foreground_resolve_seconds=0.005,
            per_rpc_seconds=0.02,
            whole_refresh_seconds=0.03,
        ),
    )
    refresh = service.refresh_coordinator
    assert refresh is not None
    refresh._limits = service._deps.refresh_limits
    status = EntityProfileDemandAdapter(refresh).status(100)
    assert status is not None and status.selected is not None
    budget = RpcAttemptBudget(limit=1, selected=status.selected)
    task = asyncio.create_task(EntityProfileDemandAdapter(refresh).run_slice(budget))
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await asyncio.sleep(0.06)
        assert not task.done() and not sink.frames and budget.attempts == 0
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() != (
            "failed",
        )
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        if response == "timeout":
            result = await asyncio.wait_for(task, 2)
            assert result.kind.value == "retryable_failure"
            assert budget.attempts == 1
            assert conn.execute(
                "SELECT status,reason FROM entity_profile_refresh_state WHERE entity_id=42"
            ).fetchone() == ("failed", "timeouterror")
            return
        state.future.set_result(
            SimpleNamespace(
                full_user=types.UserFull(
                    id=42,
                    settings=types.PeerSettings(),
                    notify_settings=types.PeerNotifySettings(),
                    common_chats_count=0,
                    about="fresh",
                    personal_channel_id=123,
                    personal_channel_message=9,
                ),
                users=[types.User(id=42, first_name="Target")],
                chats=[SimpleNamespace(id=123, title="Local Channel", username="local_channel")],
            )
        )
        assert await task is not None
        assert len(sink.frames) == 1 and budget.attempts == 1
        profile = service._profiles.read(42, now=100)
        assert profile is not None and profile.detail.get("personal_channel_id") == 123
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() == (
            "complete",
        )
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await service.shutdown()
        await writer.aclose()
        conn.close()
