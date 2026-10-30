"""Automatic recovery identity through the real startup and physical wire owners."""

import asyncio
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram import telegram_demand, telegram_rpc_scheduler
from mcp_telegram.daemon import _acquire_startup_identity_before_updates
from mcp_telegram.dialog_directory import CanonicalDialogDirectory
from mcp_telegram.sync_db import _open_sync_db
from mcp_telegram.telegram_demand import RpcAttemptBudget, demand_context
from mcp_telegram.telegram_rpc import _WireAttempt
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import rpc_attempt_budget
from tests.test_daemon import _ApiStub, _CadenceStub, _typed_ctx
from tests.test_telegram_auto_resume import trial_wire as trial_wire
from tests.test_telegram_rpc_safety import Clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


async def test_consumed_trial_runs_real_startup_self_probe_once(  # noqa: PLR0914, PLR0915
    trial_wire: tuple[_WireHarness, Clock], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (gate, sender, connection, sink), clock = trial_wire
    loop = asyncio.get_running_loop()
    real_time = loop.time
    offset = clock.monotonic_seconds - real_time()
    monkeypatch.setattr(loop, "time", lambda: real_time() + offset)
    monkeypatch.setattr(telegram_demand, "time", clock)
    monkeypatch.setattr(telegram_rpc_scheduler, "time", clock)
    gate._admission_scheduler._clock = loop.time
    conn = _open_sync_db(tmp_path / "sync.db")
    bound: list[int] = []

    class Directory:
        async def bind_account_id(self, account_id: int) -> None:
            bound.append(account_id)

    api = _ApiStub()
    api._client = gate
    ctx = _typed_ctx(conn=conn, client=gate, api_server=api, self_profile_cadence=_CadenceStub())
    task = asyncio.create_task(
        _acquire_startup_identity_before_updates(ctx, cast(CanonicalDialogDirectory, Directory()))
    )
    try:
        waiting = asyncio.create_task(sender._send_queue.next_state())
        done, _ = await asyncio.wait((task, waiting), timeout=3, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            await task
        assert waiting in done, f"identity stalled: {task.get_stack()}"
        state = waiting.result()
        request = state.request
        assert type(request) is functions.users.GetUsersRequest
        assert len(request.id) == 1 and type(request.id[0]) is types.InputUserSelf
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        owner = cast(_WireAttempt, packet.state.owner)
        budget = owner.scope.attempt_budget
        assert owner.scope.owner_task is task
        assert budget is not None and budget.limit == 1 and budget.attempts == 0
        assert sender.write_packet(packet, connection._send)
        assert budget.attempts == 1 and len(sink.frames) == 1
        state.future.set_result([types.User(id=42, is_self=True)])
        result = await asyncio.wait_for(task, 3)
        assert result.identity_only and result.result().own_only_context.account_id == 42
        assert bound == [42] and api.self_id == 42
        assert gate.automatic_trial_active()
        assert len(sink.frames) == 1

        async def ordinary_root() -> object:
            with demand_context(DemandKind.SELF_PROFILE_MAINTENANCE):
                with rpc_attempt_budget(RpcAttemptBudget(1)):
                    return await gate.get_me()

        ordinary = asyncio.create_task(ordinary_root())
        queued = await asyncio.wait_for(sender._send_queue.next_state(), 3)
        preparation = asyncio.create_task(sender._prepare_state(queued))
        await asyncio.sleep(0.05)
        assert not preparation.done() or preparation.exception() is not None
        assert len(sink.frames) == 1 and connection._send_queue.empty()
        await gate.finish_startup_auto_resume()
        assert not gate.automatic_trial_active()
        preparation.cancel()
        await asyncio.gather(preparation, return_exceptions=True)
        ordinary.cancel()
        await asyncio.gather(ordinary, return_exceptions=True)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        conn.close()
