"""Contact RPC updates cross the owned transport and durable intake once."""

# pyright: reportAny=false
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from telethon._updates import GapError
from telethon.network.requeststate import RequestState
from telethon.sessions import SQLiteSession
from telethon.tl import functions, types
from telethon.tl.core import RpcResult, TLMessage

from mcp_telegram.native_write_fence import NativeWriteCheck, NativeWriteReceiptBinding
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.state import telegram_session_lease
from mcp_telegram.sync_db import open_sync_db_reader
from mcp_telegram.telegram_rpc import TelegramRpcGate, _WireAttempt
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from mcp_telegram.telegram_transport import _RequestState
from tests.test_telegram_transport import _Harness
from tests.test_update_delivery import _client, _database
from tests.test_update_delivery_handlers import _manager

DATE = datetime(2026, 10, 1, tzinfo=UTC)


async def _contact_response(client: TelegramRpcGate, wire: _Harness, delete: bool, *, too_long: bool = False) -> object:
    scope = client.retained_auth_scope()
    assert scope is not None and client._image_scope_cell.publish(scope)
    token = client._image_scope_cell.capture()
    assert token is not None
    request = (
        functions.contacts.DeleteContactsRequest([types.InputUser(55, 555)])
        if delete
        else functions.contacts.AddContactRequest(types.InputUser(55, 555), "Fixture", "", "", False)
    )
    rpc_scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, 10.0, asyncio.current_task()
    )
    binding = NativeWriteReceiptBinding(
        "contact", 99, 2, scope.auth_key_id, "key", "delete" if delete else "create", 55, "a" * 64, 1
    )

    async def revalidate():
        return lambda: True

    check = NativeWriteCheck(binding, bytes(request), revalidate)
    owner = _WireAttempt(
        client._wire_issuer,
        "attempt",
        "contact",
        rpc_scope,
        claimed=1,
        native_check=check,
        native_request=request,
        native_body=bytes(request),
        native_scope_token=token,
    )
    vars(wire.authority)["contact_response_scope"] = client.contact_response_scope
    state = cast(_RequestState, RequestState(request))
    owned = wire.sender._remember(state, owner, application=True)
    owned.claims = 1
    state.msg_id = 123
    wire.sender._pending_state[123] = state
    queue: asyncio.Queue[object] = asyncio.Queue()
    wire.sender._updates_queue = queue
    response = (
        types.UpdatesTooLong()
        if too_long
        else types.Updates(
            [types.UpdateUser(55)], [types.User(id=55, first_name="Fixture", contact=not delete)], [], DATE, 0
        )
    )
    await wire.sender._handle_rpc_result(TLMessage(400, 0, RpcResult(123, bytes(response), None)))
    assert queue.qsize() == 1
    envelope = queue.get_nowait()
    assert vars(envelope)["_self_outgoing"] is False
    return envelope


@pytest.mark.parametrize("delete", [False, True])
async def test_owned_contact_response_reaches_durable_update_pipeline(tmp_path: Path, delete: bool) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    lease_context = telegram_session_lease(tmp_path)
    client.bind_session_lease(lease_context.__enter__())
    wire = _Harness()
    try:
        await client.start_update_delivery(99)
        _manager(client, reader, writer)
        if delete:
            initial: list[object] = []
            client._message_box.process_updates(
                types.UpdateShort(types.UpdateUser(55), DATE), client._mb_entity_cache, initial
            )
            seeded = await client._preprocess_updates(
                initial, [types.User(id=55, first_name="Fixture", contact=True)], []
            )
            await asyncio.gather(*(client._dispatch_update(update) for update in seeded))
        envelope = await _contact_response(client, wire, delete)
        processed: list[object] = []
        users, chats = client._message_box.process_updates(envelope, client._mb_entity_cache, processed)
        assert len(processed) == 1
        admitted = await client._preprocess_updates(processed, users, chats)
        await asyncio.gather(*(client._dispatch_update(update) for update in admitted))
        row = reader.execute(
            "SELECT fields_json FROM entity_profile_semantic_snapshots WHERE owner_account_id=99 AND entity_id=55"
        ).fetchone()
        assert row is not None
        assert json.loads(row[0])["user.contact"] is (not delete)
    finally:
        await wire.close()
        cast(SQLiteSession, client.session).close()
        assert client._session_pin is not None
        client._session_pin._release()
        lease_context.__exit__(None, None, None)
        reader.close()
        await writer.aclose()


@pytest.mark.parametrize(
    ("claimed", "authorized", "cancelled", "too_long"),
    [(False, True, False, False), (True, False, False, False), (True, True, True, False), (True, True, False, True)],
)
async def test_contact_response_is_queued_only_for_valid_owner(
    claimed: bool, authorized: bool, cancelled: bool, too_long: bool
) -> None:
    from mcp_telegram.auth_scope import AUTH_SCOPE_VERSION, TelegramAuthScope
    from mcp_telegram.image_authorization import ScopeCell

    wire = _Harness()
    cell = ScopeCell()
    assert cell.publish(TelegramAuthScope(AUTH_SCOPE_VERSION, 99, 2, 3))
    token = cell.capture()
    vars(wire.authority)["contact_response_scope"] = lambda _owner, _request, _result: token if authorized else None
    try:
        request = functions.contacts.DeleteContactsRequest([types.InputUser(55, 555)])
        state = cast(_RequestState, RequestState(request))
        owned = wire.sender._remember(state, object(), application=True)
        owned.claims = int(claimed)
        state.msg_id = 321
        wire.sender._pending_state[321] = state
        queue: asyncio.Queue[object] = asyncio.Queue()
        wire.sender._updates_queue = queue
        if cancelled:
            state.future.cancel()
        response = types.UpdatesTooLong() if too_long else types.UpdateShort(types.UpdateUser(55), DATE)
        await wire.sender._handle_rpc_result(TLMessage(500, 0, RpcResult(321, bytes(response), None)))
        assert queue.qsize() == int(claimed and authorized)
        if claimed and authorized:
            result = queue.get_nowait()
            assert vars(result)["_mcp_contact_scope_token"] is token
            assert vars(result)["_self_outgoing"] is False
            assert type(result) is type(response)
    finally:
        await wire.close()


async def test_queued_contact_response_is_fenced_after_scope_change(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer, channel=False)
    lease_context = telegram_session_lease(tmp_path)
    client.bind_session_lease(lease_context.__enter__())
    wire = _Harness()
    try:
        await client.start_update_delivery(99)
        envelope = await _contact_response(client, wire, False)
        client._image_scope_cell.begin_change()
        with pytest.raises(GapError):
            client._message_box.process_updates(envelope, client._mb_entity_cache, [])
        response = cast(types.Updates, envelope)
        with pytest.raises(RuntimeError, match="scope"):
            await client._preprocess_updates(list(response.updates), list(response.users), [])
    finally:
        await wire.close()
        cast(SQLiteSession, client.session).close()
        assert client._session_pin is not None
        client._session_pin._release()
        lease_context.__exit__(None, None, None)
        await writer.aclose()


async def test_contact_too_long_uses_messagebox_gap_path(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    client = _client(tmp_path / "session", writer, channel=False)
    lease_context = telegram_session_lease(tmp_path)
    client.bind_session_lease(lease_context.__enter__())
    wire = _Harness()
    try:
        await client.start_update_delivery(99)
        envelope = await _contact_response(client, wire, False, too_long=True)
        with pytest.raises(GapError):
            client._message_box.process_updates(envelope, client._mb_entity_cache, [])
        from telethon._updates.messagebox import ENTRY_ACCOUNT

        assert ENTRY_ACCOUNT in client._message_box.getting_diff_for
    finally:
        await wire.close()
        cast(SQLiteSession, client.session).close()
        assert client._session_pin is not None
        client._session_pin._release()
        lease_context.__exit__(None, None, None)
        await writer.aclose()
