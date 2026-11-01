from dataclasses import FrozenInstanceError, replace
from typing import cast

import pytest

from mcp_telegram.telegram_rpc_recovery import RecoveryCause
from tests.test_telegram_rpc_safety import Clock, Store, intents, recovery
from tests.test_telegram_rpc_safety import clock as clock


async def test_recovery_identity_is_immutable_and_does_not_read_storage(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)

    def forbidden() -> tuple[int | None, int | None]:
        raise AssertionError("identity inspection must not access SQLite")

    ledger._store = replace(ledger._store, canonical_owners=forbidden)
    identity = ledger.recovery_identity()
    attribute = "enabled"
    with pytest.raises(FrozenInstanceError):
        setattr(identity, attribute, True)
    assert not identity.enabled


async def test_unknown_exact_read_recovery_binds_history_without_acknowledgment(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    await ledger.prepare("lost", "telethon.tl.functions.users.GetUsersRequest:0d91a548")
    ledger.mark_claimed("lost")
    await ledger.complete("lost", known_outcome=False)
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume()
    clock.advance(299)
    assert not await ledger.consume_auto_resume()
    clock.advance(1)
    assert await ledger.consume_auto_resume()
    item = intents(store)["lost"]
    assert item["phase"] == "unknown"
    disposition = cast(dict[str, object], item["policy_recovery"])
    assert disposition["scope"] == item["scope"]
    assert cast(dict[str, object], recovery(store)["automatic"])["bound_intent_ids"] == ["lost"]


async def test_unknown_mutation_holds_even_after_time(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    await ledger.prepare("lost", "telethon.tl.functions.messages.SaveDraftRequest:ad0fa15c")
    ledger.mark_claimed("lost")
    await ledger.complete("lost", known_outcome=False)
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    clock.advance(100_000)
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.TRANSIENT)
    assert not await ledger.consume_auto_resume()
    assert intents(store)["lost"]["phase"] == "unknown"


async def test_transient_zero_intent_outage_continues_after_six_trials(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.TRANSIENT)
    for attempt in range(1, 9):
        clock.advance(1800)
        assert await ledger.consume_auto_resume()
        automatic = cast(dict[str, object], recovery(store)["automatic"])
        assert automatic["attempts_total"] == attempt
        assert cast(dict[str, int], automatic["attempts_by_policy"])["transient"] == attempt
        ledger = store.ledger()
        ledger.configure_auto_resume(enabled=True)
        assert not await ledger.maintain_auto_resume(cause=RecoveryCause.TRANSIENT)


async def test_interrupted_consumed_trial_keeps_spent_flood_budget(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    ledger.configure_auto_resume(enabled=True)
    await ledger.record_floor(clock.time() + 1, circuit_open=True)
    assert not await ledger.maintain_auto_resume()
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume()
    automatic = cast(dict[str, object], recovery(store)["automatic"])
    assert automatic["attempts_total"] == 1
    assert cast(dict[str, int], automatic["attempts_by_policy"])["flood_wait"] == 1
    clock.advance(7199)
    assert not await ledger.consume_auto_resume()
    clock.advance(1)
    assert await ledger.consume_auto_resume()


async def test_unclassified_terminal_does_not_strengthen_existing_flood_wait(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    await ledger.prepare("lost", "telethon.tl.functions.users.GetUsersRequest:0d91a548")
    ledger.mark_claimed("lost")
    await ledger.complete("lost", known_outcome=False)
    ledger.configure_auto_resume(enabled=True)
    await ledger.record_floor(clock.time() + 3600, circuit_open=True)
    await ledger.maintain_auto_resume()
    before = cast(dict[str, object], recovery(store)["automatic"])
    due = dict(cast(dict[str, object], before["scheduled_due"]))
    counts = dict(cast(dict[str, int], before["attempts_by_policy"]))
    clock.advance(10)
    await ledger.complete("lost", known_outcome=False, floor_until_utc=clock.time())
    after = cast(dict[str, object], recovery(store)["automatic"])
    assert after["scheduled_due"] == due and after["attempts_by_policy"] == counts


async def test_single_method_flood_without_account_circuit_does_not_arm_recovery(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    ledger.configure_auto_resume(enabled=True)
    method = "telethon.tl.functions.users.GetUsersRequest:0d91a548"
    await ledger.prepare("flood", method)
    ledger.mark_claimed("flood")
    await ledger.complete("flood", known_outcome=True, floor_until_utc=clock.time() + 30, actual_flood=True)
    assert ledger._record["recovery"] is None and ledger.protection_reason is None
    assert ledger.floor_deadline >= clock.monotonic() + 30
    assert method in cast(dict[str, object], ledger._record["methods"])
