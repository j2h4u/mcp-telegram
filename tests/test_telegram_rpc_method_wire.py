"""Method adaptation crosses the real gate, SQLite receipt and vendor writer."""

import asyncio
import json
import time
from dataclasses import replace
from typing import cast

import pytest
from telethon.errors import FloodWaitError
from telethon.tl import functions, types

from mcp_telegram import telegram_rpc
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptBudgetExhaustedError, RpcAttemptEvidence
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc_consumers import TELEGRAM_RPC_CONSUMERS, DemandKind, demand_contract
from mcp_telegram.telegram_rpc_scheduler import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcScope,
    TelegramRpcSource,
)
from tests.test_telegram_rpc_safety import Clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


def _clock(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock(time.time(), time.monotonic())
    monkeypatch.setattr(telegram_rpc, "time", value)
    monkeypatch.setattr(wire[0]._admission_scheduler, "_clock", value.monotonic)
    return value


def _scope(clock: Clock, source: TelegramRpcSource = TelegramRpcSource.MCP_INTERACTIVE) -> TelegramRpcScope:
    return TelegramRpcScope(
        source,
        TELEGRAM_RPC_CONSUMERS[source].admission.service_class,
        clock.monotonic() + 4,
        asyncio.current_task(),
        attempt_budget=RpcAttemptBudget(2),
        attempt_evidence=RpcAttemptEvidence(),
    )


async def _flood(wire: _WireHarness, clock: Clock) -> str:
    _, sender, connection, _ = wire
    request = functions.users.GetFullUserRequest(types.InputUserSelf())
    result = sender.send_owned(request, _scope(clock))
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_exception(FloodWaitError(request, capture=7))
    with pytest.raises(FloodWaitError):
        await result
    return telegram_rpc._canonical_wire_method(request)


def _durable_claim(gate: telegram_rpc.TelegramRpcGate, method: str) -> dict[str, object]:
    encoded = gate._safety_store.load_record()
    assert encoded is not None
    record = cast(dict[str, object], json.loads(encoded))
    episode = cast(dict[str, dict[str, object]], record["methods"])[method]
    return cast(dict[str, object], episode["claim"])


async def test_method_hold_crosses_peers_and_sources_but_not_eligible_gap_method(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    notifications: list[str | None] = []
    gate.set_rpc_eligibility_observer(notifications.append)
    clock = _clock(wire, monkeypatch)
    method = await _flood(wire, clock)
    assert None in notifications and method in notifications
    hint = gate.rpc_eligibility(method)
    assert isinstance(hint, TelegramRpcAdmissionDeferred) and hint.reason == "server"
    clock.advance(8)
    hint = gate.rpc_eligibility(method)
    assert isinstance(hint, TelegramRpcAdmissionDeferred) and hint.reason == "method"
    for source in (TelegramRpcSource.MCP_INTERACTIVE, TelegramRpcSource.ENTITY_INFO_FOREGROUND):
        request = functions.users.GetFullUserRequest(types.InputUser(7, 0))
        result = sender.send_owned(request, _scope(clock, source))
        state = await sender._send_queue.next_state()
        with pytest.raises(TelegramRpcAdmissionDeferred) as error:
            await sender._prepare_state(state)
        assert error.value.method_key == method and error.value.reason == "method"
        with pytest.raises(TelegramRpcAdmissionDeferred):
            await result
    request = functions.updates.GetDifferenceRequest(1, None, 0)
    scope = _scope(clock, TelegramRpcSource.TELETHON_UPDATE_DIFFERENCE)
    result = sender.send_owned(request, scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result("difference")
    assert await result == "difference"
    assert len(sink.frames) == 2
    assert method in cast(dict[str, object], gate._ensure_safety_ledger()._record["methods"])


async def test_five_real_contenders_get_one_durable_trial_and_current_success_clears_it(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    method = await _flood(wire, clock)
    clock.advance(301)
    original = gate._admission_scheduler.admit
    all_admitted = asyncio.Event()
    count = 0

    async def simultaneous_admission(scope: TelegramRpcScope) -> telegram_rpc.RpcAdmission:
        nonlocal count
        admission = await original(scope)
        count += 1
        if count == 5:
            all_admitted.set()
        await all_admitted.wait()
        return admission

    monkeypatch.setattr(gate._admission_scheduler, "admit", simultaneous_admission)
    results = [
        sender.send_owned(functions.users.GetFullUserRequest(types.InputUser(peer, 0)), _scope(clock))
        for peer in range(1, 6)
    ]
    states = [await sender._send_queue.next_state() for _ in results]
    preparations = await asyncio.wait_for(
        asyncio.gather(*(sender._prepare_state(state) for state in states), return_exceptions=True), 3
    )
    assert sum(isinstance(value, TelegramRpcMethodRecoveryPending) for value in preparations) == 4
    claim = _durable_claim(gate, method)
    assert isinstance(gate.rpc_eligibility(method), TelegramRpcMethodRecoveryPending)
    packet = connection._send_queue.get_nowait()
    assert claim["attempt_id"] == gate._require_wire_owner(packet.state.owner).attempt_id
    assert len(sink.frames) == 1
    assert sender.write_packet(packet, connection._send)
    packet.state.state.future.set_result("recovered")
    outcomes = await asyncio.gather(*results, return_exceptions=True)
    assert outcomes.count("recovered") == 1
    assert sum(isinstance(value, TelegramRpcMethodRecoveryPending) for value in outcomes) == 4
    assert (
        cast(dict[str, dict[str, object]], gate._ensure_safety_ledger()._record["methods"])[method]["state"]
        == "recovered"
    )
    assert gate.rpc_eligibility(method) is None
    assert len(sink.frames) == 2
    assert not any(gate._admission_scheduler.active_depths().values())


async def test_late_success_from_before_the_flood_cannot_clear_new_method_episode(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, _ = wire
    clock = _clock(wire, monkeypatch)
    request = functions.users.GetFullUserRequest(types.InputUser(1, 0))
    late = sender.send_owned(request, _scope(clock))
    old = await sender._send_queue.next_state()
    await sender._prepare_state(old)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    clock.advance(3)
    method = await _flood(wire, clock)
    episodes = cast(dict[str, object], gate._ensure_safety_ledger()._record["methods"])
    before = json.dumps(episodes[method], sort_keys=True)
    old.future.set_result("old success")
    assert await late == "old success"
    assert json.dumps(episodes[method], sort_keys=True) == before


async def test_new_flood_generation_revokes_a_durable_trial_even_after_both_holds_expire(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    request = functions.users.GetFullUserRequest(types.InputUserSelf())
    late = sender.send_owned(request, _scope(clock))
    old = await sender._send_queue.next_state()
    await sender._prepare_state(old)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    clock.advance(3)
    await _flood(wire, clock)
    clock.advance(301)
    scope = replace(_scope(clock, TelegramRpcSource.ENTITY_INFO_REFRESH), deadline=clock.monotonic() + 900)
    result = sender.send_owned(request, scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    old.future.set_exception(FloodWaitError(request, capture=9))
    with pytest.raises(FloodWaitError):
        await late
    clock.advance(601)
    assert gate._wire_release_at(packet.state.owner.scope) <= clock.monotonic()
    assert not sender.write_packet(packet, connection._send)
    assert len(sink.frames) == 2 and scope.attempt_budget is not None and scope.attempt_budget.attempts == 0
    state.future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result


def _selected_budget(target: str = "7") -> RpcAttemptBudget:
    kind = DemandKind.ENTITY_PROFILE_REFRESH
    limit = demand_contract(kind).max_rpc_attempts_per_slice
    assert limit is not None
    return RpcAttemptBudget(limit, attempts=limit, selected=WorkRevision(WorkKey(kind, 42, target), "page:0"))


async def test_credit_is_shared_by_trial_continuation_and_duplicate_budgets(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    method = await _flood(wire, clock)
    first = _selected_budget()
    duplicate = _selected_budget()
    original_limit = first.limit
    assert not gate.prepare_durable_retry(first, method)
    assert not gate.prepare_durable_retry(duplicate, method)
    assert first._retry_candidate is duplicate._retry_candidate
    clock.advance(301)
    assert gate.prepare_durable_retry(first, method)
    request = functions.users.GetFullUserRequest(types.InputUser(7, 0))
    for index in range(original_limit):
        budget = first if index == 0 else duplicate
        scope = replace(_scope(clock), attempt_budget=budget)
        result = sender.send_owned(request, scope)
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        state.future.set_result("ok")
        assert await result == "ok"
        assert budget.attempts == original_limit + index + 1
        assert budget.limit == original_limit * 2
        clock.advance(3)
    assert len(sink.frames) == original_limit + 1
    assert not gate.prepare_durable_retry(first, method)
    assert not gate.prepare_durable_retry(_selected_budget("8"), method)
    result = sender.send_owned(request, replace(_scope(clock), attempt_budget=first))
    state = await sender._send_queue.next_state()
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await sender._prepare_state(state)
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await result


@pytest.mark.parametrize("refusal", ["revoked", "expired", "generation", "missing_intent", "storage"])
async def test_denied_final_claim_never_spends_credit_or_grows_budget(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    method = await _flood(wire, clock)
    budget = _selected_budget()
    gate.prepare_durable_retry(budget, method)
    clock.advance(301)
    request = functions.users.GetFullUserRequest(types.InputUser(7, 0))
    result = sender.send_owned(request, replace(_scope(clock), attempt_budget=budget))
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    packet = connection._send_queue.get_nowait()
    owner = gate._require_wire_owner(packet.state.owner)
    ledger = gate._ensure_safety_ledger()
    initial = budget.limit, budget.attempts
    if refusal == "revoked":
        owner.revoked = True
    elif refusal == "expired":
        clock.advance(5)
    elif refusal == "generation":
        gate._wire_generation += 1
    elif refusal == "missing_intent":
        cast(dict[str, object], ledger._record["intents"]).pop(owner.attempt_id)
    else:
        ledger._storage_failed = True
    assert not sender.write_packet(packet, connection._send)
    assert (budget.limit, budget.attempts) == initial
    assert len(sink.frames) == 1
    credit = gate._retry_credit(budget)
    assert credit is not None and not credit.granted
    ledger._storage_failed = False
    if refusal == "missing_intent":
        with pytest.raises(RuntimeError, match="durable intent"):
            await result
        return
    state.future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await result


async def _send_result(
    wire: _WireHarness, clock: Clock, budget: RpcAttemptBudget, request: object, *, flood: bool = False
) -> None:
    _, sender, connection, _ = wire
    result = sender.send_owned(request, replace(_scope(clock), attempt_budget=budget))
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    if flood:
        state.future.set_exception(FloodWaitError(request, capture=7))
        with pytest.raises(FloodWaitError):
            await result
    else:
        state.future.set_result("ok")
        assert await result == "ok"


async def test_alternating_method_bindings_share_all_six_physical_debits(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, sender, _, sink = wire
    clock = _clock(wire, monkeypatch)
    selected = WorkRevision(WorkKey(DemandKind.HOT_ACTIVITY_PAGE, 42, "7"), "page:0")
    first = RpcAttemptBudget(2, selected=selected)
    requests = (functions.users.GetFullUserRequest(types.InputUser(7, 0)), functions.updates.GetStateRequest())
    methods = tuple(telegram_rpc._canonical_wire_method(request) for request in requests)
    await _send_result(wire, clock, first, requests[0], flood=True)
    gate.prepare_durable_retry(first, methods[0])
    clock.advance(301)
    await _send_result(wire, clock, first, requests[1], flood=True)
    duplicate = RpcAttemptBudget(2, attempts=2, selected=selected)
    gate.prepare_durable_retry(duplicate, methods[1])
    clock.advance(301)
    for budget, index in ((first, 0), (duplicate, 1), (duplicate, 1), (first, 0)):
        assert gate.prepare_durable_retry(budget, methods[index])
        await _send_result(wire, clock, budget, requests[index])
        clock.advance(3)
    assert len(sink.frames) == 6
    assert (first.limit, first.attempts) == (6, 6)
    assert (duplicate.limit, duplicate.attempts) == (6, 6)
    assert not gate.prepare_durable_retry(first, methods[0])
    result = sender.send_owned(requests[0], replace(_scope(clock), attempt_budget=first))
    state = await sender._send_queue.next_state()
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await sender._prepare_state(state)
    with pytest.raises(RpcAttemptBudgetExhaustedError):
        await result
    assert len(sink.frames) == 6


@pytest.mark.parametrize("live_episode", [False, True])
async def test_authorized_authentication_preserves_protection_but_can_prepare_method_permission(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, live_episode: bool
) -> None:
    from mcp_telegram.flood import TelegramRpcThrottled
    from mcp_telegram.telegram_demand import demand_context

    gate, sender, connection, sink = wire
    clock = _clock(wire, monkeypatch)
    ledger = gate._ensure_safety_ledger()
    request = functions.help.GetConfigRequest()
    method = telegram_rpc._canonical_wire_method(request)
    if live_episode:
        await ledger.prepare("earlier-flood", method)
        ledger.mark_claimed("earlier-flood")
        await ledger.complete("earlier-flood", known_outcome=True, actual_flood=True)
        clock.advance(301)
    await ledger.prepare("unknown", method)
    ledger.mark_claimed("unknown")
    await ledger.complete("unknown", known_outcome=False)
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication(acknowledge_unknown_outcome=True)
        with pytest.raises(TelegramRpcThrottled):
            ledger.require_writable()
        result = sender.send_owned(request, _scope(clock))
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        state.future.set_result("authorized")
        assert await result == "authorized"
    assert len(sink.frames) == 1 and ledger.protection_reason == "unresolved_outbound_outcome"
