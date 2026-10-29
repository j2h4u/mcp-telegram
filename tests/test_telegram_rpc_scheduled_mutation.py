"""Native scheduled writes are admitted only through their one-send root."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal, cast

import pytest
from telethon.tl import functions, types

from mcp_telegram import telegram_rpc
from mcp_telegram.scheduled_write_fence import (
    ScheduledMutationCheck,
    ScheduledMutationFence,
    ScheduledMutationIntent,
    ScheduledMutationToken,
    ScheduledWriteRejected,
    scheduled_mutation_context,
)
from mcp_telegram.telegram_demand import (
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    RpcAttemptEvidence,
    demand_context,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcScope,
    UnclassifiedTelegramRpcError,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from mcp_telegram.telegram_transport import _OwnedPacket
from tests.test_telegram_rpc_safety import Clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


def _clock(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> Clock:
    clock = Clock(time.time(), time.monotonic())
    monkeypatch.setattr(telegram_rpc, "time", clock)
    monkeypatch.setattr(wire[0]._admission_scheduler, "_clock", clock.monotonic)
    return clock


def _scheduled_request(action: str, schedule_at: int) -> object:
    peer = types.InputPeerSelf()
    if action == "create":
        return functions.messages.SendMessageRequest(
            peer, "scheduled", random_id=98765, schedule_date=datetime.fromtimestamp(schedule_at, UTC)
        )
    if action == "update":
        return functions.messages.EditMessageRequest(
            peer, 321, message="edited", schedule_date=datetime.fromtimestamp(schedule_at, UTC)
        )
    return functions.messages.DeleteScheduledMessagesRequest(peer, [321])


def _check(action: str, schedule_at: int, fence: ScheduledMutationFence) -> ScheduledMutationCheck:
    intent = ScheduledMutationIntent(
        account_id=42,
        dialog_id=42,
        action=cast(Literal["create", "update", "cancel"], action),
        operation_key=f"{action}-key",
        request_fingerprint=f"fingerprint-{action}",
        expected_observation_order=7,
        scheduled_message_id=None if action == "create" else 321,
        schedule_at_utc=None if action == "cancel" else schedule_at,
        random_id=98765 if action == "create" else None,
    )

    async def revalidate() -> ScheduledMutationToken:
        return fence.watch(42, 42, intent.scheduled_message_id)

    return ScheduledMutationCheck(intent, revalidate)


async def _prepare(
    wire: _WireHarness,
    action: str,
    schedule_at: int,
    check: ScheduledMutationCheck,
    budget: RpcAttemptBudget,
) -> tuple[asyncio.Future[object], _OwnedPacket, RpcAttemptEvidence]:
    _, sender, connection, _ = wire
    with demand_context(DemandKind.SCHEDULED_MUTATION) as root:
        with rpc_attempt_budget(budget):
            with rpc_scope(TelegramRpcSource.SCHEDULED_MUTATION):
                with scheduled_mutation_context(check):
                    response = sender.send_owned(_scheduled_request(action, schedule_at), current_rpc_scope())
                    state = await sender._send_queue.next_state()
                    await sender._prepare_state(state)
                    assert root.attempt_evidence is not None
                    return response, connection._send_queue.get_nowait(), root.attempt_evidence


async def _complete(wire: _WireHarness, action: str, schedule_at: int) -> None:
    _, sender, connection, sink = wire
    fence = ScheduledMutationFence()
    check = _check(action, schedule_at, fence)
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, action, schedule_at, check, budget)
    assert sender.write_packet(packet, connection._send)
    packet.state.state.future.set_result(True)
    assert await response is True
    assert len(sink.frames) == 1
    assert budget.attempts == 1 and evidence.scheduled_mutation_dispatched


async def _expect_refusal(response: asyncio.Future[object], observing: set[asyncio.Task[None]], code: str) -> None:
    done, _ = await asyncio.wait({response}, timeout=15)
    assert response in done, [
        [(frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name) for frame in task.get_stack()]
        for task in observing
    ]
    with pytest.raises(ScheduledWriteRejected, match=code):
        response.result()


@pytest.mark.parametrize("action", ["create", "update", "cancel"])
async def test_scheduled_methods_dispatch_once_under_the_explicit_root(wire: _WireHarness, action: str) -> None:
    await _complete(wire, action, int(time.time()) + 300)


@pytest.mark.parametrize("remaining", [60.0, 59.999])
async def test_native_schedule_lead_boundary_is_checked_before_claim_accounting(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, remaining: float
) -> None:
    clock = _clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    schedule_at = int(clock.time()) + 300
    check = _check("create", schedule_at, ScheduledMutationFence())
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, "create", schedule_at, check, budget)
    clock.utc = schedule_at - remaining

    dispatched = sender.write_packet(packet, connection._send)
    assert dispatched is (remaining >= 60)
    if dispatched:
        packet.state.state.future.set_result(True)
        assert await response is True
    else:
        await _expect_refusal(response, sender._observing, "scheduled_window_expired")
    assert len(sink.frames) == budget.attempts == evidence.actual_attempts == int(dispatched)
    assert evidence.scheduled_mutation_dispatched is dispatched
    assert gate.account_safety_status() is None


@pytest.mark.parametrize("crossing", ["after_prepare", "during_revalidation"])
async def test_schedule_lead_is_rechecked_at_claim_after_waits_without_protecting_transport(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, crossing: str
) -> None:
    clock = _clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    schedule_at = int(clock.time()) + 300
    fence = ScheduledMutationFence()
    check = _check("create", schedule_at, fence)
    if crossing == "during_revalidation":

        async def revalidate() -> ScheduledMutationToken:
            clock.advance(schedule_at - clock.time() - 59)
            return fence.watch(42, 42, None)

        check = ScheduledMutationCheck(check.intent, revalidate)
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, "create", schedule_at, check, budget)
    if crossing == "after_prepare":
        clock.advance(schedule_at - clock.time() - 59)
    assert not sender.write_packet(packet, connection._send)
    await _expect_refusal(response, sender._observing, "scheduled_window_expired")
    await asyncio.sleep(0)
    assert sink.frames == []
    assert budget.attempts == 0 and evidence.actual_attempts == 0
    assert gate.account_safety_status() is None
    assert gate._transport_state.value == "ready"
    assert not gate._ensure_safety_ledger()._record["intents"]


async def test_invalidated_version_after_async_prepare_is_refused_without_wire_or_latch(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    schedule_at = int(clock.time()) + 300
    fence = ScheduledMutationFence()
    check = _check("update", schedule_at, fence)
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, "update", schedule_at, check, budget)
    fence.invalidate_message(42, 42, 321)
    assert not sender.write_packet(packet, connection._send)
    await _expect_refusal(response, sender._observing, "scheduled_version_changed")
    await asyncio.sleep(0)
    assert sink.frames == []
    assert budget.attempts == 0 and evidence.actual_attempts == 0
    assert gate.account_safety_status() is None
    assert not gate._ensure_safety_ledger()._record["intents"]


async def test_known_pre_dispatch_refusal_allows_a_healthy_followup(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    schedule_at = int(clock.time()) + 300
    check = _check("create", schedule_at, ScheduledMutationFence())
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, "create", schedule_at, check, budget)
    clock.utc += schedule_at - clock.time() - 59
    assert not sender.write_packet(packet, connection._send)
    await _expect_refusal(response, sender._observing, "scheduled_window_expired")
    assert sink.frames == [] and budget.attempts == 0 and evidence.actual_attempts == 0

    await _complete(wire, "create", int(clock.time()) + 300)
    assert len(sink.frames) == 1
    assert gate._transport_state.value == "ready"
    assert gate.account_safety_status() is None


async def _reject_fresh_budget_replay(
    wire: _WireHarness,
    owner: object,
    check: ScheduledMutationCheck,
    schedule_at: int,
) -> RpcAttemptBudget:
    _, sender, connection, _ = wire
    fresh_budget = RpcAttemptBudget(1)
    replay_scope = replace(cast(telegram_rpc._WireAttempt, owner).scope, attempt_budget=fresh_budget)
    with scheduled_mutation_context(check):
        replay_response = sender.send_owned(_scheduled_request("create", schedule_at), replay_scope)
        replay_state = await sender._send_queue.next_state()
        await sender._prepare_state(replay_state)
    replay_packet = connection._send_queue.get_nowait()
    assert not sender.write_packet(replay_packet, connection._send)
    with pytest.raises(ScheduledWriteRejected, match="scheduled_attempt_already_dispatched"):
        await replay_response
    await asyncio.sleep(0)
    return fresh_budget


async def test_unknown_scheduled_write_cannot_be_replayed_with_same_or_fresh_budget(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    schedule_at = int(time.time()) + 300
    check = _check("create", schedule_at, ScheduledMutationFence())
    budget = RpcAttemptBudget(1)
    response, packet, evidence = await _prepare(wire, "create", schedule_at, check, budget)
    assert sender.write_packet(packet, connection._send)
    owner = packet.state.owner

    # The already-debited budget rejects a same-owner vendor replay in prepare.
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await gate.prepare_write(owner)

    # Rebinding the same root evidence to a fresh budget cannot authorize a
    # second native mutation either.
    fresh_budget = await _reject_fresh_budget_replay(wire, owner, check, schedule_at)

    packet.state.state.future.set_exception(ConnectionError("fixture lost the response"))
    with pytest.raises(ConnectionError, match="lost the response"):
        await response
    assert len(sink.frames) == 1
    assert budget.attempts == 1 and fresh_budget.attempts == 0
    assert evidence.actual_attempts == 1 and evidence.scheduled_mutation_dispatched
    status = gate.account_safety_status()
    assert status is not None and status["unresolved_request_count"] == 1
    assert gate._transport_state.value == "ready"


async def test_unexpected_claim_exception_still_protects_transport(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(wire, monkeypatch)
    gate, sender, connection, sink = wire
    schedule_at = int(clock.time()) + 300
    check = _check("create", schedule_at, ScheduledMutationFence())
    budget = RpcAttemptBudget(1)
    response, packet, _ = await _prepare(wire, "create", schedule_at, check, budget)

    def unexpected(*_args: object) -> bool:
        raise RuntimeError("unexpected claim invariant")

    monkeypatch.setattr(gate, "claim_write", unexpected)
    assert not sender.write_packet(packet, connection._send)
    with pytest.raises(RuntimeError, match="unexpected claim invariant"):
        await response
    assert sink.frames == []
    assert gate.account_safety_status() is not None


def test_item_invalidation_preserves_create_watches_but_dialog_and_account_invalidate_them() -> None:
    fence = ScheduledMutationFence()
    create = fence.watch(42, 100, None)
    item = fence.watch(42, 100, 321)
    fence.invalidate_message(42, 100, 321)
    assert create.is_valid_for(ScheduledMutationIntent(42, 100, "create", "k", "f", 0, None, 1234, -123))
    assert not item.is_valid_for(ScheduledMutationIntent(42, 100, "update", "k", "f", 0, 321, 1234, None))
    fence.invalidate_dialog(42, 100)
    assert create.invalidated.is_set()
    other_account = fence.watch(43, 100, None)
    fence.invalidate_account(42)
    assert not other_account.invalidated.is_set()


def test_scheduled_mutation_intent_rejects_subsecond_schedule_precision() -> None:
    with pytest.raises(ValueError, match="whole-second precision"):
        ScheduledMutationIntent(42, 100, "create", "k", "f", 0, None, 1234.5, -123)


@pytest.mark.parametrize("bad_method", ["missing_check", "send_scheduled"])
async def test_scheduled_root_rejects_missing_intent_and_immediate_flush(wire: _WireHarness, bad_method: str) -> None:
    gate = wire[0]
    scope: TelegramRpcScope
    with demand_context(DemandKind.SCHEDULED_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
        with rpc_scope(TelegramRpcSource.SCHEDULED_MUTATION):
            scope = current_rpc_scope()
            request = (
                functions.messages.DeleteScheduledMessagesRequest(types.InputPeerSelf(), [321])
                if bad_method == "missing_check"
                else functions.messages.SendScheduledMessagesRequest(types.InputPeerSelf(), [321])
            )
            with pytest.raises(UnclassifiedTelegramRpcError):
                gate.register_application(request, bytes(request), scope)


@pytest.mark.parametrize(
    "fault", ["no_root", "bad_source", "wide_budget", "missing_schedule", "online_sentinel", "clear_draft"]
)
async def test_scheduled_write_rejects_bypass_or_unsafe_create_fields(wire: _WireHarness, fault: str) -> None:
    gate = wire[0]
    schedule_at = int(time.time()) + 300
    fence = ScheduledMutationFence()
    check = _check("create", schedule_at, fence)
    request = cast(functions.messages.SendMessageRequest, _scheduled_request("create", schedule_at))
    if fault == "missing_schedule":
        request.schedule_date = None
    elif fault == "online_sentinel":
        request.schedule_date = datetime.fromtimestamp(0x7FFFFFFE, UTC)
    elif fault == "clear_draft":
        request.clear_draft = True
    budget = RpcAttemptBudget(2 if fault == "wide_budget" else 1)
    root = DemandKind.MCP_REMOTE_ACQUISITION if fault == "no_root" else DemandKind.SCHEDULED_MUTATION
    source = TelegramRpcSource.MCP_INTERACTIVE if fault == "no_root" else TelegramRpcSource.SCHEDULED_MUTATION
    with demand_context(root):
        with rpc_attempt_budget(budget):
            with rpc_scope(source):
                scope = current_rpc_scope()
                if fault == "no_root":
                    with pytest.raises(UnclassifiedTelegramRpcError):
                        gate.register_application(request, bytes(request), scope)
                    return
                if fault == "bad_source":
                    scope = replace(scope, source=TelegramRpcSource.MCP_INTERACTIVE)
                    with scheduled_mutation_context(check), pytest.raises(UnclassifiedTelegramRpcError):
                        gate.register_application(request, bytes(request), scope)
                    return
                if fault in {"missing_schedule", "online_sentinel", "clear_draft"}:
                    with scheduled_mutation_context(check), pytest.raises(ScheduledWriteRejected):
                        gate.register_application(request, bytes(request), scope)
                    return
                with scheduled_mutation_context(check), pytest.raises((UnclassifiedTelegramRpcError, ValueError)):
                    gate.register_application(request, bytes(request), scope)
