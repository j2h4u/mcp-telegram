"""Durable outbound intent, account binding, and explicit recovery boundaries."""

import asyncio
import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import cast

import pytest

from mcp_telegram import telegram_rpc
from mcp_telegram.telegram_rpc import TelegramRpcSafetyLedger, TelegramRpcSafetyStore, TelegramRpcThrottled


@dataclass
class Clock:
    utc: float = 1_000
    monotonic_seconds: float = 100

    def time(self) -> float:
        return self.utc

    def monotonic(self) -> float:
        return self.monotonic_seconds

    def advance(self, seconds: float) -> None:
        self.utc += seconds
        self.monotonic_seconds += seconds


@dataclass
class Store:
    raw: str | None = None
    owners: tuple[int | None, int | None] = (None, None)
    legacy_floor: float | None = None
    fail_next: bool = False
    block_next: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)
    committed: asyncio.Event = field(default_factory=asyncio.Event)
    writes: list[str] = field(default_factory=list)

    async def save(self, expected: str | None, value: str) -> None:
        if self.block_next:
            self.block_next = False
            self.entered.set()
            await self.released.wait()
        if self.fail_next:
            self.fail_next = False
            raise sqlite3.OperationalError("synthetic storage failure")
        if self.raw != expected:
            raise sqlite3.OperationalError("synthetic safety CAS conflict")
        self.raw = value
        self.writes.append(value)
        self.committed.set()

    def api(self) -> TelegramRpcSafetyStore:
        return TelegramRpcSafetyStore(
            load_record=lambda: self.raw,
            save_record=self.save,
            load_legacy_until_utc=lambda: self.legacy_floor,
            canonical_owners=lambda: self.owners,
        )

    def ledger(self, *, dc_id: int = 2, key_id: int | None = 99, boot_id: str = "boot-a") -> TelegramRpcSafetyLedger:
        return TelegramRpcSafetyLedger(self.api(), dc_id=dc_id, key_id=key_id, boot_id=boot_id)

    def block(self) -> None:
        self.block_next = True
        self.entered.clear()
        self.released.clear()
        self.committed.clear()


def record(store: Store) -> dict[str, object]:
    assert store.raw is not None
    return cast(dict[str, object], json.loads(store.raw))


def intents(store: Store) -> dict[str, dict[str, object]]:
    return cast(dict[str, dict[str, object]], record(store)["intents"])


def recovery(store: Store) -> dict[str, object]:
    value = record(store)["recovery"]
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def generation(ledger: TelegramRpcSafetyLedger) -> str:
    value = ledger.status()["generation"]
    assert isinstance(value, str)
    return value


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    value = Clock()
    # Replace the module binding; do not freeze asyncio's global monotonic clock.
    monkeypatch.setattr(telegram_rpc, "time", value)
    return value


async def protected(store: Store) -> TelegramRpcSafetyLedger:
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    await ledger.prepare("original", "users.GetFullUserRequest")
    ledger.mark_claimed("original")
    await ledger.complete("original", known_outcome=False)
    assert ledger.protection_reason is not None
    return ledger


async def consume_trial(store: Store) -> TelegramRpcSafetyLedger:
    ledger = await protected(store)
    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    assert await ledger.consume_recovery()
    return ledger


async def test_protection_diagnostics_are_bounded_safe_and_read_only(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    method = "telethon.tl.functions.messages.GetHistoryRequest:4423e6c5"
    for number in range(10):
        await ledger.prepare(str(number), method)
    ledger.mark_claimed("0")
    await ledger.complete("0", known_outcome=False)
    before = store.raw
    writes = len(store.writes)
    status = store.ledger().status()
    assert status["unresolved_request_count"] == 10
    rows = cast(list[dict[str, object]], status["unresolved_requests"])
    assert len(rows) == 8 and status["unresolved_requests_truncated"] is True
    assert all(set(row) == {"method", "phase"} and row["method"] == method for row in rows)
    assert {row["phase"] for row in rows} == {"prepared", "unknown"}
    assert store.raw == before and len(store.writes) == writes
    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    assert await ledger.consume_recovery()
    assert ledger.status()["unresolved_request_count"] == 0
    assert ledger.status()["unresolved_requests"] == []
    assert ledger.status()["unresolved_requests_truncated"] is False


async def test_protection_diagnostics_never_disclose_unrecognized_method_values(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("private-attempt", "private payload:01234567")
    before = store.raw
    assert ledger.status()["unresolved_requests"] == [{"method": "unrecognized", "phase": "prepared"}]
    assert store.raw == before


@pytest.mark.parametrize("method", ["secret", "private:00000000", "private:zzzzzzzz"])
def test_non_request_constructor_diagnostics_are_redacted(method: str) -> None:
    assert telegram_rpc._safe_diagnostic_method(method) == "unrecognized"


async def test_prewrite_intent_is_durable_before_claim_and_terminal_clears_it(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    with pytest.raises(RuntimeError, match="intent"):
        ledger.mark_claimed("first")
    await ledger.prepare("first", "updates.GetDifferenceRequest")
    assert intents(store)["first"]["phase"] == "prepared"
    ledger.mark_claimed("first")
    await ledger.complete("first", known_outcome=True)
    assert intents(store) == {}
    assert store.ledger().protection_reason is None


async def test_never_written_cancellation_is_not_a_global_stop(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("expired", "users.GetFullUserRequest")
    await ledger.complete("expired", known_outcome=False)
    assert intents(store) == {} and ledger.protection_reason is None
    await ledger.prepare("healthy", "updates.GetDifferenceRequest")
    ledger.mark_claimed("healthy")
    await ledger.complete("healthy", known_outcome=True)


@pytest.mark.parametrize("claimed", [False, True])
async def test_restart_cannot_assume_unsettled_intent_was_never_written(clock: Clock, claimed: bool) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("interrupted", "users.GetFullUserRequest")
    if claimed:
        ledger.mark_claimed("interrupted")
    restarted = store.ledger()
    assert restarted.protection_reason is not None
    with pytest.raises(TelegramRpcThrottled):
        await restarted.prepare("new", "updates.GetDifferenceRequest")
    assert set(intents(store)) == {"interrupted"}


@pytest.mark.parametrize("known_outcome", [False, True])
async def test_old_run_unknown_cannot_be_erased_as_current_unclaimed_work(clock: Clock, known_outcome: bool) -> None:
    store = Store()
    await protected(store)
    before = dict(intents(store)["original"])
    restarted = store.ledger()
    try:
        await restarted.complete("original", known_outcome=known_outcome)
    except ValueError, RuntimeError, TelegramRpcThrottled:
        pass
    assert intents(store)["original"] == before
    assert store.ledger().protection_reason is not None


async def test_duplicate_unknown_completion_cannot_erase_a_written_intent(clock: Clock) -> None:
    store = Store()
    ledger = await protected(store)
    before = dict(intents(store)["original"])
    await ledger.complete("original", known_outcome=False)
    assert intents(store)["original"] == before
    assert ledger.protection_reason is not None and store.ledger().protection_reason is not None


async def test_canceled_prepare_can_commit_but_cannot_mint_an_unrecorded_attempt(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    store.block()
    task = asyncio.create_task(ledger.prepare("canceled", "users.GetFullUserRequest"))
    await store.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.raw is None
    store.released.set()
    await store.committed.wait()
    assert set(intents(store)) == {"canceled"}
    assert store.ledger().protection_reason is not None
    await ledger.complete("canceled", known_outcome=False)
    assert intents(store) == {} and ledger.protection_reason is None


async def test_canceled_terminal_wait_still_persists_the_known_floor(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("flood", "users.GetFullUserRequest")
    ledger.mark_claimed("flood")
    store.block()
    task = asyncio.create_task(ledger.complete("flood", known_outcome=True, floor_until_utc=clock.utc + 600))
    await store.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.released.set()
    await store.committed.wait()
    assert intents(store) == {}
    assert store.ledger().floor_deadline >= clock.monotonic_seconds + 600


async def test_failed_intent_save_and_stale_cas_both_close_admission(clock: Clock) -> None:
    store = Store(fail_next=True)
    failed = store.ledger()
    with pytest.raises(sqlite3.OperationalError):
        await failed.prepare("failed", "users.GetFullUserRequest")
    assert store.raw is None and failed.protection_reason is not None
    with pytest.raises(TelegramRpcThrottled):
        failed.mark_claimed("failed")
    first, stale = store.ledger(), store.ledger()
    await first.prepare("first", "updates.GetDifferenceRequest")
    committed = store.raw
    with pytest.raises(sqlite3.OperationalError, match="CAS"):
        await stale.prepare("stale", "users.GetFullUserRequest")
    assert store.raw == committed and stale.protection_reason is not None


async def test_failed_terminal_persistence_retains_restart_protection(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("sent", "users.GetFullUserRequest")
    ledger.mark_claimed("sent")
    store.fail_next = True
    with pytest.raises(sqlite3.OperationalError):
        await ledger.complete("sent", known_outcome=True, floor_until_utc=clock.utc + 300)
    assert "sent" in intents(store)
    assert ledger.protection_reason is not None and store.ledger().protection_reason is not None


@pytest.mark.parametrize("order", [(50_000.0, 300.0), (300.0, 50_000.0)])
async def test_concurrent_terminal_floors_keep_uncapped_maximum(clock: Clock, order: tuple[float, float]) -> None:
    store = Store()
    ledger = store.ledger()
    for attempt in ("a", "b"):
        await ledger.prepare(attempt, "users.GetFullUserRequest")
        ledger.mark_claimed(attempt)
    await asyncio.gather(
        ledger.complete("a", known_outcome=True, floor_until_utc=clock.utc + order[0]),
        ledger.complete("b", known_outcome=True, floor_until_utc=clock.utc + order[1]),
    )
    assert intents(store) == {}
    assert ledger.floor_deadline >= clock.monotonic_seconds + max(order)
    assert store.ledger().floor_deadline >= clock.monotonic_seconds + max(order)


async def test_same_boot_wall_clock_jump_cannot_shorten_saved_monotonic_floor(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("a", "users.GetFullUserRequest")
    ledger.mark_claimed("a")
    await ledger.complete("a", known_outcome=True, floor_until_utc=clock.utc + 300)
    original = ledger.floor_deadline
    clock.utc += 86_400
    assert store.ledger().floor_deadline >= original


async def test_new_floor_after_proven_expiry_does_not_resurrect_historical_duration_on_reboot(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("old", "users.GetFullUserRequest")
    ledger.mark_claimed("old")
    await ledger.complete("old", known_outcome=True, floor_until_utc=clock.utc + 50_000)
    clock.advance(50_001)
    assert ledger.floor_deadline <= clock.monotonic_seconds
    await ledger.prepare("current", "users.GetFullUserRequest")
    ledger.mark_claimed("current")
    await ledger.complete("current", known_outcome=True, floor_until_utc=clock.utc + 600)
    clock.monotonic_seconds = 2
    restarted = store.ledger(boot_id="boot-b")
    assert restarted.floor_deadline == clock.monotonic_seconds + 600


async def test_legacy_floor_is_not_lost_during_transport_receipt_upgrade(clock: Clock) -> None:
    store = Store(legacy_floor=clock.utc + 50_000)
    ledger = store.ledger()
    assert ledger.floor_deadline >= clock.monotonic_seconds + 50_000
    await ledger.prepare("bootstrap", "help.GetConfigRequest")
    # Persistence of admission is not permission for a physical write.
    assert store.ledger().floor_deadline >= clock.monotonic_seconds + 50_000


async def test_reboot_keeps_unknown_outcome_and_conservative_known_floor(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.prepare("a", "users.GetFullUserRequest")
    ledger.mark_claimed("a")
    await ledger.complete("a", known_outcome=False, floor_until_utc=clock.utc + 600)
    clock.monotonic_seconds = 2
    clock.utc += 30
    restarted = store.ledger(boot_id="boot-b")
    assert restarted.protection_reason is not None
    assert restarted.floor_deadline >= clock.monotonic_seconds + 570


@pytest.mark.parametrize("raw", ["{", "[]", "null", '{"version":999}'])
async def test_corrupt_or_unknown_receipt_never_becomes_fresh_permission(clock: Clock, raw: str) -> None:
    store = Store(raw=raw)
    ledger = store.ledger()
    assert ledger.protection_reason is not None
    with pytest.raises(TelegramRpcThrottled):
        await ledger.prepare("blocked", "help.GetConfigRequest")
    assert store.raw == raw and store.writes == []


@pytest.mark.parametrize("legacy", [-1, True, math.nan, math.inf])
def test_invalid_legacy_floor_fails_closed(clock: Clock, legacy: float) -> None:
    store = Store(legacy_floor=legacy)
    assert store.ledger().protection_reason is not None
    assert store.writes == []


@pytest.mark.parametrize("dc_id,key_id", [(3, 99), (2, 100), (2, None)])
async def test_offline_transport_mismatch_cannot_rebind_existing_account(
    clock: Clock, dc_id: int, key_id: int | None
) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    before = store.raw
    restarted = store.ledger(dc_id=dc_id, key_id=key_id)
    assert restarted.protection_reason is not None and restarted.verified_account_id is None
    with pytest.raises(TelegramRpcThrottled):
        await restarted.prepare("blocked", "help.GetConfigRequest")
    assert store.raw == before


@pytest.mark.parametrize("owners", [(99, 99), (42, 99), (99, None)])
async def test_verified_identity_cannot_overwrite_conflicting_canonical_owners(
    clock: Clock, owners: tuple[int | None, int | None]
) -> None:
    store = Store(owners=owners)
    ledger = store.ledger()
    with pytest.raises(TelegramRpcThrottled):
        await ledger.verify_account(42)
    assert ledger.protection_reason is not None and ledger.verified_account_id is None
    assert store.raw is None


async def test_fresh_key_transition_requires_unowned_state_and_is_durable(clock: Clock) -> None:
    blocked = Store(owners=(42, 42)).ledger(key_id=None)
    assert blocked.protection_reason is not None
    store = Store()
    ledger = store.ledger(key_id=None)
    await ledger.bind_key(2, 99)
    scope = cast(dict[str, object], record(store)["scope"])
    assert scope == {"stage": "transport_only", "dc_id": 2, "key_id": 99, "account_id": None}
    assert store.ledger().protection_reason is None


async def test_account_verification_requires_an_already_bound_transport_key(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger(key_id=None)
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await ledger.verify_account(42)
    assert ledger.verified_account_id is None and store.raw is None


async def test_key_binding_cannot_make_a_protected_fresh_session_restart_as_unprotected(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger(key_id=None)
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await ledger.bind_key(2, 99)
    assert store.raw is None


@pytest.mark.parametrize("owners", [(None, None), (42, None), (None, 42)])
async def test_crash_after_verified_receipt_before_owner_publication_is_recoverable(
    clock: Clock, owners: tuple[int | None, int | None]
) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = owners
    restarted = store.ledger()
    # An absent publication is not evidence of a different authenticated account.
    assert restarted.protection_reason is None
    await restarted.verify_account(42)
    assert restarted.verified_account_id == 42


async def test_canonical_owner_change_while_identity_save_waits_cannot_publish_wrong_binding(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    store.block()
    task = asyncio.create_task(ledger.prepare("serialize", "help.GetConfigRequest"))
    await store.entered.wait()
    bind = asyncio.create_task(ledger.verify_account(42))
    await asyncio.sleep(0)
    store.owners = (99, 99)
    store.released.set()
    await task
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await bind
    assert ledger.verified_account_id is None


async def test_recovery_requires_acknowledgment_and_exact_current_generation(clock: Clock) -> None:
    store = Store()
    ledger = await protected(store)
    with pytest.raises(ValueError):
        await ledger.authorize_recovery(generation(ledger), acknowledged=False)
    with pytest.raises(ValueError, match="generation"):
        await ledger.authorize_recovery("stale", acknowledged=True)
    assert record(store)["recovery"] is None


async def test_only_one_concurrent_recovery_authorization_can_commit(clock: Clock) -> None:
    store = Store()
    ledger = await protected(store)
    results = await asyncio.gather(
        ledger.authorize_recovery(generation(ledger), acknowledged=True),
        ledger.authorize_recovery(generation(ledger), acknowledged=True),
        return_exceptions=True,
    )
    assert sum(isinstance(value, str) for value in results) == 1
    assert sum(isinstance(value, ValueError) for value in results) == 1
    assert recovery(store)["state"] == "authorized"


async def test_known_floor_prevents_recovery_without_cap_or_reset(clock: Clock) -> None:
    store = Store()
    ledger = await protected(store)
    await ledger.complete("original", known_outcome=False, floor_until_utc=clock.utc + 50_000)
    before = store.raw
    with pytest.raises(ValueError, match="restriction"):
        await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    assert store.raw == before
    assert ledger.floor_deadline >= clock.monotonic_seconds + 50_000


async def test_queued_recovery_authorization_rechecks_a_newly_committed_floor(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    for attempt in ("original", "late_flood"):
        await ledger.prepare(attempt, "users.GetFullUserRequest")
        ledger.mark_claimed(attempt)
    await ledger.complete("original", known_outcome=False)
    store.block()
    floor = asyncio.create_task(ledger.complete("late_flood", known_outcome=True, floor_until_utc=clock.utc + 600))
    await store.entered.wait()
    authorize = asyncio.create_task(ledger.authorize_recovery(generation(ledger), acknowledged=True))
    await asyncio.sleep(0)
    store.released.set()
    await floor
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await authorize
    assert record(store)["recovery"] is None
    assert ledger.floor_deadline >= clock.monotonic_seconds + 600


@pytest.mark.parametrize("mismatch", ["dc", "key", "canonical_owner"])
async def test_interrupted_recovery_cannot_hide_account_isolation_mismatch(clock: Clock, mismatch: str) -> None:
    store = Store()
    await consume_trial(store)
    if mismatch == "canonical_owner":
        store.owners = (99, 99)
    restarted = store.ledger(dc_id=3 if mismatch == "dc" else 2, key_id=100 if mismatch == "key" else 99)
    assert restarted.verified_account_id is None
    assert restarted.protection_reason in {"safety_auth_scope_mismatch", "safety_canonical_owner_mismatch"}
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await restarted.authorize_recovery(generation(restarted), acknowledged=True)
    assert recovery(store)["state"] == "consumed"


async def test_shutdown_drain_keeps_cancelled_callers_mutations_owned_until_storage_settles(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    store.block()
    first = asyncio.create_task(ledger.prepare("first", "users.GetFullUserRequest"))
    await store.entered.wait()
    second = asyncio.create_task(ledger.prepare("second", "users.GetFullUserRequest"))
    await asyncio.sleep(0)
    first.cancel()
    second.cancel()
    await asyncio.gather(first, second, return_exceptions=True)
    draining = asyncio.create_task(ledger.drain())
    await asyncio.sleep(0)
    draining.cancel()
    await asyncio.sleep(0)
    assert not draining.done()
    assert store.raw is None
    store.released.set()
    with pytest.raises(asyncio.CancelledError):
        await draining
    assert set(intents(store)) == {"first", "second"}
    assert not ledger._operations


async def test_recovery_grant_cannot_acknowledge_new_uncertainty_after_authorization(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    for attempt in ("original", "late_unknown"):
        await ledger.prepare(attempt, "users.GetFullUserRequest")
        ledger.mark_claimed(attempt)
    await ledger.complete("original", known_outcome=False)
    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    await ledger.complete("late_unknown", known_outcome=False)
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await ledger.consume_recovery()
    assert intents(store)["late_unknown"]["phase"] == "unknown"
    assert recovery(store)["state"] != "consumed"
    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    assert await ledger.consume_recovery()
    assert intents(store)["late_unknown"]["phase"] == "acknowledged_unknown"


async def test_recovery_is_consumed_durably_before_new_attempts_and_crash_cannot_remint(clock: Clock) -> None:
    store = Store()
    ledger = await consume_trial(store)
    assert recovery(store)["state"] == "consumed"
    assert intents(store)["original"]["phase"] == "acknowledged_unknown"
    restarted = store.ledger()
    assert restarted.protection_reason is not None
    assert not await restarted.consume_recovery()
    with pytest.raises(TelegramRpcThrottled):
        await restarted.prepare("new", "updates.GetDifferenceRequest")


async def test_recovery_success_retains_unknown_history_and_requires_same_account(clock: Clock) -> None:
    store = Store()
    ledger = await consume_trial(store)
    old = dict(intents(store)["original"])
    with pytest.raises(ValueError):
        await ledger.finish_recovery(99)
    await ledger.prepare("trial", "updates.GetDifferenceRequest")
    ledger.mark_claimed("trial")
    await ledger.complete("trial", known_outcome=True)
    await ledger.verify_account(42)
    await ledger.finish_recovery(42)
    assert recovery(store)["state"] == "succeeded"
    assert intents(store)["original"] == old
    assert ledger.protection_reason is None and store.ledger().protection_reason is None


async def test_late_uncertain_result_cannot_be_cleared_by_recovery_success(clock: Clock) -> None:
    store = Store()
    ledger = await consume_trial(store)
    await ledger.prepare("trial", "updates.GetDifferenceRequest")
    ledger.mark_claimed("trial")
    await ledger.complete("trial", known_outcome=False)
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await ledger.finish_recovery(42)
    assert intents(store)["trial"]["phase"] == "unknown"
    assert ledger.protection_reason is not None and store.ledger().protection_reason is not None


async def test_recovery_success_cannot_override_a_new_known_floor(clock: Clock) -> None:
    store = Store()
    ledger = await consume_trial(store)
    await ledger.prepare("trial", "updates.GetDifferenceRequest")
    ledger.mark_claimed("trial")
    await ledger.complete("trial", known_outcome=True, floor_until_utc=clock.utc + 600)
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await ledger.finish_recovery(42)
    assert recovery(store)["state"] != "succeeded"
    assert ledger.floor_deadline >= clock.monotonic_seconds + 600


async def test_known_storm_hold_survives_settlement_restart_and_requires_explicit_recovery(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    await ledger.prepare("storm", "users.GetFullUserRequest")
    ledger.mark_claimed("storm")
    floor = clock.utc + 600
    await ledger.complete("storm", known_outcome=True, floor_until_utc=floor, circuit_open=True)
    assert intents(store) == {}
    restarted = store.ledger()
    assert restarted.protection_reason == "flood_wait_circuit_open"
    with pytest.raises(ValueError, match="restriction"):
        await restarted.authorize_recovery(generation(restarted), acknowledged=True)
    clock.advance(601)
    assert restarted.protection_reason == "flood_wait_circuit_open"
    await restarted.authorize_recovery(generation(restarted), acknowledged=True)
    assert recovery(store)["acknowledged_protection"] == "flood_wait_circuit_open"
    assert await restarted.consume_recovery()
    await restarted.prepare("startup", "users.GetUsersRequest")
    restarted.mark_claimed("startup")
    await restarted.complete("startup", known_outcome=True)
    await restarted.verify_account(42)
    await restarted.finish_recovery(42)
    assert store.ledger().protection_reason is None
    assert cast(dict[str, object], record(store)["floor"])["until_utc"] == floor


async def test_failed_trial_requires_a_new_explicit_authorization_but_is_not_a_permanent_dead_end(clock: Clock) -> None:
    store = Store()
    ledger = await consume_trial(store)
    await ledger.fail_recovery()
    original_grant = recovery(store)["generation"]
    old_unknown = dict(intents(store)["original"])
    restarted = store.ledger()
    assert restarted.protection_reason is not None
    assert not await restarted.consume_recovery()
    with pytest.raises(TelegramRpcThrottled):
        await restarted.prepare("automatic", "updates.GetDifferenceRequest")
    grant = await restarted.authorize_recovery(generation(restarted), acknowledged=True)
    assert grant != original_grant
    assert intents(store)["original"] == old_unknown
    assert await restarted.consume_recovery()


async def test_queued_recovery_consumption_cannot_cross_a_new_known_floor(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    for attempt in ("original", "late_flood"):
        await ledger.prepare(attempt, "users.GetFullUserRequest")
        ledger.mark_claimed(attempt)
    await ledger.complete("original", known_outcome=False)
    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    store.block()
    floor = asyncio.create_task(ledger.complete("late_flood", known_outcome=True, floor_until_utc=clock.utc + 600))
    await store.entered.wait()
    consume = asyncio.create_task(ledger.consume_recovery())
    await asyncio.sleep(0)
    store.released.set()
    await floor
    with pytest.raises((ValueError, RuntimeError, TelegramRpcThrottled)):
        await consume
    assert recovery(store)["state"] != "consumed"
    assert ledger.protection_reason is not None
    assert ledger.floor_deadline >= clock.monotonic_seconds + 600
