"""Profile admission retains real coordinator work while other demand proceeds."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from telethon import functions, types

from mcp_telegram.daemon_entity_info import DaemonEntityInfoService, _EntityInfoClient
from mcp_telegram.entity_profile.refresh import EntityProfileDemandAdapter
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.telegram_demand import DemandStatus, DurableDemandAdapter
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import SliceOutcomeKind
from mcp_telegram.telegram_gateway import TelethonUserProfileGateway
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.test_entity_profile_full_user_pair import _pair_service, _PairClient, _prepare
from tests.test_profile_slice_deadline import _prepare_self
from tests.test_telegram_demand_coordinator import _adapters
from tests.test_telegram_rpc_scheduler import _ControlledLimiter
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.fixture
async def profile_setup(
    tmp_path: Path, wire: _WireHarness
) -> AsyncIterator[tuple[sqlite3.Connection, DaemonEntityInfoService, EntityProfileDemandAdapter]]:
    gate = wire[0]
    conn, prior = _prepare(tmp_path / "profile.sqlite", migrated=True)
    await prior.shutdown()  # type: ignore[attr-defined]
    _prepare_self(conn)
    writer = await SQLiteWriter.open(tmp_path / "profile.sqlite")
    service = cast(DaemonEntityInfoService, _pair_service(conn, _PairClient(), enabled=True))
    service._deps = replace(
        service._deps,
        writer=writer,
        client=cast(_EntityInfoClient, gate),
        full_user_auth_scope=gate.retained_auth_scope,
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
    profile = EntityProfileDemandAdapter(refresh)
    try:
        yield conn, service, profile
    finally:
        await service.shutdown()
        await writer.aclose()
        conn.close()


async def test_profile_claim_wait_keeps_ticket_and_allows_sibling(
    wire: _WireHarness, profile_setup: tuple[sqlite3.Connection, DaemonEntityInfoService, EntityProfileDemandAdapter]
) -> None:
    gate, sender = wire[:2]
    conn, service, profile = profile_setup
    status = profile.status(100)
    assert status is not None and status.selected is not None
    coordinator, sibling_done, sibling_task = _coordinator(profile)
    limiter = _ControlledLimiter()
    gate._admission_scheduler._limiter = limiter
    pump = asyncio.create_task(coordinator.run())
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        assert isinstance(state.request, functions.users.GetFullUserRequest)
        preparation = asyncio.create_task(sender._prepare_state(state))
        async with asyncio.timeout(2):
            while not limiter.acquisitions:
                await asyncio.sleep(0)
        await asyncio.wait_for(sibling_done.wait(), 2)
        await asyncio.sleep(0.06)
        assert sibling_task == [pump]
        assert not preparation.done()
        assert not wire[3].frames
        invocation = coordinator._invocations[status.selected]
        assert invocation.budget.attempts == 0
        assert coordinator.inspect_work(status.selected)["active_for_target"] is True
        assert conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() != (
            "failed",
        )
        await limiter.allow_one()
        await asyncio.wait_for(preparation, 2)
        assert sender.write_packet(wire[2]._send_queue.get_nowait(), wire[2]._send)
        state.future.set_result(_full_user())
        async with asyncio.timeout(2):
            while conn.execute("SELECT status FROM entity_profile_refresh_state WHERE entity_id=42").fetchone() != (
                "complete",
            ):
                await asyncio.sleep(0)
        assert len(wire[3].frames) == 1 and invocation.budget.attempts == 1
        stored = service._profiles.read(42, now=100)
        assert stored is not None and stored.detail.get("personal_channel_id") == 123
        assert stored.detail.get("about") == "fresh"
        assert conn.execute(
            "SELECT status FROM entity_detail_sections WHERE entity_id=42 AND section='full_profile'"
        ).fetchone() == ("fresh",)
    finally:
        coordinator.shutdown()
        await asyncio.wait_for(pump, 2)


def _coordinator(
    profile: EntityProfileDemandAdapter,
) -> tuple[TelegramDemandCoordinator, asyncio.Event, list[asyncio.Task[object] | None]]:
    sibling_kind = DemandKind.DELTA_GAP_FILL
    fakes = _adapters({sibling_kind: DemandStatus(0)})
    sibling = fakes[sibling_kind]
    sibling.outcome_kind = SliceOutcomeKind.NO_PROGRESS
    sibling_done = asyncio.Event()
    sibling_task: list[asyncio.Task[object] | None] = []

    async def run_sibling() -> None:
        sibling_task.append(asyncio.current_task())
        sibling_done.set()

    sibling.on_run = run_sibling
    adapters = cast(dict[DemandKind, DurableDemandAdapter], dict(fakes))
    adapters[DemandKind.ENTITY_PROFILE_REFRESH] = profile
    return TelegramDemandCoordinator(adapters, clock=lambda: 100), sibling_done, sibling_task


def _full_user() -> types.users.UserFull:
    return types.users.UserFull(
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
        chats=[
            types.Channel(
                id=123, title="Local Channel", username="local_channel", photo=types.ChatPhotoEmpty(), date=None
            )
        ],
    )
