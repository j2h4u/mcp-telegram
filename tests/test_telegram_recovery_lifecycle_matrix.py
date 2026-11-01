"""Serialized automatic recovery survives new incidents and restart boundaries."""

import asyncio
import json
import sqlite3
from typing import cast

import pytest

from mcp_telegram.telegram_rpc import TelegramRpcSafetyLedger, TelegramRpcThrottled
from tests.test_daemon_api import _close_tracked_sqlite_connections as _close_tracked_sqlite_connections
from tests.test_telegram_auto_resume import _waiting
from tests.test_telegram_rpc_safety import Clock, Store, consume_trial, generation, intents, protected, record, recovery
from tests.test_telegram_rpc_safety import clock as clock

READ = "telethon.tl.functions.users.GetUsersRequest:0d91a548"
HISTORY = "telethon.tl.functions.messages.GetHistoryRequest:4423e6c5"
MUTATION = "telethon.tl.functions.messages.SaveDraftRequest:ad0fa15c"


async def _success(store: Store, clock: Clock) -> TelegramRpcSafetyLedger:
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    await ledger.finish_recovery(42)
    assert recovery(store)["state"] == "succeeded"
    return ledger


@pytest.mark.parametrize("restart", [False, True])
async def test_new_uncertain_read_after_success_rearms_and_retains_episode(clock: Clock, restart: bool) -> None:
    store = Store()
    ledger = await _success(store, clock)
    before = cast(dict[str, object], recovery(store)["automatic"])
    episode, total = before["episode_id"], before["attempts_total"]
    if restart:
        ledger = store.ledger()
        ledger.configure_auto_resume(enabled=True)
    await ledger.prepare("new-read", HISTORY)
    ledger.mark_claimed("new-read")
    await ledger.complete("new-read", known_outcome=False)
    assert intents(store)["new-read"]["phase"] == "unknown"
    assert recovery(store)["state"] == "fencing"
    assert not await ledger.maintain_auto_resume(quiesced=False)
    assert not await ledger.maintain_auto_resume(quiesced=True)
    assert recovery(store)["state"] == "waiting"
    automatic = cast(dict[str, object], recovery(store)["automatic"])
    assert automatic["episode_id"] == episode and automatic["attempts_total"] == total
    saved = store.raw
    for _ in range(3):
        assert not await ledger.maintain_auto_resume(quiesced=True)
    assert store.raw == saved
    assert intents(store)["new-read"]["phase"] == "unknown"


async def test_healthy_success_restart_does_not_create_recovery_or_write(clock: Clock) -> None:
    store = Store()
    await _success(store, clock)
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    saved = store.raw
    assert not await restarted.maintain_auto_resume()
    assert store.raw == saved and recovery(store)["state"] == "succeeded"


async def test_plain_floor_without_episode_does_not_create_automatic_trial(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    ledger.configure_auto_resume(enabled=True)
    await ledger.record_floor(clock.time() + 600, circuit_open=False)
    assert record(store)["recovery"] is None
    assert ledger.floor_deadline >= clock.monotonic() + 600
    assert not await ledger.consume_auto_resume()


@pytest.mark.parametrize("phase", ["authorized", "consumed"])
async def test_new_plain_floor_invalidates_active_manual_trial(clock: Clock, phase: str) -> None:
    store = Store()
    if phase == "authorized":
        ledger = await protected(store)
        await ledger.authorize_recovery(generation(ledger), acknowledged=True)
    else:
        ledger = await consume_trial(store)
    assert recovery(store)["state"] == phase
    grant = recovery(store)["generation"]
    await ledger.record_floor(clock.time() + 600, circuit_open=False)
    assert ledger.floor_deadline >= clock.monotonic() + 600
    if phase == "authorized":
        assert record(store)["generation"] != grant
        with pytest.raises(TelegramRpcThrottled):
            await ledger.consume_recovery()
    else:
        assert recovery(store)["state"] == "failed"
        with pytest.raises(ValueError):
            await ledger.finish_recovery(42)
    clock.advance(601)
    with pytest.raises(ValueError):
        if phase == "authorized":
            await ledger.consume_recovery()
        else:
            await ledger.finish_recovery(42)
    assert recovery(store)["state"] != "succeeded"


@pytest.mark.parametrize("claimed", [False, True])
async def test_current_prepared_owner_is_not_a_new_failure(clock: Clock, claimed: bool) -> None:
    store = Store()
    ledger = await _success(store, clock)
    await ledger.prepare("pending", HISTORY)
    if claimed:
        ledger.mark_claimed("pending")
    before = store.raw
    status = ledger.auto_resume_status()
    assert status["state"] != "reconciling"
    assert not await ledger.maintain_auto_resume(quiesced=True)
    assert store.raw == before and recovery(store)["state"] == "succeeded"
    assert not await ledger.consume_auto_resume()
    assert intents(store)["pending"]["phase"] == "prepared"


async def test_previous_run_prepared_read_reconciles_as_uncertain(clock: Clock) -> None:
    store = Store()
    ledger = await _success(store, clock)
    await ledger.prepare("interrupted", HISTORY)
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    before = store.raw
    assert restarted.auto_resume_status()["state"] == "reconciling"
    assert store.raw == before
    assert not await restarted.maintain_auto_resume(quiesced=False)
    assert recovery(store)["state"] == "fencing"
    assert intents(store)["interrupted"]["phase"] == "prepared"


async def test_mutation_unknown_after_success_projects_manual_without_writes(clock: Clock) -> None:
    from mcp_telegram.server import _bounded_account_protection, _is_valid_active_account_protection
    from tests.test_daemon_api import make_server

    store = Store()
    ledger = await _success(store, clock)
    await ledger.prepare("mutation", MUTATION)
    ledger.mark_claimed("mutation")
    await ledger.complete("mutation", known_outcome=False)
    before = store.raw
    status = ledger.status()
    automatic = ledger.auto_resume_status()
    assert automatic["state"] == "manual" and automatic["eligible"] is False
    assert automatic.get("next_trial_at_utc") is None
    server = make_server()
    server._account_safety_status = lambda: {**status, "automatic_resume": automatic}
    result = server._get_account_protection({})
    public = cast(dict[str, object], cast(dict[str, object], result["data"])["account_protection"])
    assert _is_valid_active_account_protection(public)
    bounded = _bounded_account_protection(public)
    assert bounded["recovery"] == "manual"
    assert "Recovery requires operator action" in cast(str, bounded["notice"])
    assert "next_trial_at_utc" not in cast(dict[str, object], bounded["automatic_resume"])
    assert store.raw == before and intents(store)["mutation"]["phase"] == "unknown"
    with pytest.raises(TelegramRpcThrottled):
        ledger.require_writable()


@pytest.mark.parametrize("phase", ["authorized", "failed"])
async def test_legacy_automatic_hold_has_valid_manual_public_projection(clock: Clock, phase: str) -> None:
    from mcp_telegram.server import _bounded_account_protection, _is_valid_active_account_protection
    from tests.test_daemon_api import make_server

    store = Store()
    await _waiting(store, clock)
    legacy = record(store)
    cast(dict[str, object], legacy["recovery"])["state"] = phase
    store.raw = json.dumps(legacy)
    ledger = store.ledger()
    ledger.configure_auto_resume(enabled=True)
    before = store.raw
    automatic = ledger.auto_resume_status()
    assert automatic["state"] == "manual" and automatic["eligible"] is False
    assert automatic.get("phase") is None and automatic.get("next_trial_at_utc") is None
    server = make_server()
    snapshot = ledger.status()
    snapshot["known_retry_until_utc"] = snapshot.pop("retry_until_utc")
    server._account_safety_status = lambda: {**snapshot, "automatic_resume": automatic}
    response = server._get_account_protection({})
    status = cast(dict[str, object], cast(dict[str, object], response["data"])["account_protection"])
    assert _is_valid_active_account_protection(status)
    bounded = _bounded_account_protection(status)
    assert bounded["recovery"] == "manual"
    assert "Recovery requires operator action" in cast(str, bounded["notice"])
    assert store.raw == before


async def test_imported_stuck_receipt_is_read_only_until_maintenance(clock: Clock) -> None:
    store = Store()
    ledger = await _success(store, clock)
    ledger.configure_auto_resume(enabled=False)
    await ledger.prepare("stuck-read", HISTORY)
    ledger.mark_claimed("stuck-read")
    await ledger.complete("stuck-read", known_outcome=False)
    assert recovery(store)["state"] == "succeeded"
    assert record(store)["version"] == 3
    imported = store.raw
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    status = restarted.auto_resume_status()
    assert status["state"] == "reconciling"
    assert status["eligible"] is False and status["block_reason"] == "recovery_reconciliation_pending"
    assert status.get("phase") is None and status.get("next_trial_at_utc") is None
    assert store.raw == imported
    assert not await restarted.maintain_auto_resume(quiesced=False)
    assert recovery(store)["state"] == "fencing"
    assert intents(store)["stuck-read"]["phase"] == "unknown"


async def test_diagnostic_counts_include_all_records_beyond_bounded_list(clock: Clock) -> None:
    store = Store()
    ledger = store.ledger()
    await ledger.verify_account(42)
    store.owners = (42, 42)
    await ledger.prepare("historical", READ)
    ledger.mark_claimed("historical")
    await ledger.complete("historical", known_outcome=False)
    ledger.configure_auto_resume(enabled=True)
    assert not await ledger.maintain_auto_resume()
    clock.advance(300)
    assert await ledger.consume_auto_resume()
    await ledger.finish_recovery(42)
    for index in range(9):
        await ledger.prepare(f"new-{index}", HISTORY)
    for index in range(9):
        ledger.mark_claimed(f"new-{index}")
    for index in range(9):
        await ledger.complete(f"new-{index}", known_outcome=False)
    status = ledger.status()
    assert status["unresolved_request_count"] == 10
    assert status["blocking_request_count"] == 9
    assert status["historical_unknown_request_count"] == 1
    assert status["unresolved_requests_truncated"] is True
    requests = cast(list[dict[str, object]], status["unresolved_requests"])
    assert len(requests) == 8
    assert requests[0]["blocks_acquisition"] is False
    assert all(item["blocks_acquisition"] is True for item in requests[1:])


async def test_interrupted_consumed_trial_preserves_spent_attempt(clock: Clock) -> None:
    store = Store()
    ledger = await _waiting(store, clock)
    clock.advance(3600)
    assert await ledger.consume_auto_resume()
    consumed = cast(dict[str, object], recovery(store)["automatic"])
    total = consumed["attempts_total"]
    restarted = store.ledger()
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    assert cast(dict[str, object], recovery(store)["automatic"])["attempts_total"] == total
    assert not await restarted.consume_auto_resume()


async def test_concurrent_unknown_settlements_keep_both_receipts_and_one_episode(clock: Clock) -> None:
    store = Store()
    ledger = await _success(store, clock)
    original = cast(dict[str, object], recovery(store)["automatic"])
    episode, total = original["episode_id"], original["attempts_total"]
    for attempt in ("one", "two"):
        await ledger.prepare(attempt, HISTORY)
        ledger.mark_claimed(attempt)
    await asyncio.gather(*(ledger.complete(attempt, known_outcome=False) for attempt in ("one", "two")))
    assert {name: item["phase"] for name, item in intents(store).items()} == {"one": "unknown", "two": "unknown"}
    assert recovery(store)["state"] == "fencing"
    automatic = cast(dict[str, object], recovery(store)["automatic"])
    assert automatic["episode_id"] == episode and automatic["attempts_total"] == total
    await ledger.complete("one", known_outcome=False)
    assert intents(store)["one"]["phase"] == "unknown"
    assert cast(dict[str, object], recovery(store)["automatic"])["attempts_total"] == total


async def test_settling_one_claim_does_not_retire_another_live_owner(clock: Clock) -> None:
    from mcp_telegram.server import _bounded_account_protection, _is_valid_active_account_protection
    from tests.test_daemon_api import make_server

    store = Store()
    ledger = await _success(store, clock)
    for attempt in ("settled", "still-live"):
        await ledger.prepare(attempt, HISTORY)
        ledger.mark_claimed(attempt)
    await ledger.complete("settled", known_outcome=False)
    assert not await ledger.maintain_auto_resume(quiesced=True)
    assert recovery(store)["state"] == "fencing"
    before = store.raw
    automatic = ledger.auto_resume_status()
    assert automatic["phase"] == "fencing" and automatic["eligible"] is True
    snapshot = ledger.status()
    snapshot["known_retry_until_utc"] = snapshot.pop("retry_until_utc")
    server = make_server()
    server._account_safety_status = lambda: {**snapshot, "automatic_resume": automatic}
    response = server._get_account_protection({})
    status = cast(dict[str, object], cast(dict[str, object], response["data"])["account_protection"])
    assert _is_valid_active_account_protection(status)
    bounded = _bounded_account_protection(status)
    assert bounded["recovery"] == "automatic"
    assert "Automatic recovery is preparing" in cast(str, bounded["notice"])
    assert store.raw == before
    assert not await ledger.consume_auto_resume()
    assert recovery(store)["state"] == "fencing"
    await ledger.complete("still-live", known_outcome=False)
    assert not await ledger.maintain_auto_resume(quiesced=True)
    assert recovery(store)["state"] == "waiting"
    assert {item["phase"] for item in intents(store).values()} == {"unknown"}


async def test_failed_persistence_does_not_grant_recovery(clock: Clock) -> None:
    store = Store()
    ledger = await _success(store, clock)
    await ledger.prepare("failed-save", HISTORY)
    ledger.mark_claimed("failed-save")
    before = store.raw
    store.fail_next = True
    with pytest.raises(sqlite3.OperationalError, match="synthetic storage failure"):
        await ledger.complete("failed-save", known_outcome=False)
    assert store.raw == before
    assert ledger.protection_reason == "safety_storage_failure"
    assert not await ledger.consume_auto_resume()


async def test_waiting_after_rearmed_success_survives_cross_boot_with_floor(clock: Clock) -> None:
    store = Store()
    ledger = await _success(store, clock)
    await ledger.prepare("rebooted-read", HISTORY)
    ledger.mark_claimed("rebooted-read")
    await ledger.complete("rebooted-read", known_outcome=False)
    await ledger.record_floor(clock.time() + 600)
    assert not await ledger.maintain_auto_resume(quiesced=True)
    before = cast(dict[str, object], recovery(store)["automatic"])
    total = before["attempts_total"]
    clock.monotonic_seconds = 20
    restarted = store.ledger(boot_id="boot-b")
    restarted.configure_auto_resume(enabled=True)
    assert not await restarted.maintain_auto_resume()
    assert not await restarted.consume_auto_resume()
    assert cast(dict[str, object], recovery(store)["automatic"])["attempts_total"] == total
    clock.advance(599)
    assert not await restarted.consume_auto_resume()
    assert intents(store)["rebooted-read"]["phase"] == "unknown"


async def test_cancelled_settlement_waits_for_owned_commit(clock: Clock) -> None:
    store = Store()
    ledger = await _success(store, clock)
    await ledger.prepare("cancelled-read", READ)
    ledger.mark_claimed("cancelled-read")
    store.block()
    task = asyncio.create_task(ledger.complete("cancelled-read", known_outcome=False))
    await store.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    drain = asyncio.create_task(ledger.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    store.released.set()
    await drain
    assert intents(store)["cancelled-read"]["phase"] == "unknown"
    assert not await ledger.maintain_auto_resume(quiesced=True)
    assert recovery(store)["state"] == "waiting"
    clock.advance(100_000)
    assert await ledger.consume_auto_resume()
    reopened = store.ledger()
    reopened.configure_auto_resume(enabled=True)
    assert json.loads(cast(str, store.raw))["version"] == 3
    assert not await reopened.maintain_auto_resume()
    assert record(store)["recovery"] is not None
