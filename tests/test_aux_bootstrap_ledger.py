import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest

from mcp_telegram.state import telegram_session_lease
from tests.test_telegram_rpc_safety import Clock, Store, intents
from tests.test_telegram_rpc_safety import clock as clock


def provenance(purpose: str) -> dict[str, object]:
    return {
        "purpose": purpose,
        "main_scope": {"dc_id": 2, "key_id": 99, "account_id": 42},
        "aux_dc_id": 4,
        "aux_key_id": 123,
        "lease_id": "lease",
        "work_id": "work",
        "attempt_generation": "attempt",
        "source_generation": "source",
        "contained": False,
        "lock_scope": {"version": 1, "device": 1, "inode": 1},
        "lock_owner_id": "a" * 32,
    }


@pytest.mark.parametrize("purpose", ["export", "import"])
async def test_contained_unknown_retains_evidence_across_restart(purpose: str) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance(purpose)
    method = (
        "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd"
        if purpose == "export"
        else "telethon.tl.functions.auth.ImportAuthorizationRequest:a57a7dad"
    )
    transport = (2, 99) if purpose == "export" else (4, 123)
    await ledger.prepare("one", method, transport_identity=transport, aux_bootstrap=aux)
    cast(dict[str, object], aux["main_scope"])["account_id"] = 999
    ledger.mark_claimed("one")
    await ledger.complete("one", known_outcome=False)
    assert ledger.protection_reason == "unresolved_outbound_outcome"
    with pytest.raises(ValueError):
        await ledger._contain_aux_unknown("one", captured_scope=provenance(purpose)["main_scope"], lease_id="other")  # type: ignore[arg-type]
    await ledger._contain_aux_unknown(
        "one", captured_scope=cast(dict[str, object], provenance(purpose)["main_scope"]), lease_id="lease"
    )
    assert ledger.protection_reason is None
    assert intents(store)["one"]["phase"] == "unknown"
    await ledger.complete("one", known_outcome=True)
    assert intents(store)["one"]["phase"] == "unknown"
    assert store.ledger().protection_reason is None
    assert store.ledger(key_id=101).protection_reason == "safety_auth_scope_mismatch"


async def test_other_unknown_and_restriction_remain_after_containment() -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    method = "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd"
    await ledger.prepare("one", method, aux_bootstrap=aux)
    await ledger.prepare("two", "users.GetFullUserRequest")
    ledger.mark_claimed("one")
    ledger.mark_claimed("two")
    await ledger.complete("one", known_outcome=False)
    await ledger.complete("two", known_outcome=False)
    await ledger._contain_aux_unknown(
        "one", captured_scope=cast(dict[str, object], aux["main_scope"]), lease_id="lease"
    )
    assert ledger.protection_reason == "unresolved_outbound_outcome"
    assert intents(store)["two"]["phase"] == "unknown"


async def test_containment_does_not_clear_a_concurrent_flood_hold() -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    await ledger.prepare("one", "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd", aux_bootstrap=aux)
    ledger.mark_claimed("one")
    await ledger.record_floor(100.0, circuit_open=True)
    await ledger.complete("one", known_outcome=False)
    await ledger._contain_aux_unknown(
        "one", captured_scope=cast(dict[str, object], aux["main_scope"]), lease_id="lease"
    )
    assert ledger.protection_reason == "flood_wait_circuit_open"
    assert store.ledger().protection_reason == "flood_wait_circuit_open"


async def test_late_flood_keeps_contained_unknown_and_persists_floor(clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    await ledger.prepare("one", "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd", aux_bootstrap=aux)
    ledger.mark_claimed("one")
    await ledger.complete("one", known_outcome=False)
    await ledger._contain_aux_unknown(
        "one", captured_scope=cast(dict[str, object], aux["main_scope"]), lease_id="lease"
    )
    await ledger.complete("one", known_outcome=True, floor_until_utc=clock.time() + 120)
    assert intents(store)["one"]["phase"] == "unknown"
    assert store.ledger().floor_deadline >= clock.monotonic() + 120


async def test_invalid_or_uncontained_provenance_cannot_authorize_restart() -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    malformed = deepcopy(aux)
    malformed["contained"] = True
    method = "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd"
    with pytest.raises(ValueError):
        await ledger.prepare("invalid", method, aux_bootstrap=malformed)
    malformed = deepcopy(aux)
    malformed["aux_key_id"] = 99
    with pytest.raises(ValueError):
        await ledger.prepare("reused-main-key", method, aux_bootstrap=malformed)
    await ledger.prepare("one", method, aux_bootstrap=aux)
    ledger.mark_claimed("one")
    await ledger.complete("one", known_outcome=False)
    restarted = store.ledger()
    assert restarted.protection_reason == "unresolved_outbound_outcome"
    with pytest.raises(ValueError):
        await restarted._contain_aux_unknown(
            "one", captured_scope=cast(dict[str, object], aux["main_scope"]), lease_id="lease"
        )


@pytest.mark.parametrize("settled", [True, False])
async def test_restart_requires_new_matching_kernel_lease(tmp_path: Path, settled: bool) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("import")
    with telegram_session_lease(tmp_path) as old:
        aux["lock_scope"] = old.identity()
        aux["lock_owner_id"] = old.owner_id()
        pin = old._pin()
        await ledger.prepare(
            "one",
            "telethon.tl.functions.auth.ImportAuthorizationRequest:a57a7dad",
            transport_identity=(4, 123),
            aux_bootstrap=aux,
        )
        ledger.mark_claimed("one")
        if settled:
            await ledger.complete("one", known_outcome=False)
        restarted = store.ledger()
        await restarted._reconcile_aux_after_restart(old)
        assert restarted.protection_reason == "unresolved_outbound_outcome"
    with pytest.raises(RuntimeError):
        with telegram_session_lease(tmp_path):
            pass
    pin._release()
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    with telegram_session_lease(other_dir) as wrong:
        await restarted._reconcile_aux_after_restart(wrong)
        assert restarted.protection_reason == "unresolved_outbound_outcome"
    with telegram_session_lease(tmp_path) as current:
        await restarted._reconcile_aux_after_restart(current)
        assert restarted.protection_reason is None
        assert intents(store)["one"]["phase"] == "unknown"
        assert cast(dict[str, object], intents(store)["one"]["aux_bootstrap"])["contained"] is True
    with pytest.raises(ValueError):
        await restarted._reconcile_aux_after_restart(current)


async def test_restart_keeps_legacy_and_other_unknowns_blocked(tmp_path: Path) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    with telegram_session_lease(tmp_path) as old:
        aux["lock_scope"] = old.identity()
        aux["lock_owner_id"] = old.owner_id()
        await ledger.prepare("one", "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd", aux_bootstrap=aux)
        await ledger.prepare("two", "users.GetFullUserRequest")
        ledger.mark_claimed("one")
        ledger.mark_claimed("two")
        await ledger.complete("one", known_outcome=False)
        await ledger.complete("two", known_outcome=False)
    with telegram_session_lease(tmp_path) as current:
        restarted = store.ledger()
        await restarted._reconcile_aux_after_restart(current)
        assert restarted.protection_reason == "unresolved_outbound_outcome"
        assert intents(store)["two"]["phase"] == "unknown"
    payload = json.loads(cast(str, store.raw))
    item = payload["intents"]["one"]["aux_bootstrap"]
    item.pop("lock_scope")
    item.pop("lock_owner_id")
    item["contained"] = False
    store.raw = json.dumps(payload)
    with telegram_session_lease(tmp_path) as current:
        restarted = store.ledger()
        await restarted._reconcile_aux_after_restart(current)
        assert restarted.protection_reason == "unresolved_outbound_outcome"
        assert not cast(dict[str, object], intents(store)["one"]["aux_bootstrap"])["contained"]


async def test_restart_keeps_kernel_pin_until_cancelled_commit_finishes(tmp_path: Path) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    with telegram_session_lease(tmp_path) as old:
        aux["lock_scope"] = old.identity()
        aux["lock_owner_id"] = old.owner_id()
        await ledger.prepare("one", "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd", aux_bootstrap=aux)
        ledger.mark_claimed("one")
        await ledger.complete("one", known_outcome=False)
    restarted = store.ledger()
    store.block()
    with telegram_session_lease(tmp_path) as current:
        task = asyncio.create_task(restarted._reconcile_aux_after_restart(current))
        await store.entered.wait()
        task.cancel()
    with pytest.raises(RuntimeError):
        with telegram_session_lease(tmp_path):
            pass
    store.released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    with telegram_session_lease(tmp_path):
        assert not restarted._unresolved()


async def test_restart_containment_preserves_flood_protection(tmp_path: Path, clock: Clock) -> None:
    store = Store(owners=(42, 42))
    ledger = store.ledger()
    await ledger.verify_account(42)
    aux = provenance("export")
    with telegram_session_lease(tmp_path) as old:
        aux["lock_scope"] = old.identity()
        aux["lock_owner_id"] = old.owner_id()
        await ledger.prepare("one", "telethon.tl.functions.auth.ExportAuthorizationRequest:e5bfffcd", aux_bootstrap=aux)
        ledger.mark_claimed("one")
        await ledger.record_floor(clock.time() + 120, circuit_open=True)
        await ledger.complete("one", known_outcome=False)
    restarted = store.ledger()
    with telegram_session_lease(tmp_path) as current:
        await restarted._reconcile_aux_after_restart(current)
    assert restarted.protection_reason == "flood_wait_circuit_open"
    assert restarted.floor_deadline >= clock.monotonic() + 120
    assert intents(store)["one"]["phase"] == "unknown"
