import copy
import json
from typing import cast

import pytest

from mcp_telegram.telegram_rpc import _validate_safety_record
from tests.test_telegram_rpc_safety import Store


def _legacy(store: Store, *, version: int = 2, state: str = "waiting") -> dict[str, object]:
    record = copy.deepcopy(store.ledger()._record)
    record["version"] = version
    record["scope"] = {"stage": "verified", "dc_id": 2, "key_id": 99, "account_id": 42}
    if version == 1:
        del record["methods"]
    floor = {"boot_id": "boot-a", "until_monotonic": 700.0, "until_utc": 1600.0, "remaining_seconds": 600.0}
    record["floor"] = dict(floor)
    record["protection"] = "flood_wait_circuit_open"
    record["recovery"] = {
        "generation": "grant-a",
        "state": state,
        "scope": dict(cast(dict[str, object], record["scope"])),
        "consumed_run": "old-run" if state == "consumed" else None,
        "acknowledged_protection": None,
        "automatic": {"attempts": 2, "due": dict(floor)},
    }
    return record


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("state", ["waiting", "consumed"])
async def test_migration_retains_legacy_authority_and_persists_with_cas(version: int, state: str) -> None:
    store = Store(owners=(42, 42))
    old = _legacy(store, version=version, state=state)
    assert _validate_safety_record(old)
    store.raw = json.dumps(old)
    original = store.raw
    ledger = store.ledger()
    assert not ledger._storage_failed
    assert ledger._auto_resume_rebased
    result = ledger._record
    recovery = cast(dict[str, object], result["recovery"])
    automatic = cast(dict[str, object], recovery["automatic"])
    assert result["version"] == 3 and result["generation"] == old["generation"]
    assert recovery["state"] == state
    assert recovery["consumed_run"] == ("old-run" if state == "consumed" else None)
    assert automatic["attempts_total"] == 2
    assert cast(dict[str, int], automatic["attempts_by_policy"])["flood_wait"] == 2
    assert cast(dict[str, int], automatic["failure_stages"])["flood_wait"] == 2
    previous_automatic = cast(dict[str, object], cast(dict[str, object], old["recovery"])["automatic"])
    assert automatic["scheduled_due"] == previous_automatic["due"]
    assert automatic["quiet_anchor"] is None and automatic["bound_intent_ids"] == []
    assert store.raw == original
    await ledger._commit(lambda _: None)
    assert store.raw != original and json.loads(cast(str, store.raw))["version"] == 3
    if state == "consumed":
        assert ledger.protection_reason == "recovery_trial_incomplete"


def test_v3_rejects_invalid_dispositions_and_counter_shapes() -> None:
    store = Store(owners=(42, 42))
    store.raw = json.dumps(_legacy(store))
    record = store.ledger()._record
    assert _validate_safety_record(record)
    automatic = cast(dict[str, object], cast(dict[str, object], record["recovery"])["automatic"])
    automatic["attempts_total"] = 3
    assert not _validate_safety_record(record)
    automatic["attempts_total"] = 2
    automatic["policy_version"] = True
    assert not _validate_safety_record(record)
    automatic["policy_version"] = 1
    scope = {"dc_id": 2, "key_id": 99, "account_id": 42}
    record["intents"] = {
        "one": {
            "method": "telethon.tl.functions.messages.SaveDraftRequest:ad0fa15c",
            "run": "run",
            "phase": "unknown",
            "scope": scope,
            "policy_recovery": {
                "policy_version": 1,
                "episode_id": "episode",
                "grant_generation": "grant",
                "scope": scope,
            },
        }
    }
    assert not _validate_safety_record(record)
    intent = cast(dict[str, dict[str, object]], record["intents"])["one"]
    intent["method"] = "telethon.tl.functions.users.GetUsersRequest:0d91a548"
    assert _validate_safety_record(record)
    intent["phase"] = "prepared"
    assert not _validate_safety_record(record)


def test_manual_grant_is_not_converted_to_automatic() -> None:
    store = Store(owners=(42, 42))
    record = _legacy(store)
    recovery = cast(dict[str, object], record["recovery"])
    del recovery["automatic"]
    recovery.update(state="authorized", acknowledged_protection="unresolved_outbound_outcome")
    store.raw = json.dumps(record)
    ledger = store.ledger()
    assert not ledger._storage_failed
    assert ledger._record["recovery"] == recovery
    assert ledger._auto_resume_rebased


def test_legacy_exhaustion_remains_latched() -> None:
    store = Store(owners=(42, 42))
    record = _legacy(store, state="failed")
    record["protection"] = "automatic_resume_exhausted"
    recovery = cast(dict[str, object], record["recovery"])
    cast(dict[str, object], recovery["automatic"])["attempts"] = 6
    store.raw = json.dumps(record)
    ledger = store.ledger()
    migrated = cast(dict[str, object], ledger._record["recovery"])
    assert migrated["state"] == "exhausted"
    assert cast(dict[str, object], migrated["automatic"])["attempts_total"] == 6
    assert ledger.protection_reason == "automatic_resume_exhausted"
