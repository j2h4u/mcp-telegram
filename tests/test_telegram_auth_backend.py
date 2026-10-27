"""Stopped authentication preserves retained state and rejects foreign identities."""

import asyncio
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from telethon import TelegramClient
from telethon.crypto import AuthKey
from telethon.events import Raw
from telethon.sessions import SQLiteSession
from telethon.tl import functions, types

from mcp_telegram.config import TelegramRpcSchedulerConfig
from mcp_telegram.telegram_demand import RpcAttemptBudget, RpcAttemptEvidence, demand_context
from mcp_telegram.telegram_rpc import TelegramRpcBudget, TelegramRpcGate
from mcp_telegram.telegram_rpc_consumers import DemandKind
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from tests.helpers import reset_rpc_process_policy
from tests.test_telegram_rpc_safety import Store, intents, record
from tests.test_telegram_rpc_wire_integration import _Circuit, _WireHarness
from tests.test_telegram_rpc_wire_integration import wire as wire


@pytest.fixture
async def auth_backend(tmp_path: Path) -> AsyncIterator[tuple[TelegramRpcGate, Store, SQLiteSession]]:
    reset_rpc_process_policy()
    session = SQLiteSession(str(tmp_path / "auth"))
    session.set_dc(2, "127.0.0.1", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    session.process_entities([types.User(777, access_hash=123, first_name="retained")])
    session.set_update_state(0, types.updates.State(5, 6, datetime.now(UTC), 7, 0))
    session.save()
    store = Store()
    gate = TelegramRpcGate(
        session,
        1,
        "test",
        rpc_budget=TelegramRpcBudget(30, 60),
        circuit_status=_Circuit,
        fallback_wait_seconds=1,
        cooldown_buffer_seconds=0,
        transient_retry_delays_seconds=(),
        scheduler_policy=TelegramRpcSchedulerConfig(),
        safety_store=store.api(),
    )
    await gate.verify_startup_account(42)
    store.owners = (42, 42)
    try:
        yield gate, store, session
    finally:
        await gate.disconnect()
        await gate.close_rpc_scheduler()
        reset_rpc_process_policy()


@pytest.mark.parametrize("replace_credentials", [False, True])
async def test_auth_preserves_key_unless_explicitly_replaced_and_retains_safety_and_session_facts(
    auth_backend: tuple[TelegramRpcGate, Store, SQLiteSession], replace_credentials: bool
) -> None:
    gate, store, session = auth_backend
    ledger = gate._ensure_safety_ledger()
    await ledger.record_floor(time.time() - 1)
    await ledger.prepare("uncertain", "users.GetFullUserRequest")
    ledger.mark_claimed("uncertain")
    await ledger.complete("uncertain", known_outcome=False)
    before = record(store)
    assert session.auth_key is not None
    old_key = session.auth_key.key
    peer = session.get_input_entity(777)
    checkpoint = session.get_update_state(0)
    with demand_context(DemandKind.OPERATOR_AUTH):
        with pytest.raises(ValueError, match="acknowledgement"):
            await gate.begin_authentication(replace_credentials=replace_credentials)
        await gate.begin_authentication(acknowledge_unknown_outcome=True, replace_credentials=replace_credentials)
    if replace_credentials:
        assert session.auth_key is None
    else:
        assert session.auth_key is not None and session.auth_key.key == old_key
    assert session.get_input_entity(777) == peer
    assert session.get_update_state(0) == checkpoint
    assert store.owners == (42, 42)
    after = record(store)
    assert after["floor"] == before["floor"] and after["intents"] == before["intents"]
    transition = cast(dict[str, object], after["transition"])
    assert transition["purpose"] == "authentication" and transition["expected_account"] == 42
    if replace_credentials:
        await ledger.bind_key(2, 12345, auth_only=True)
    assert await ledger.validate_authentication_account(42, finish=True)
    assert ledger.verified_account_id == 42
    assert ledger.protection_reason == "unresolved_outbound_outcome"
    assert intents(store)["uncertain"]["phase"] == "unknown"
    assert record(store)["floor"] == before["floor"]


async def test_explicit_replacement_cannot_bypass_an_active_known_floor(
    auth_backend: tuple[TelegramRpcGate, Store, SQLiteSession],
) -> None:
    gate, store, session = auth_backend
    await gate._ensure_safety_ledger().record_floor(time.time() + 50_000)
    before = store.raw
    assert session.auth_key is not None
    old_key = session.auth_key.key
    with demand_context(DemandKind.OPERATOR_AUTH), pytest.raises(ValueError, match="restriction"):
        await gate.begin_authentication(acknowledge_unknown_outcome=True, replace_credentials=True)
    assert store.raw == before
    assert session.auth_key is not None and session.auth_key.key == old_key


async def test_wrong_login_account_is_rejected_before_vendor_session_mutation(
    auth_backend: tuple[TelegramRpcGate, Store, SQLiteSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, store, session = auth_backend
    vendor_login = AsyncMock(side_effect=AssertionError("foreign login must not reach vendor mutation"))
    monkeypatch.setattr(TelegramClient, "_on_login", vendor_login)
    checkpoint = session.get_update_state(0)
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication()
        with pytest.raises(RuntimeError, match="different account"):
            await gate._on_login(types.User(99, access_hash=999))
    vendor_login.assert_not_awaited()
    assert session.get_update_state(0) == checkpoint
    assert store.owners == (42, 42)
    assert record(store)["protection"] == "authentication_account_mismatch"


async def test_wrong_self_reply_is_rejected_before_public_future_succeeds(wire: _WireHarness) -> None:
    gate, sender, connection, _sink = wire
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication()
        scope = TelegramRpcScope(
            TelegramRpcSource.MCP_INTERACTIVE,
            RpcServiceClass.INTERACTIVE,
            time.monotonic() + 10,
            asyncio.current_task(),
            attempt_budget=RpcAttemptBudget(2),
            attempt_evidence=RpcAttemptEvidence(),
        )
        result = sender.send_owned(functions.users.GetUsersRequest([types.InputUserSelf()]), scope)
        state = await sender._send_queue.next_state()
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        assert sender.write_packet(packet, connection._send)
        state.future.set_result([types.User(99, access_hash=999)])
        with pytest.raises(RuntimeError, match="different account"):
            await result
    assert gate._ensure_safety_ledger().protection_reason == "authentication_account_mismatch"


async def test_confirmed_logout_clears_only_credentials_after_the_durable_reply(wire: _WireHarness) -> None:
    gate, sender, connection, _sink = wire
    assert gate.session is not None and gate.session.auth_key is not None
    gate.session.process_entities([types.User(777, access_hash=123, first_name="retained")])
    peer = gate.session.get_input_entity(777)
    original_key = gate.session.auth_key.key

    async def logout() -> None:
        with demand_context(DemandKind.OPERATOR_LOGOUT):
            await gate.logout_account()

    operation = asyncio.create_task(logout())
    try:
        state = await asyncio.wait_for(sender._send_queue.next_state(), 2)
        assert type(state.request) is functions.auth.LogOutRequest
        await sender._prepare_state(state)
        packet = connection._send_queue.get_nowait()
        assert sender.write_packet(packet, connection._send)
        assert gate.session.auth_key.key == original_key
        assert not operation.done()
        state.future.set_result(types.auth.LoggedOut())
        await asyncio.wait_for(operation, 2)
        assert gate.session.auth_key is None
        assert gate.session.get_input_entity(777) == peer
        receipt = gate._ensure_safety_ledger()._record
        assert receipt["intents"] == {}
        assert cast(dict[str, object], receipt["scope"])["stage"] == "key_pending"
        assert cast(dict[str, object], receipt["transition"])["phase"] == "logout_confirmed"
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)


async def test_auth_receive_dispatches_only_login_tokens_without_implicit_acquisition(
    auth_backend: tuple[TelegramRpcGate, Store, SQLiteSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, _store, session = auth_backend
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication()
    sender = cast(GuardedMTProtoSender, gate._main_sender)
    connection = GuardedConnectionTcpFull("127.0.0.1", 443, 2, loggers=gate._log)
    connection.bind_owner(sender)
    connection._connected = True
    sender._connection = connection
    received: list[object] = []
    forbidden = AsyncMock(side_effect=AssertionError("AUTH notification dispatch must remain local"))
    monkeypatch.setattr(gate, "get_me", forbidden)
    monkeypatch.setattr(gate, "_dispatch_update", forbidden)
    monkeypatch.setattr(gate, "_call_registered", forbidden)
    checkpoint = session.get_update_state(0)

    async def token_callback(update: object) -> None:
        received.append(update)
        connection._connected = False

    gate.add_event_handler(token_callback, Raw(types.UpdateLoginToken))
    gate.add_event_handler(forbidden, Raw())
    token = types.UpdateLoginToken()
    await gate._updates_queue.put(
        types.Updates(
            [types.UpdateUserName(99, "foreign", "", []), token],
            [types.User(99, access_hash=999)],
            [],
            datetime.now(UTC),
            1,
        )
    )
    await asyncio.wait_for(gate._update_loop(), timeout=2)
    assert received == [token]
    assert vars(token)["_entities"] == {}
    forbidden.assert_not_awaited()
    assert session.get_update_state(0) == checkpoint
    with pytest.raises(ValueError):
        session.get_input_entity(99)
