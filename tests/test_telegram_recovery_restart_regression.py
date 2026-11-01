"""Recovery remains live after a successful trial and a later uncertain read."""

import json
from typing import cast

import pytest

from mcp_telegram.telegram_rpc import TelegramRpcSafetyLedger, _validate_safety_record
from mcp_telegram.telegram_rpc_recovery import RecoveryCause
from tests.test_telegram_rpc_safety import Clock, Store, intents, recovery
from tests.test_telegram_rpc_safety import clock as clock

_HISTORY = "telethon.tl.functions.messages.GetHistoryRequest:4423e6c5"
_MUTATION = "telethon.tl.functions.messages.SaveDraftRequest:ad0fa15c"


async def _successful_trial(store: Store, clock: Clock) -> TelegramRpcSafetyLedger:
    ledger = store.ledger()
    await ledger.verify_account(42)
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.TRANSIENT)
    clock.advance(31)
    assert await ledger.consume_auto_resume()
    await ledger.finish_recovery(42)
    return ledger


async def _uncertain(ledger: TelegramRpcSafetyLedger, key: str, method: str) -> None:
    await ledger.prepare(key, method)
    ledger.mark_claimed(key)
    await ledger.complete(key, known_outcome=False)


@pytest.mark.parametrize("reload_after_failure", [False, True])
async def test_successful_old_trial_does_not_strand_new_unknown_read(clock: Clock, reload_after_failure: bool) -> None:
    store = Store(owners=(42, 42))
    await _successful_trial(store, clock)
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    await _uncertain(ledger, "current-read", _HISTORY)
    if reload_after_failure:
        ledger = store.ledger()
        ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    automatic = cast(dict[str, object], recovery(store)["automatic"])
    assert recovery(store)["state"] == "waiting"
    assert automatic["scheduled_due"] is not None
    clock.advance(300)
    assert await ledger.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    assert await ledger.consume_auto_resume()
    assert intents(store)["current-read"]["phase"] == "unknown"


async def test_restart_after_success_without_new_blocker_does_not_rearm(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    await _successful_trial(store, clock)
    before = store.raw
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    clock.advance(100_000)
    assert not await ledger.maintain_auto_resume()
    assert not await ledger.consume_auto_resume()
    assert store.raw == before


async def test_successful_old_trial_does_not_authorize_new_unknown_mutation(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    await _successful_trial(store, clock)
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    await _uncertain(ledger, "current-mutation", _MUTATION)
    clock.advance(100_000)
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    assert not await ledger.consume_auto_resume()
    assert intents(store)["current-mutation"]["phase"] == "unknown"


async def test_disposed_historical_read_is_not_current_blocker(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    await _uncertain(ledger, "historical", "telethon.tl.functions.users.GetUsersRequest:0d91a548")
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume()
    clock.advance(300)
    assert await ledger.consume_auto_resume()
    await ledger.finish_recovery(42)
    historical = dict(intents(store)["historical"])
    current = store.ledger()
    current.configure_auto_resume(enabled=True)
    await _uncertain(current, "current-read", _HISTORY)
    assert not await current.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    assert recovery(store)["state"] == "waiting"
    clock.advance(300)
    assert await current.consume_auto_resume()
    assert intents(store)["historical"] == historical
    assert intents(store)["current-read"]["phase"] == "unknown"


@pytest.mark.parametrize("fault", ["owner", "scope", "corrupt"])
async def test_successful_old_trial_does_not_recover_unsafe_state(clock: Clock, fault: str) -> None:
    store = Store(owners=(42, 42))
    await _successful_trial(store, clock)
    if fault == "owner":
        store.owners = (43, 43)
    elif fault == "corrupt":
        store.raw = "{}"
    ledger = store.ledger(key_id=100 if fault == "scope" else 99)
    ledger.configure_auto_resume(enabled=True)
    clock.advance(100_000)
    assert not await ledger.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    assert not await ledger.consume_auto_resume()
    assert ledger.protection_reason is not None


async def test_manual_recovery_acknowledges_only_current_blocker(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    await _uncertain(ledger, "historical", "telethon.tl.functions.users.GetUsersRequest:0d91a548")
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume()
    clock.advance(300)
    assert await ledger.consume_auto_resume()
    await ledger.finish_recovery(42)
    historical = dict(intents(store)["historical"])
    assert historical["phase"] == "unknown" and historical.get("policy_recovery") is not None
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    await _uncertain(ledger, "current-read", _HISTORY)
    assert store.raw is not None and _validate_safety_record(cast(object, json.loads(store.raw)))
    assert store.ledger().protection_reason == "unresolved_outbound_outcome"
    from tests.test_telegram_rpc_safety import generation

    await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    assert await ledger.consume_recovery()
    assert intents(store)["historical"] == historical
    assert intents(store)["current-read"]["phase"] == "acknowledged_unknown"
    assert store.raw is not None and _validate_safety_record(cast(object, json.loads(store.raw)))
    restarted = store.ledger()
    assert restarted.protection_reason != "corrupt_safety_record"
    assert restarted.status()["generation"] is not None


async def test_imported_stuck_successful_receipt_rearms_current_read(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    await _successful_trial(store, clock)
    previous_trial = dict(recovery(store))
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    await _uncertain(ledger, "current-read", _HISTORY)
    assert store.raw is not None
    receipt = cast(dict[str, object], json.loads(store.raw))
    receipt["recovery"] = previous_trial
    store.raw = json.dumps(receipt)
    imported = store.ledger()
    imported.configure_auto_resume(enabled=True)
    assert imported.protection_reason == "unresolved_outbound_outcome"
    assert not await imported.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    assert recovery(store)["state"] == "waiting"
    clock.advance(300)
    assert await imported.maintain_auto_resume(cause=RecoveryCause.READ_UNCERTAINTY)
    assert await imported.consume_auto_resume()
    assert intents(store)["current-read"]["phase"] == "unknown"
