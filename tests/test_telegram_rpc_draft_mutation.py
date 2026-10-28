"""Explicit native draft writes cannot escape the root's single-send allowance."""

import asyncio
import time
from dataclasses import replace
from typing import cast

import pytest
from telethon.errors import ServerError
from telethon.tl import functions, types
from telethon.tl.tlobject import TLRequest

from mcp_telegram import telegram_rpc
from mcp_telegram.telegram_demand import (
    AcquisitionKind,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    demand_context,
)
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
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


def _save() -> functions.messages.SaveDraftRequest:
    return functions.messages.SaveDraftRequest(types.InputPeerSelf(), "draft")


async def _prepare(
    wire: _WireHarness, request: object, scope: TelegramRpcScope
) -> tuple[asyncio.Future[object], _OwnedPacket]:
    _, sender, connection, _ = wire
    response = sender.send_owned(request, scope)
    state = await sender._send_queue.next_state()
    await sender._prepare_state(state)
    return response, connection._send_queue.get_nowait()


async def _complete(wire: _WireHarness, request: object) -> None:
    _, sender, connection, _ = wire
    response, packet = await _prepare(wire, request, current_rpc_scope())
    assert sender.write_packet(packet, connection._send)
    packet.state.state.future.set_result(True)
    assert await response is True


async def test_snapshot_then_save_preserve_one_root_and_separate_single_attempt_budgets(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _clock(wire, monkeypatch)
    with demand_context(DemandKind.DRAFT_MUTATION) as root:
        with rpc_attempt_budget(RpcAttemptBudget(1)) as preflight:
            with rpc_scope(TelegramRpcSource.DRAFT_SNAPSHOT, acquisition_kind=AcquisitionKind.DRAFT_SNAPSHOT):
                scope = current_rpc_scope()
                assert scope.source is TelegramRpcSource.DRAFT_MUTATION
                assert scope.demand_kind is DemandKind.DRAFT_MUTATION
                assert scope.attempt_evidence is root.attempt_evidence
                await _complete(wire, functions.messages.GetAllDraftsRequest())
        clock.advance(3)
        with rpc_attempt_budget(RpcAttemptBudget(1)) as mutation:
            await _complete(wire, _save())
        assert preflight.attempts == mutation.attempts == 1
        assert root.attempt_evidence.actual_attempts == 2
        assert root.attempt_evidence.draft_preflight_dispatched
        assert root.attempt_evidence.draft_mutation_dispatched
    assert len(wire[3].frames) == 2


@pytest.mark.parametrize("mutation", [False, True])
async def test_new_budget_cannot_repeat_same_root_preflight_or_mutation(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, mutation: bool
) -> None:
    clock = _clock(wire, monkeypatch)
    _, sender, connection, sink = wire
    request = _save() if mutation else functions.messages.GetAllDraftsRequest()
    with demand_context(DemandKind.DRAFT_MUTATION):
        with rpc_scope(
            TelegramRpcSource.DRAFT_MUTATION,
            acquisition_kind=None if mutation else AcquisitionKind.DRAFT_SNAPSHOT,
        ):
            with rpc_attempt_budget(RpcAttemptBudget(1)):
                await _complete(wire, request)
            clock.advance(3)
            with rpc_attempt_budget(RpcAttemptBudget(1)) as fresh:
                response, packet = await _prepare(wire, request, current_rpc_scope())
                assert not sender.write_packet(packet, connection._send)
                assert fresh.attempts == 0
                response.cancel()
    assert len(sink.frames) == 1


async def test_physical_save_replay_cannot_acquire_a_second_attempt(wire: _WireHarness) -> None:
    _, sender, connection, sink = wire
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)) as budget:
        response, packet = await _prepare(wire, _save(), current_rpc_scope())
        assert sender.write_packet(packet, connection._send)
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await sender._prepare_state(packet.state.state)
        assert budget.attempts == 1 and len(sink.frames) == 1
        response.cancel()


async def test_successful_save_is_not_acquisition_evidence(wire: _WireHarness) -> None:
    gate = wire[0]
    gate._auto_resume_stable_since = time.monotonic()
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
        await _complete(wire, _save())
    assert not gate._auto_resume_acquired


async def test_unknown_save_outcome_remains_protected_without_retry(wire: _WireHarness) -> None:
    gate, sender, connection, sink = wire
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)) as budget:
        response, packet = await _prepare(wire, _save(), current_rpc_scope())
        assert sender.write_packet(packet, connection._send)
        packet.state.state.future.set_exception(ConnectionError("fixture disconnected"))
        with pytest.raises(ConnectionError):
            await response
        assert budget.attempts == 1 and len(sink.frames) == 1
    status = gate.account_safety_status()
    assert status is not None and status["unresolved_request_count"] == 1


@pytest.mark.parametrize("fault", ["missing", "two", "selected", "retry", "root", "source", "acquisition"])
async def test_save_rejects_missing_or_incorrect_classification_and_budget(wire: _WireHarness, fault: str) -> None:
    gate = wire[0]
    budget = RpcAttemptBudget(1)
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(budget):
        scope = current_rpc_scope()
    if fault == "missing":
        scope = replace(scope, attempt_budget=None)
    elif fault == "two":
        budget.limit = 2
    elif fault == "selected":
        budget.selected = WorkRevision(WorkKey(DemandKind.DRAFT_MUTATION, 42, "draft"), "revision")
    elif fault == "retry":
        budget._retry_candidate = object()
    elif fault == "root":
        scope = replace(scope, demand_kind=DemandKind.MCP_REMOTE_ACQUISITION)
    elif fault == "source":
        scope = replace(scope, source=TelegramRpcSource.MCP_INTERACTIVE)
    else:
        scope = replace(scope, acquisition_kind=AcquisitionKind.DRAFT_SNAPSHOT)
    request = _save()
    with pytest.raises((UnclassifiedTelegramRpcError, ValueError)):
        gate.register_application(request, bytes(request), scope)
    assert wire[3].frames == []


async def test_budget_change_after_preparation_cannot_enable_more_mutation_writes(wire: _WireHarness) -> None:
    _, sender, connection, sink = wire
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)) as budget:
        response, packet = await _prepare(wire, _save(), current_rpc_scope())
        budget.limit = 2
        assert not sender.write_packet(packet, connection._send)
        assert sink.frames == []
        response.cancel()


@pytest.mark.parametrize(
    "rpc_request",
    [functions.messages.GetPeerDialogsRequest([]), functions.messages.SendMessageRequest(types.InputPeerSelf(), "x")],
)
async def test_mutation_root_rejects_unrequested_rpc_methods(wire: _WireHarness, rpc_request: TLRequest) -> None:
    gate = wire[0]
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
        with acquisition_context(AcquisitionKind.DRAFT_SNAPSHOT):
            with pytest.raises(UnclassifiedTelegramRpcError, match="only GetAllDrafts"):
                gate.register_application(rpc_request, bytes(rpc_request), current_rpc_scope())
    assert wire[3].frames == []


@pytest.mark.parametrize("failure", ["transient", "migration"])
async def test_save_does_not_enter_logical_retry_loop(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    gate = wire[0]
    gate._transient_retry_delays = (0, 0)
    error = (
        ServerError(None, "fixture") if failure == "transient" else telegram_rpc._ConnectionDcMigrationRequestedError(3)
    )
    calls = 0

    async def fail(*_args: object, **_kwargs: object) -> object:
        nonlocal calls
        calls += 1
        raise error

    monkeypatch.setattr(gate, "_call_with_source_policy", fail)
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
        with pytest.raises(type(error)):
            await gate(_save())
    assert calls == 1 and wire[3].frames == []


async def test_save_without_root_is_rejected_before_dispatch(wire: _WireHarness) -> None:
    with pytest.raises(UnclassifiedTelegramRpcError):
        await wire[0](_save())
    assert wire[3].frames == []


async def test_draft_mutation_does_not_receive_method_retry_credit(wire: _WireHarness) -> None:
    gate = wire[0]
    selected = WorkRevision(WorkKey(DemandKind.DRAFT_MUTATION, 42, "draft"), "revision")
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1, selected=selected)):
        with pytest.raises(ValueError, match="without retry credit"):
            await gate(_save())
    assert cast(object, gate._ensure_safety_ledger()._record["intents"]) == {}


async def test_retained_draft_peer_uses_only_session_cache(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = wire[0]
    assert gate.session is not None
    peer = types.InputPeerUser(42, 123)
    seen: list[int] = []

    def cached(dialog_id: int) -> object:
        seen.append(dialog_id)
        if dialog_id == 42:
            return peer
        raise ValueError("not retained")

    monkeypatch.setattr(gate.session, "get_input_entity", cached)
    assert gate.retained_draft_peer(42) is peer
    assert gate.retained_draft_peer(43) is None
    assert seen == [42, 43]
    assert wire[3].frames == []


@pytest.mark.parametrize("value", [True, False, 0, "42"])
async def test_retained_draft_peer_rejects_invalid_identifiers(wire: _WireHarness, value: object) -> None:
    with pytest.raises(ValueError, match="nonzero integer"):
        wire[0].retained_draft_peer(cast(int, value))
