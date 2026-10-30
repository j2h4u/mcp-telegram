from __future__ import annotations

import asyncio
from contextvars import Context

import pytest
from telethon import functions, types

from mcp_telegram.telegram_demand import demand_context
from mcp_telegram.telegram_rpc_consumers import DemandKind
from tests.test_telegram_rpc import (
    _gate,
    _RawFutureSender,
    _reset_process_policy,  # noqa: F401 - local autouse fixture
    _scripted_authenticator,  # noqa: F401 - local autouse fixture
    _set_sender,
    _wait_for,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("detached_callback", [False, True])
async def test_auth_reconnect_keeps_original_auth_scope(detached_callback: bool) -> None:
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    with demand_context(DemandKind.OPERATOR_AUTH) as original:
        await gate.begin_authentication()
        if detached_callback:
            callback = asyncio.create_task(gate._handle_auto_reconnect(), context=Context())
        else:
            callback = None
        if callback is None:

            async def respond() -> None:
                await _wait_for(lambda: sender.calls == 1)
                sender.futures[0].set_result([types.User(id=1, is_self=True)])

            response = asyncio.create_task(respond())
            await gate._handle_auto_reconnect()
            await response
        else:
            await _wait_for(lambda: sender.calls == 1)
            sender.futures[0].set_result([types.User(id=1, is_self=True)])
            await callback
        assert len(sender.requests) == 1
        assert isinstance(sender.requests[0], functions.users.GetUsersRequest)
        assert isinstance(sender.requests[0].id[0], types.InputUserSelf)
        assert sender.scopes[0].demand_kind is DemandKind.OPERATOR_AUTH
        assert sender.scopes[0].attempt_evidence is original.attempt_evidence
        assert sender.scopes[0].transport_auth
        assert gate._reconnect_probe is None
        assert not gate.reconnect_event.is_set()
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_kind", [DemandKind.OPERATOR_AUTH, DemandKind.MCP_REMOTE_ACQUISITION])
async def test_auth_reconnect_rejects_foreign_owner_before_send(foreign_kind: DemandKind) -> None:
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication()

        async def foreign_callback() -> None:
            with demand_context(foreign_kind):
                await gate._handle_auto_reconnect()

        callback = asyncio.create_task(foreign_callback(), context=Context())
        with pytest.raises(RuntimeError, match="authentication reconnect inherited another owner"):
            await callback
        assert sender.calls == 0
        assert gate._reconnect_probe is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_auth_reconnect_rejects_different_account() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication()
        callback = asyncio.create_task(gate._handle_auto_reconnect(), context=Context())
        await _wait_for(lambda: sender.calls == 1)
        sender.futures[0].set_result([types.User(id=2, is_self=True)])
        await callback
        assert gate._ensure_safety_ledger().protection_reason == "authentication_account_mismatch"
        assert gate._mb_entity_cache.self_id is None
        assert gate._reconnect_probe is None
    await gate.close_rpc_scheduler()


@pytest.mark.asyncio
async def test_auth_reconnect_missing_retained_owner_fails_closed() -> None:
    gate = _gate()
    sender = _RawFutureSender()
    _set_sender(gate, sender)
    with demand_context(DemandKind.OPERATOR_AUTH):
        await gate.begin_authentication()
        gate._authentication_token = None
        with pytest.raises(RuntimeError, match="authentication reconnect lacks its original owner"):
            await gate._handle_auto_reconnect()
        assert sender.calls == 0
        assert gate._reconnect_probe is None
    await gate.close_rpc_scheduler()
