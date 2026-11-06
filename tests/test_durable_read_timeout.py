"""Durable timeout recovery crosses the actual sender and SQLite ledger."""

import asyncio
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import closing
from pathlib import Path
from typing import cast

import pytest
from telethon.tl import functions, types

from mcp_telegram.peer_access import peer_target
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptEvidence,
    RpcReadTimeoutError,
)
from mcp_telegram.telegram_demand_coordinator import TelegramDemandCoordinator
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc import _AdmissionAwareSender, _TransportBoundaryState
from mcp_telegram.telegram_rpc_consumers import DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcScope,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)
from tests.test_telegram_demand_coordinator import _adapters
from tests.test_telegram_rpc_method_wire import _clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


def _read_scope(budget: RpcAttemptBudget) -> TelegramRpcScope:
    return TelegramRpcScope(
        TelegramRpcSource.DELTA_SYNC,
        demand_contract(DemandKind.DELTA_GAP_FILL).service_class,
        time.monotonic() + 10,
        asyncio.current_task(),
        demand_kind=DemandKind.DELTA_GAP_FILL,
        acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
        attempt_budget=budget,
        attempt_evidence=RpcAttemptEvidence(),
    )


@pytest.mark.parametrize("known_response", [False, True])
@pytest.mark.parametrize("cancel_during_receipt", [False, True])
async def test_timeout_race_drains_exact_read_and_receipt_before_release(  # noqa: PLR0915 - physical lifecycle assertions
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, known_response: bool, cancel_during_receipt: bool
) -> None:
    gate, sender, connection, sink = wire
    selected = WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "peer:42"), "r1")
    budget = RpcAttemptBudget(1, selected=selected)
    started, release = asyncio.Event(), asyncio.Event()
    ledger = gate._ensure_safety_ledger()
    original = cast(Callable[..., Awaitable[object]], ledger.complete)

    async def blocked_complete(*args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(ledger, "complete", blocked_complete)

    async def call() -> object:
        return await _AdmissionAwareSender(gate, _read_scope(budget)).send(
            functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0)
        )

    task = asyncio.create_task(call())
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = gate._require_wire_owner(sender.require_owned(state).owner)
    timer = owner.response_timer
    assert timer is not None
    sender._send_queue.append(state)
    try:
        cast(Callable[[], None], object.__getattribute__(timer, "_on_timeout"))()
        if known_response:
            assert timer.expired()
            # Deliver the known result after expiry, before the caller resumes.
            state.future.set_result("known")
        await asyncio.wait_for(started.wait(), 2)
        assert state.future.done() and not task.done()
        assert gate.prepare_durable_retry(budget, owner.method) is None
        assert len(sink.frames) == 1 and budget.attempts == 1
        assert state in sender._owned_states and state.msg_id in sender._pending_state
        assert gate.account_safety_status() is None
        if cancel_during_receipt:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        if cancel_during_receipt:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        elif known_response:
            assert await asyncio.wait_for(task, 2) == "known"
        else:
            with pytest.raises(RpcReadTimeoutError):
                await asyncio.wait_for(task, 2)
        if known_response:
            assert ledger._record["intents"] == {} and ledger._record["methods"] == {}
        else:
            intents = cast(dict[str, dict[str, object]], ledger._record["intents"])
            assert cast(dict[str, object], next(iter(intents.values()))["read_retry"])["reason"] == "sender_timeout"
            assert gate.rpc_eligibility(owner.method) is not None
            assert gate.account_safety_status() is None
        assert state not in sender._owned_states and state.msg_id not in sender._pending_state
        assert state not in sender._send_queue._deque
        assert len(sink.frames) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_known_durable_response_disables_parent_deadline_during_sqlite_receipt(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, _sink = wire
    started, release = asyncio.Event(), asyncio.Event()
    ledger = gate._ensure_safety_ledger()
    original = cast(Callable[..., Awaitable[object]], ledger.complete)

    async def blocked_complete(*args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(ledger, "complete", blocked_complete)
    budget = RpcAttemptBudget(1, selected=WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "peer:42"), "r1"))

    async def call() -> object:
        return await _AdmissionAwareSender(gate, _read_scope(budget)).send(
            functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0)
        )

    task = asyncio.create_task(call())
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = gate._require_wire_owner(sender.require_owned(state).owner)
    timer = owner.response_timer
    assert timer is not None and timer.when() is not None
    try:
        state.future.set_result("known")
        await asyncio.wait_for(started.wait(), 2)
        assert timer.when() is None and not task.done()
        assert gate.prepare_durable_retry(budget, owner.method) is None
    finally:
        release.set()
    assert await asyncio.wait_for(task, 2) == "known"
    assert ledger._record["intents"] == {} and ledger._record["methods"] == {}


async def test_claimed_sender_timeout_retries_unchanged_revision_only_after_gateway_floor(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    from mcp_telegram import telegram_demand, telegram_rpc_scheduler

    monkeypatch.setattr(telegram_demand, "time", clock)
    monkeypatch.setattr(telegram_rpc_scheduler, "time", clock)
    kind = DemandKind.DELTA_GAP_FILL
    adapters = _adapters({kind: DemandStatus(0)})
    adapter = adapters[kind]
    selected = WorkRevision(WorkKey(kind, 42, "peer:42"), "r1")
    adapter.selected, adapter.selections = selected, [selected]
    coordinator = TelegramDemandCoordinator(
        cast(Mapping[DemandKind, DurableDemandAdapter], adapters),
        clock=clock.time,
        monotonic_clock=clock.monotonic,
        rpc_eligibility=gate.rpc_eligibility,
        prepare_durable_retry=gate.prepare_durable_retry,
    )
    gate.set_rpc_eligibility_observer(coordinator.eligibility_changed)

    async def acquire() -> None:
        with rpc_scope(TelegramRpcSource.DELTA_SYNC, acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE):
            with rpc_attempt_budget(adapter.run_calls[-1]), peer_target(42):
                await gate(functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0))

    adapter.on_run = acquire
    first = asyncio.create_task(coordinator._execute_slice(kind))
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = gate._require_wire_owner(sender.require_owned(state).owner)
    assert owner.response_timer is not None
    owner.response_timer.reschedule(asyncio.get_running_loop().time() - 1)
    await asyncio.wait_for(first, 2)
    budget = adapter.run_calls[0]
    assert budget.attempts == budget.limit == 1
    assert coordinator.inspect_work(selected)["hold_kind"] == "gate_wait"
    assert state not in sender._owned_states and gate.account_safety_status() is None
    coordinator._active_kind = None
    clock.advance(299)
    coordinator.scan()
    assert kind not in coordinator.ready_kinds
    clock.advance(2)
    coordinator.scan()
    assert kind in coordinator.ready_kinds
    second = asyncio.create_task(coordinator._execute_slice(kind))
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result("ok")
    await asyncio.wait_for(second, 2)
    assert adapter.run_calls == [budget, budget] and budget.attempts == 2
    assert len(sink.frames) == 2 and selected not in coordinator._invocations


@pytest.mark.parametrize("invalid_scope", ["account_switch", "shutdown"])
async def test_sender_timeout_cannot_contain_old_or_stopping_read(wire: _WireHarness, invalid_scope: str) -> None:
    gate, sender, connection, sink = wire
    budget = RpcAttemptBudget(1, selected=WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "peer:42"), "r1"))

    async def call() -> object:
        return await _AdmissionAwareSender(gate, _read_scope(budget)).send(
            functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0)
        )

    task = asyncio.create_task(call())
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = gate._require_wire_owner(sender.require_owned(state).owner)
    if invalid_scope == "account_switch":
        assert gate.session is not None
        gate.session.set_dc(3, "127.0.0.1", 443)
    else:
        gate._transport_state = _TransportBoundaryState.DISCONNECTING
    assert owner.response_timer is not None
    owner.response_timer.reschedule(asyncio.get_running_loop().time() - 1)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task, 2)
    assert not state.future.done() and state in sender._owned_states
    assert budget._read_budget_failure_method is None and len(sink.frames) == 1
    assert gate._ensure_safety_ledger()._record["methods"] == {}


@pytest.mark.parametrize("after_read_timeout", [False, True])
async def test_zero_claim_expired_admission_rearms_same_revision_and_unused_credit(  # noqa: PLR0914, PLR0915 - exact root and physical claims
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, after_read_timeout: bool
) -> None:
    from mcp_telegram import telegram_demand, telegram_rpc_scheduler
    from mcp_telegram.telegram_demand_progress import SliceOutcomeKind

    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    monkeypatch.setattr(telegram_demand, "time", clock)
    monkeypatch.setattr(telegram_rpc_scheduler, "time", clock)
    kind, sibling = DemandKind.DELTA_GAP_FILL, DemandKind.HOT_ACTIVITY_PAGE
    adapters = _adapters({kind: DemandStatus(0), sibling: DemandStatus(0)})
    adapter = adapters[kind]
    selected = WorkRevision(WorkKey(kind, 42, "peer:42"), "r1")
    adapter.selected, adapter.selections = selected, [selected]
    adapters[sibling].outcome_kind = SliceOutcomeKind.NO_PROGRESS
    coordinator = TelegramDemandCoordinator(
        cast(Mapping[DemandKind, DurableDemandAdapter], adapters),
        clock=clock.time,
        monotonic_clock=clock.monotonic,
        rpc_eligibility=gate.rpc_eligibility,
        prepare_durable_retry=gate.prepare_durable_retry,
    )
    gate.set_rpc_eligibility_observer(coordinator.eligibility_changed)
    expiry_run = 2 if after_read_timeout else 1

    async def acquire() -> None:
        if len(adapter.run_calls) == expiry_run:
            # A local prelude consumes admission time without a current-root claim.
            clock.advance(demand_contract(kind).admission_timeout_seconds + 1)
        with rpc_scope(TelegramRpcSource.DELTA_SYNC, acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE):
            with rpc_attempt_budget(adapter.run_calls[-1]), peer_target(42):
                await gate(functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0))

    adapter.on_run = acquire
    if after_read_timeout:
        first = asyncio.create_task(coordinator._execute_slice(kind))
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        owner = gate._require_wire_owner(sender.require_owned(state).owner)
        assert owner.response_timer is not None
        cast(Callable[[], None], object.__getattribute__(owner.response_timer, "_on_timeout"))()
        await asyncio.wait_for(first, 2)
        coordinator._active_kind = None
        clock.advance(301)
        coordinator.scan()
        assert kind in coordinator.ready_kinds
    await asyncio.wait_for(coordinator._execute_slice(kind), 2)
    budget = adapter.run_calls[0]
    expected_attempts = int(after_read_timeout)
    assert budget.attempts == expected_attempts and budget.limit == 1
    assert coordinator.inspect_work(selected)["hold_kind"] == "gate_wait"
    coordinator._active_kind = None
    await coordinator._execute_slice(sibling)
    assert len(adapters[sibling].run_calls) == 1
    coordinator._active_kind = None
    clock.advance(gate._scheduler_policy.admission_retry_seconds + 1)
    coordinator.scan()
    assert kind in coordinator.ready_kinds
    final = asyncio.create_task(coordinator._execute_slice(kind))
    state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result("ok")
    await asyncio.wait_for(final, 2)
    assert all(value is budget for value in adapter.run_calls)
    assert budget.attempts == expected_attempts + 1 and len(sink.frames) == expected_attempts + 1
    assert selected not in coordinator._invocations


async def test_real_hot_adapter_timeout_retry_and_commit_share_safe_receipts(  # noqa: PLR0914, PLR0915 - real cross-layer composition
    wire: _WireHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp_telegram import telegram_demand, telegram_rpc_scheduler
    from mcp_telegram.activity_hot_sweep import HotActivityDemandAdapter
    from mcp_telegram.activity_peer_sweep import enroll_activity_dialog
    from mcp_telegram.config import ActivityHotSweepConfig, RuntimeObservationConfig
    from mcp_telegram.image_authorization import ScopeToken
    from mcp_telegram.rpc_admission_observations import RpcAdmissionObservationAggregator, work_references
    from mcp_telegram.runtime_observations import RuntimeObservationSink
    from mcp_telegram.sqlite_writer import SQLiteWriter
    from mcp_telegram.state import telegram_session_lease
    from sqlite_writer_helpers import inline_writer

    gate, sender, connection, frames = wire
    clock = _clock(wire, monkeypatch)
    monkeypatch.setattr(telegram_demand, "time", clock)
    monkeypatch.setattr(telegram_rpc_scheduler, "time", clock)

    class Client:
        async def __call__(self, request: object) -> object:
            return await gate(request)

        async def get_input_entity(self, dialog_id: int) -> object:
            del dialog_id
            return types.InputPeerSelf()

        def capture_image_scope(self) -> ScopeToken | None:
            return gate.capture_image_scope()

        def claim_image_scope(self, token: ScopeToken) -> bool:
            return gate.claim_image_scope(token)

    with telegram_session_lease(tmp_path) as lease:
        gate.bind_session_lease(lease)
        path = tmp_path / "sync.db"
        conn = sqlite3.connect(path)
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.commit()
        enroll_activity_dialog(conn, 42, "user", last_activity_at=int(clock.time()))
        conn.execute("UPDATE activity_dialog_state SET hot_next_due_at=0 WHERE dialog_id=42")
        conn.commit()
        observer_writer = await SQLiteWriter.open(path)
        observations = RuntimeObservationSink(
            path, writer=observer_writer, retention_ttl_seconds=3600, policy=RuntimeObservationConfig()
        )
        observer = RpcAdmissionObservationAggregator(
            observations, policy=RuntimeObservationConfig(), method_names=frozenset()
        )
        kind = DemandKind.HOT_ACTIVITY_PAGE
        adapters: dict[DemandKind, DurableDemandAdapter] = dict(_adapters())
        adapters[kind] = HotActivityDemandAdapter(
            Client(),
            conn,
            asyncio.Event(),
            ActivityHotSweepConfig(jitter_max_seconds=0),
            1,
            writer=inline_writer(conn),
            account_id=42,
        )
        coordinator = TelegramDemandCoordinator(
            adapters,
            clock=clock.time,
            monotonic_clock=clock.monotonic,
            rpc_eligibility=gate.rpc_eligibility,
            prepare_durable_retry=gate.prepare_durable_retry,
            observer=observer,
        )
        gate.set_rpc_eligibility_observer(coordinator.eligibility_changed)
        status = adapters[kind].status(clock.time())
        assert status is not None and status.selected is not None and status.selected.key.target == "peer:42"
        selected = status.selected
        release = asyncio.Event()
        try:
            first = asyncio.create_task(coordinator._execute_slice(kind))
            state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
            await sender._prepare_state(state)
            assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
            owner = gate._require_wire_owner(sender.require_owned(state).owner)
            assert owner.response_timer is not None
            cast(Callable[[], None], object.__getattribute__(owner.response_timer, "_on_timeout"))()
            await asyncio.wait_for(first, 2)
            budget = coordinator._invocations[selected].budget
            assert budget.attempts == 1 and budget.limit == demand_contract(kind).max_rpc_attempts_per_slice
            assert coordinator.inspect_work(selected)["hold_kind"] == "gate_wait"
            assert conn.execute(
                "SELECT hot_next_retry_at FROM activity_dialog_state WHERE dialog_id=42"
            ).fetchone() == (None,)
            coordinator._active_kind = None
            clock.advance(301)
            coordinator.scan()
            assert kind in coordinator.ready_kinds
            ledger = gate._ensure_safety_ledger()
            original = cast(Callable[..., Awaitable[object]], ledger.complete)
            started = asyncio.Event()

            async def blocked_complete(*args: object, **kwargs: object) -> object:
                started.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(ledger, "complete", blocked_complete)
            second = asyncio.create_task(coordinator._execute_slice(kind))
            state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
            await sender._prepare_state(state)
            assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
            state.future.set_result(types.messages.Messages(messages=[], chats=[], users=[], topics=[]))
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.sleep(1.05)
            assert not second.done() and state.future.done() and budget.attempts == 2
            release.set()
            await asyncio.wait_for(second, 2)
            assert selected not in coordinator._excluded and selected not in coordinator._invocations
            assert len(frames.frames) == 2
            assert (
                conn.execute("SELECT hot_last_sync_at FROM activity_dialog_state WHERE dialog_id=42").fetchone()[0]
                is not None
            )
        finally:
            release.set()
            await observations.aclose()
            await observer_writer.aclose()
            conn.close()
        with closing(sqlite3.connect(path)) as reopened:
            rows = cast(
                list[tuple[str, str]],
                reopened.execute(
                    "SELECT reason_code,payload_json FROM runtime_observations "
                    "WHERE kind='telegram.demand' AND outcome='slice_receipt' ORDER BY id"
                ).fetchall(),
            )
        assert [row[0] for row in rows] == ["deferred", "terminal"]
        failed, committed = (cast(dict[str, object], json.loads(row[1])) for row in rows)
        assert (failed["work_ref"], failed["selected_ref"]) == work_references(selected)
        assert failed["work_ref"] == committed["work_ref"] and failed["selected_ref"] == committed["selected_ref"]
        assert failed["hold_kind"] == "gate_wait" and "committed_ref" not in failed
        assert committed["committed_ref"] != committed["selected_ref"] and committed["hold_kind"] == "none"
        assert [failed["actual_attempts"], committed["actual_attempts"]] == [1, 2]
        assert failed["root_attempts"] == committed["root_attempts"] == 1
        assert cast(float, failed["duration_ms"]) >= 0 and cast(float, committed["duration_ms"]) >= 0
        assert not {"account_id", "dialog_id", "target", "message", "content"}.intersection(failed)


async def test_read_timeout_does_not_shorten_real_hour_flood_floor(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from telethon.errors import FloodWaitError

    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    budget = RpcAttemptBudget(1, selected=WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, "peer:42"), "r1"))
    request = functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0)

    async def call() -> object:
        return await _AdmissionAwareSender(gate, _read_scope(budget)).send(request)

    task = asyncio.create_task(call())
    first = await asyncio.wait_for(sender._send_queue.next_state(), 2)
    await sender._prepare_state(first)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    owner = gate._require_wire_owner(sender.require_owned(first).owner)
    gate._admission_scheduler._next_write_at = 0.0
    other = sender.send_owned(request, _read_scope(RpcAttemptBudget(1)))
    second = await sender._send_queue.next_state()
    await sender._prepare_state(second)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    second.future.set_exception(FloodWaitError(request, capture=3600))
    with pytest.raises(FloodWaitError):
        await other
    assert owner.response_timer is not None
    cast(Callable[[], None], object.__getattribute__(owner.response_timer, "_on_timeout"))()
    with pytest.raises(RpcReadTimeoutError):
        await asyncio.wait_for(task, 2)
    episode = cast(dict[str, dict[str, object]], gate._ensure_safety_ledger()._record["methods"])[owner.method]
    assert episode["consecutive_actual_floods"] == episode["consecutive_read_budget_failures"] == 1
    clock.advance(3599)
    assert gate.rpc_eligibility(owner.method) is not None and gate.prepare_durable_retry(budget, owner.method) is None
    clock.advance(2)
    assert gate.rpc_eligibility(owner.method) is None and gate.prepare_durable_retry(budget, owner.method) is not None
    assert budget.attempts == budget.limit == 1 and len(sink.frames) == 2
