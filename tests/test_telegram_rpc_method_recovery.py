"""Durable bounded retry holds for repeated FloodWaits on one RPC method."""

import asyncio
import json
import sqlite3
from typing import cast

import pytest
from telethon import functions, types

from mcp_telegram import telegram_rpc
from mcp_telegram.telegram_demand import RpcAttemptBudget
from mcp_telegram.telegram_demand_progress import WorkKey, WorkRevision
from mcp_telegram.telegram_rpc import (
    TelegramRpcMethodRecoveryPending,
)
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.test_telegram_rpc_safety import Clock, Store
from tests.test_telegram_rpc_safety import clock as clock
from tests.test_telegram_rpc_safety import intents as safety_intents
from tests.test_telegram_rpc_safety import record as safety_record


@pytest.fixture(name="method_clock")
def method_clock_fixture(clock: Clock) -> Clock:
    return clock


def method_key() -> str:
    return telegram_rpc._canonical_wire_method(functions.users.GetFullUserRequest(id=types.InputUserSelf()))


async def record_flood(store: Store, attempt: str, method: str, *, global_floor: float | None = None) -> str:
    ledger = store.ledger()
    await ledger.prepare(attempt, method)
    generation = await ledger.prepare_method_trial(attempt, method)
    ledger.mark_claimed(attempt)
    await ledger.complete(
        attempt,
        known_outcome=True,
        floor_until_utc=global_floor,
        actual_flood=True,
        method_generation=generation,
    )
    recorded_generation = method_episode(store, method)["generation"]
    assert isinstance(recorded_generation, str)
    return recorded_generation


def method_episode(store: Store, method: str) -> dict[str, object]:
    methods = cast(dict[str, dict[str, object]], safety_record(store)["methods"])
    return methods[method]


def method_floor(store: Store, method: str) -> dict[str, object]:
    floor = method_episode(store, method)["floor"]
    assert isinstance(floor, dict)
    return cast(dict[str, object], floor)


def method_claim(store: Store, method: str) -> dict[str, object] | None:
    claim = method_episode(store, method)["claim"]
    assert claim is None or isinstance(claim, dict)
    return cast(dict[str, object] | None, claim)


def _credit_budget(target: str = "7") -> RpcAttemptBudget:
    selected = WorkRevision(WorkKey(DemandKind.HOT_ACTIVITY_PAGE, 42, target), "cursor:1")
    return RpcAttemptBudget(2, attempts=2, selected=selected)


async def _live_credit(store: Store) -> tuple[telegram_rpc.TelegramRpcSafetyLedger, str]:
    ledger = store.ledger()
    await ledger.verify_account(42)
    method = method_key()
    await ledger.prepare("flood", method)
    ledger.mark_claimed("flood")
    await ledger.complete("flood", known_outcome=True, actual_flood=True)
    return ledger, method


async def test_credit_consumption_survives_concurrent_receipt_copy_and_failure(method_clock: Clock) -> None:
    store = Store()
    ledger, method = await _live_credit(store)
    budget = _credit_budget()
    credit = ledger.bind_method_retry(budget, method, 2)
    assert credit is not None
    store.block()
    operation = asyncio.create_task(ledger.record_floor(method_clock.utc + 1))
    await store.entered.wait()
    credit.debit(budget)
    store.released.set()
    await operation
    duplicate = ledger.bind_method_retry(_credit_budget(), method, 2)
    assert duplicate is credit and credit.granted and credit.attempts == 3
    store.fail_next = True
    with pytest.raises(sqlite3.OperationalError):
        await ledger.record_floor(method_clock.utc + 2)
    assert credit.attempts == 3 and credit.limit == 4
    assert ledger.protection_reason == "safety_storage_failure"


@pytest.mark.parametrize("success", [False, True])
async def test_restart_never_remints_same_live_or_recovered_generation_credit(
    method_clock: Clock, success: bool
) -> None:
    store = Store()
    ledger, method = await _live_credit(store)
    budget = _credit_budget()
    credit = ledger.bind_method_retry(budget, method, 2)
    assert credit is not None
    method_clock.advance(301)
    await ledger.prepare("trial", method)
    generation = await ledger.prepare_method_trial("trial", method)
    credit.debit(budget)
    ledger.mark_claimed("trial")
    await ledger.complete("trial", known_outcome=True, method_generation=generation, method_success=success)
    restarted = store.ledger()
    assert restarted.protection_reason is None
    assert restarted.bind_method_retry(_credit_budget(), method, 2) is None
    assert restarted.method_deferral(method) is None
    await restarted.prepare("ordinary-base", method)
    ordinary_generation = await restarted.prepare_method_trial("ordinary-base", method)
    assert ordinary_generation == (None if success else generation)


async def test_binding_capacity_refuses_only_new_extra_credit_and_never_evicts_spent(method_clock: Clock) -> None:
    del method_clock
    ledger, method = await _live_credit(Store())
    budget = _credit_budget()
    credit = ledger.bind_method_retry(budget, method, 1)
    assert credit is not None
    credit.debit(budget)
    credit.debit(budget)
    assert not credit.available
    assert ledger.bind_method_retry(_credit_budget("8"), method, 1) is None
    assert ledger.bind_method_retry(_credit_budget(), method, 1) is credit


@pytest.mark.parametrize("global_first", [False, True])
async def test_method_flood_delay_doubles_to_hour_cap_without_capping_global_floor(
    method_clock: Clock, global_first: bool
) -> None:
    store = Store()
    method = method_key()
    global_floor = method_clock.utc + 50_000
    delays = [300, 600, 1200, 2400, 3600, 3600]

    for index, delay in enumerate(delays):
        prior_count = index + 1
        eligible_at = store.ledger().method_release_at(method)
        if index:
            assert eligible_at > method_clock.monotonic_seconds
            method_clock.advance(eligible_at - method_clock.monotonic_seconds)
        current_global = global_floor if (global_first or index == len(delays) - 1) else None
        await record_flood(store, f"flood-{index}", method, global_floor=current_global)
        episode = method_episode(store, method)
        assert episode["consecutive_actual_floods"] == prior_count
        assert method_floor(store, method)["remaining_seconds"] == delay

    assert store.ledger().floor_deadline >= 100 + 50_000
    assert store.ledger().method_release_at(method) - method_clock.monotonic_seconds == 3600


async def test_duplicate_terminal_flood_after_intent_removal_does_not_escalate(method_clock: Clock) -> None:
    store = Store()
    method = method_key()
    ledger = store.ledger()
    await ledger.prepare("once", method)
    generation = await ledger.prepare_method_trial("once", method)
    ledger.mark_claimed("once")
    await ledger.complete("once", known_outcome=True, actual_flood=True, method_generation=generation)
    before = dict(method_episode(store, method))

    await ledger.complete("once", known_outcome=True, actual_flood=True, method_generation=generation)

    assert method_episode(store, method) == before
    assert method_episode(store, method)["consecutive_actual_floods"] == 1


async def test_only_one_of_five_contenders_commits_the_durable_method_claim(method_clock: Clock) -> None:
    store = Store()
    method = method_key()
    await record_flood(store, "seed", method)
    method_clock.advance(300)
    ledger = store.ledger()
    for index in range(5):
        await ledger.prepare(f"candidate-{index}", method)

    results = await asyncio.gather(
        *(ledger.prepare_method_trial(f"candidate-{index}", method) for index in range(5)),
        return_exceptions=True,
    )

    granted = [value for value in results if isinstance(value, str)]
    refused = [value for value in results if isinstance(value, TelegramRpcMethodRecoveryPending)]
    assert len(granted) == 1 and len(refused) == 4
    claim = method_claim(store, method)
    assert claim is not None
    assert claim["attempt_id"] in {f"candidate-{index}" for index in range(5)}
    assert claim["run"] == ledger._run
    assert store.ledger().method_release_at(method) == float("inf")


async def test_never_written_trial_releases_claim_without_erasing_method_history(method_clock: Clock) -> None:
    store = Store()
    method = method_key()
    await record_flood(store, "seed", method)
    method_clock.advance(300)
    ledger = store.ledger()
    await ledger.prepare("cancelled", method)
    generation = await ledger.prepare_method_trial("cancelled", method)
    before = dict(method_episode(store, method))

    await ledger.complete("cancelled", known_outcome=False, method_generation=generation)

    after = method_episode(store, method)
    assert after["consecutive_actual_floods"] == before["consecutive_actual_floods"] == 1
    assert after["generation"] == before["generation"]
    assert method_floor(store, method) == cast(dict[str, object], before["floor"])
    assert method_claim(store, method) is None
    assert store.ledger().protection_reason is None


async def test_only_matching_current_trial_success_clears_method_episode(method_clock: Clock) -> None:
    store = Store()
    method = method_key()
    await record_flood(store, "seed", method)
    method_clock.advance(300)
    ledger = store.ledger()
    await ledger.prepare("unrelated", method)
    await ledger.prepare("current", method)
    current_generation = await ledger.prepare_method_trial("current", method)

    await ledger.complete("unrelated", known_outcome=True, method_success=True, method_generation="stale")
    assert method in cast(dict[str, object], safety_record(store)["methods"])
    current_claim = method_claim(store, method)
    assert current_claim is not None and current_claim["attempt_id"] == "current"

    ledger.mark_claimed("current")
    await ledger.complete("current", known_outcome=True, method_success=True, method_generation=current_generation)
    recovered = method_episode(store, method)
    assert recovered["state"] == "recovered" and recovered["consecutive_actual_floods"] == 0
    assert ledger.method_deferral(method) is None


async def test_restart_preserves_method_hold_and_unsettled_trial_is_unknown_protected(method_clock: Clock) -> None:
    store = Store()
    method = method_key()
    await record_flood(store, "seed", method)
    method_clock.advance(100)
    restarted = store.ledger(boot_id="boot-b")
    assert restarted.method_release_at(method) >= method_clock.monotonic_seconds + 300
    assert restarted.protection_reason is None

    method_clock.advance(301)
    await restarted.prepare("trial", method)
    await restarted.prepare_method_trial("trial", method)
    restarted.mark_claimed("trial")
    recovered = store.ledger(boot_id="boot-c")
    assert recovered.protection_reason == "unresolved_outbound_outcome"
    assert recovered.method_release_at(method) == float("inf")
    assert "trial" in safety_intents(store)


async def test_method_reservation_save_failure_closes_admission(method_clock: Clock) -> None:
    store = Store()
    method = method_key()
    await record_flood(store, "seed", method)
    method_clock.advance(300)
    ledger = store.ledger()
    await ledger.prepare("trial", method)
    store.fail_next = True

    with pytest.raises(sqlite3.OperationalError, match="storage failure"):
        await ledger.prepare_method_trial("trial", method)

    assert ledger.protection_reason == "safety_storage_failure"
    assert ledger.method_release_at(method) == float("inf")
    assert method_claim(store, method) is None
    with pytest.raises(telegram_rpc.TelegramRpcThrottled):
        await ledger.prepare_method_trial("trial", method)


async def test_version_one_receipt_migration_preserves_floor_binding_and_unknown_intent(method_clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    await ledger.record_floor(method_clock.utc + 50_000)
    await ledger.prepare("old", method_key())
    ledger.mark_claimed("old")
    await ledger.complete("old", known_outcome=False)
    legacy = safety_record(store)
    legacy.pop("methods")
    legacy["version"] = 1
    store.raw = json.dumps(legacy, sort_keys=True)
    migrated = store.ledger()
    assert migrated.protection_reason == "unresolved_outbound_outcome"
    assert migrated.verified_account_id == 42
    assert migrated._record["methods"] == {}
    await migrated.record_floor(method_clock.utc + 50_000)
    persisted = safety_record(store)
    assert persisted.pop("version") == 2 and persisted.pop("methods") == {}
    legacy.pop("version")
    assert persisted == legacy
