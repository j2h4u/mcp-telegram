"""Source policy preserves definite server refusal without guessing unknown outcomes."""

import asyncio

import pytest
from telethon.errors import FloodPremiumWaitError, FloodTestPhoneWaitError, FloodWaitError

from mcp_telegram import telegram_rpc
from mcp_telegram.flood import TelegramRpcThrottled
from mcp_telegram.telegram_demand import RpcAttemptBudget, demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import TelegramRpcScope, rpc_attempt_budget
from tests.test_telegram_rpc_draft_mutation import _prepare, _save
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


def _server_response(wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    gate, sender, connection, _ = wire

    async def dispatch(request: object, *, ordered: bool, scope: TelegramRpcScope) -> object:
        del ordered
        response, packet = await _prepare(wire, request, scope)
        assert sender.write_packet(packet, connection._send)
        packet.state.state.future.set_exception(error)
        return await response

    monkeypatch.setattr(gate, "_dispatch_attempt", dispatch)


async def _throttled_save(wire: _WireHarness) -> TelegramRpcThrottled:
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)) as budget:
        with pytest.raises(TelegramRpcThrottled) as caught:
            await wire[0](_save())
        assert budget.attempts <= 1
    return caught.value


@pytest.mark.parametrize("error_type", [FloodWaitError, FloodPremiumWaitError, FloodTestPhoneWaitError])
async def test_actual_flood_refusal_crosses_source_policy_with_durable_known_outcome(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, error_type: type[FloodWaitError]
) -> None:
    _server_response(wire, monkeypatch, error_type(_save(), capture=20))
    error = await _throttled_save(wire)
    ledger = wire[0]._ensure_safety_ledger()
    assert error.request_rejected
    assert error.retry_after_seconds == 20 and not error.latched
    assert ledger.status()["unresolved_request_count"] == 0
    assert ledger.floor_deadline > telegram_rpc.time.monotonic()
    assert len(wire[3].frames) == 1


async def test_latched_throttle_during_actual_flood_recording_retains_definite_refusal(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = wire[0]
    _server_response(wire, monkeypatch, FloodWaitError(_save(), capture=20))
    observe = gate._observe_flood
    latched = TelegramRpcThrottled(latched=True, detail="fixture account latch")

    async def observe_then_latch(exc: BaseException, *, scope: TelegramRpcScope | None = None) -> int:
        await observe(exc, scope=scope)
        raise latched

    monkeypatch.setattr(gate, "_observe_flood", observe_then_latch)
    error = await _throttled_save(wire)
    assert error is latched and error.request_rejected
    assert error.latched and error.retry_after_seconds is None
    assert gate._ensure_safety_ledger().status()["unresolved_request_count"] == 0


@pytest.mark.parametrize("origin", ["vendor_cache", "never_dispatched", "unknown"])
async def test_non_server_flood_deferral_does_not_claim_request_rejected(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    error = FloodWaitError(_save(), capture=20)
    telegram_rpc._annotate_flood_attempt(
        error,
        request_method=telegram_rpc._canonical_wire_method(_save()),
        admission_sequence=None,
        dispatch_at_monotonic=None,
        dispatch_kind=origin,
    )

    async def defer(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(wire[0], "_dispatch_attempt", defer)
    throttled = await _throttled_save(wire)
    assert not throttled.request_rejected
    assert wire[3].frames == []


async def test_generic_account_protection_remains_an_unclassified_outcome(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    uncertain = TelegramRpcThrottled(latched=True, detail="fixture unresolved outbound outcome")

    async def protect(*_args: object, **_kwargs: object) -> object:
        raise uncertain

    monkeypatch.setattr(wire[0], "_dispatch_attempt", protect)
    assert await _throttled_save(wire) is uncertain
    assert not uncertain.request_rejected


async def test_unknown_physical_outcome_stays_unknown_in_central_ledger(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server_response(wire, monkeypatch, ConnectionError("fixture unknown result"))
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
        with pytest.raises(ConnectionError):
            await wire[0](_save())
    assert wire[0]._ensure_safety_ledger().status()["unresolved_request_count"] == 1
    assert len(wire[3].frames) == 1


async def test_flood_recording_cancellation_is_not_reclassified(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _server_response(wire, monkeypatch, FloodWaitError(_save(), capture=20))

    async def cancelled(*_args: object, **_kwargs: object) -> int:
        raise asyncio.CancelledError

    monkeypatch.setattr(wire[0], "_observe_flood", cancelled)
    with demand_context(DemandKind.DRAFT_MUTATION), rpc_attempt_budget(RpcAttemptBudget(1)):
        with pytest.raises(asyncio.CancelledError):
            await wire[0](_save())
    assert wire[0]._ensure_safety_ledger().status()["unresolved_request_count"] == 0
