"""Read-budget receipt boundaries use the real safety ledger."""

import asyncio
import json
from typing import cast

import pytest
from telethon import functions, types

from mcp_telegram import telegram_rpc
from tests.test_telegram_rpc_safety import Clock, Store, record
from tests.test_telegram_rpc_safety import clock as clock
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


def _method() -> str:
    return telegram_rpc._canonical_wire_method(
        functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0)
    )


async def _failed_read(ledger: telegram_rpc.TelegramRpcSafetyLedger, attempt: str, method: str) -> None:
    await ledger.prepare(attempt, method)
    generation = await ledger.prepare_method_trial(attempt, method)
    ledger.mark_claimed(attempt)
    await ledger.complete(attempt, known_outcome=False, method_generation=generation, scoped_read_failure=True)


async def test_read_failures_grow_and_survive_restart(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    for index, delay in enumerate((300, 600, 1200, 2400, 3600, 3600), 1):
        await _failed_read(ledger, f"read-{index}", method)
        current = record(store)
        episode = cast(dict[str, dict[str, object]], current["methods"])[method]
        assert episode["consecutive_read_budget_failures"] == index
        assert episode["consecutive_actual_floods"] == 0
        assert cast(dict[str, float], episode["floor"])["until_utc"] == clock.utc + delay
        assert current["protection"] is None
        receipt = cast(dict[str, dict[str, object]], current["intents"])[f"read-{index}"]
        assert receipt["phase"] == "unknown" and receipt["read_retry"] is not None
        ledger = store.ledger()
        assert ledger.protection_reason is None
        clock.advance(delay)


async def test_matching_trial_success_clears_counters_but_unrelated_does_not(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    await _failed_read(ledger, "read", method)
    other = telegram_rpc._canonical_wire_method(functions.users.GetFullUserRequest(types.InputUserSelf()))
    await ledger.prepare("other", other)
    ledger.mark_claimed("other")
    await ledger.complete("other", known_outcome=True, method_success=True)
    assert cast(dict[str, dict[str, object]], record(store)["methods"])[method]["consecutive_read_budget_failures"] == 1
    clock.advance(300)
    await ledger.prepare("trial", method)
    generation = await ledger.prepare_method_trial("trial", method)
    ledger.mark_claimed("trial")
    await ledger.complete("trial", known_outcome=True, method_success=True, method_generation=generation)
    episode = cast(dict[str, dict[str, object]], record(store)["methods"])[method]
    assert episode["state"] == "recovered" and episode["consecutive_read_budget_failures"] == 0


async def test_old_unknown_is_not_reclassified(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    await ledger.prepare("old", _method())
    ledger.mark_claimed("old")
    await ledger.complete("old", known_outcome=False)
    old = record(store)
    old["version"] = 3
    store.raw = json.dumps(old)
    reloaded = store.ledger()
    assert reloaded.protection_reason == "unresolved_outbound_outcome"
    assert "read_retry" not in cast(dict[str, dict[str, object]], reloaded._record["intents"])["old"]


async def test_flood_floor_and_read_counter_remain_independent(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    await ledger.prepare("flood", method)
    ledger.mark_claimed("flood")
    await ledger.complete("flood", known_outcome=True, actual_flood=True)
    clock.advance(3600)
    await _failed_read(ledger, "read", method)
    episode = cast(dict[str, dict[str, object]], record(store)["methods"])[method]
    assert episode["consecutive_actual_floods"] == 1
    assert episode["consecutive_read_budget_failures"] == 1
    assert cast(dict[str, float], episode["floor"])["until_utc"] == clock.utc + 300


async def test_failed_read_terminal_save_preserves_storage_hold(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    await ledger.prepare("read", method)
    ledger.mark_claimed("read")
    store.fail_next = True
    with pytest.raises(Exception, match="synthetic storage failure"):
        await ledger.complete("read", known_outcome=False, scoped_read_failure=True)
    assert ledger.protection_reason is not None


async def test_cancelled_terminal_waits_for_owned_commit(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    await ledger.prepare("read", method)
    ledger.mark_claimed("read")
    store.block()
    pending = asyncio.create_task(ledger.complete("read", known_outcome=False, scoped_read_failure=True))
    await store.entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    store.released.set()
    await ledger.drain()
    assert cast(dict[str, dict[str, object]], record(store)["intents"])["read"]["read_retry"] is not None


async def test_concurrent_claimed_reads_settle_independently(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    for attempt in ("first", "second"):
        await ledger.prepare(attempt, method)
        ledger.mark_claimed(attempt)
    store.block()
    first = asyncio.create_task(ledger.complete("first", known_outcome=False, scoped_read_failure=True))
    await store.entered.wait()
    second = asyncio.create_task(ledger.complete("second", known_outcome=False, scoped_read_failure=True))
    await asyncio.sleep(0)
    assert not first.done() and not second.done()
    assert ledger.protection_reason is None
    store.released.set()
    await asyncio.gather(first, second)
    current = record(store)
    episode = cast(dict[str, dict[str, object]], current["methods"])[method]
    assert episode["consecutive_read_budget_failures"] == 2
    assert cast(dict[str, float], episode["floor"])["until_utc"] == clock.utc + 600
    assert all(value["phase"] == "unknown" for value in cast(dict[str, dict[str, object]], current["intents"]).values())


async def test_scoped_read_does_not_clear_independent_unknown(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    await ledger.prepare("read", method)
    await ledger.prepare("independent", method)
    ledger.mark_claimed("read")
    ledger.mark_claimed("independent")
    await ledger.complete("independent", known_outcome=False)
    await ledger.complete("read", known_outcome=False, scoped_read_failure=True)
    assert ledger.protection_reason == "unresolved_outbound_outcome"
    assert cast(dict[str, dict[str, object]], record(store)["intents"])["independent"]["phase"] == "unknown"


async def test_physical_pending_receipts_are_counted_until_each_commit(  # noqa: PLR0914, PLR0915 - physical lifecycle
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from mcp_telegram.telegram_demand import AcquisitionKind, RpcAttemptBudget, RpcAttemptBudgetExhaustedError
    from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
    from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
    from tests.test_telegram_rpc_method_wire import _clock, _scope

    gate, sender, connection, sink = wire
    now = _clock(wire, monkeypatch)
    ledger = gate._ensure_safety_ledger()
    states = []
    results = []
    for index in range(2):
        selected = WorkRevision(WorkKey(DemandKind.DELTA_GAP_FILL, 42, f"peer:{index}"), "revision")
        scope = replace(
            _scope(now, TelegramRpcSource.DELTA_SYNC),
            demand_kind=DemandKind.DELTA_GAP_FILL,
            acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
            attempt_budget=RpcAttemptBudget(1, selected=selected),
        )
        result = sender.send_owned(
            functions.messages.GetHistoryRequest(types.InputPeerSelf(), 0, None, 0, 1, 0, 0, 0), scope
        )
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
        states.append(state)
        results.append(result)
        now.advance(2)
    entered = [asyncio.Event(), asyncio.Event()]
    release = [asyncio.Event(), asyncio.Event()]
    original = ledger._store.save_record
    calls = 0

    async def blocked(expected: str | None, value: str) -> None:
        nonlocal calls
        index = calls
        calls += 1
        entered[index].set()
        await release[index].wait()
        await original(expected, value)

    ledger._store = replace(ledger._store, save_record=blocked)
    first_owner = gate._require_wire_owner(sender.require_owned(states[0]).owner)
    for state in states:
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await sender._prepare_state(state)
    method = _method()
    await entered[0].wait()
    assert gate._pending_method_receipts[method] == 2
    assert gate.account_safety_status() is None and gate.rpc_eligibility(method) is not None
    release[0].set()
    await entered[1].wait()
    assert first_owner.receipt is not None
    await first_owner.receipt
    await asyncio.sleep(0)
    assert gate._pending_method_receipts[method] == 1
    assert gate.account_safety_status() is None and gate.rpc_eligibility(method) is not None
    release[1].set()
    for result in results:
        with pytest.raises(RpcAttemptBudgetExhaustedError):
            await result
    await asyncio.gather(*sender._observing)
    assert method not in gate._pending_method_receipts and len(sink.frames) == 2


async def test_old_same_method_success_does_not_clear_new_failure(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    await ledger.prepare("old-success", method)
    old_generation = await ledger.prepare_method_trial("old-success", method)
    ledger.mark_claimed("old-success")
    await _failed_read(ledger, "new-failure", method)
    await ledger.complete("old-success", known_outcome=True, method_success=True, method_generation=old_generation)
    episode = cast(dict[str, dict[str, object]], record(store)["methods"])[method]
    assert episode["consecutive_read_budget_failures"] == 1
    assert cast(dict[str, float], episode["floor"])["until_utc"] == clock.utc + 300


@pytest.mark.parametrize(
    "corruption", ["negative_flood", "missing_counter", "old_version_with_counter", "old_version_with_retry"]
)
async def test_receipt_version_and_failure_counts_are_strict(clock: Clock, corruption: str) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    await _failed_read(ledger, "read", _method())
    receipt = record(store)
    episode = cast(dict[str, dict[str, object]], receipt["methods"])[_method()]
    if corruption == "negative_flood":
        episode["consecutive_actual_floods"] = -1
    elif corruption == "missing_counter":
        episode.pop("consecutive_read_budget_failures")
    elif corruption == "old_version_with_counter":
        receipt["version"] = 3
        cast(dict[str, dict[str, object]], receipt["intents"])["read"].pop("read_retry")
    else:
        receipt["version"] = 3
        episode.pop("consecutive_read_budget_failures")
    store.raw = json.dumps(receipt)
    assert store.ledger().protection_reason == "safety_storage_failure"


@pytest.mark.parametrize("mixed", [False, True])
async def test_method_diagnostics_report_read_and_mixed_causes(clock: Clock, mixed: bool) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = _method()
    if mixed:
        await ledger.prepare("flood", method)
        ledger.mark_claimed("flood")
        await ledger.complete("flood", known_outcome=True, actual_flood=True)
        clock.advance(3600)
    await _failed_read(ledger, "read", method)
    deferred = ledger.method_deferral(method)
    assert deferred is not None
    assert deferred.reason == ("method_mixed_retry" if mixed else "read_budget_retry")
    assert "FloodWait" in str(deferred) if mixed else "local read failure" in str(deferred)


@pytest.mark.parametrize("kind", ["draft", "scheduled", "folder", "note", "bootstrap", "authentication", "self_lookup"])
async def test_nonordinary_owner_is_not_scoped_read_retry(
    wire: _WireHarness, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    from dataclasses import replace

    from mcp_telegram.telegram_demand import AcquisitionKind
    from mcp_telegram.telegram_rpc_consumers import DemandKind, TelegramRpcSource
    from tests.test_telegram_rpc_method_wire import _clock, _scope

    gate, _, _, _ = wire
    now = _clock(wire, monkeypatch)
    scope = replace(
        _scope(now, TelegramRpcSource.DELTA_SYNC),
        acquisition_kind=AcquisitionKind.MESSAGE_HISTORY_PAGE,
        demand_kind=DemandKind.DELTA_GAP_FILL,
    )
    changes = {
        "draft": {"demand_kind": DemandKind.DRAFT_MUTATION},
        "scheduled": {"demand_kind": DemandKind.SCHEDULED_MUTATION},
        "folder": {"demand_kind": DemandKind.FOLDER_COMMAND},
        "note": {"demand_kind": DemandKind.NOTE_COMMAND},
        "bootstrap": {"transport_bootstrap": True},
        "authentication": {"transport_auth": True},
        "self_lookup": {},
    }
    owner = telegram_rpc._WireAttempt(object(), "attempt", _method(), replace(scope, **changes[kind]))
    owner.self_lookup = kind == "self_lookup"
    assert not gate._ordinary_read_owner(owner)
