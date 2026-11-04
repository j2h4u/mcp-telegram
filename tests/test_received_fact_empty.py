"""TG-220: empty contact-response envelopes through the actual intake owners."""

import asyncio
import sqlite3
from contextlib import closing
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
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_rpc import TelegramRpcGate, _WireAttempt
from mcp_telegram.telegram_rpc_scheduler import RpcServiceClass, TelegramRpcScope, TelegramRpcSource
from mcp_telegram.telegram_transport import _RequestState
from tests.test_contact_response_delivery import DATE
from tests.test_telegram_transport import _Harness
from tests.test_update_delivery import _client, _database, _pending
from tests.test_update_delivery_handlers import _manager


async def _wire_response(
    client: TelegramRpcGate, wire: _Harness, response: types.Updates | types.UpdatesCombined
) -> object:
    scope = client.retained_auth_scope()
    assert scope is not None and client._image_scope_cell.publish(scope)
    token = client._image_scope_cell.capture()
    assert token is not None
    request = functions.contacts.AddContactRequest(types.InputUser(55, 555), "Fixture", "", "", False)
    rpc_scope = TelegramRpcScope(
        TelegramRpcSource.MCP_INTERACTIVE, RpcServiceClass.INTERACTIVE, 10, asyncio.current_task()
    )
    binding = NativeWriteReceiptBinding("contact", 99, 2, scope.auth_key_id, "key", "create", 55, "a" * 64, 1)

    async def revalidate():
        return lambda: True

    owner = _WireAttempt(
        client._wire_issuer,
        "attempt",
        "contact",
        rpc_scope,
        claimed=1,
        native_check=NativeWriteCheck(binding, bytes(request), revalidate),
        native_request=request,
        native_body=bytes(request),
        native_scope_token=token,
    )
    vars(wire.authority)["contact_response_scope"] = client.contact_response_scope
    state = cast(_RequestState, RequestState(request))
    wire.sender._remember(state, owner, application=True).claims = 1
    state.msg_id = 123
    wire.sender._pending_state[123] = state
    queue: asyncio.Queue[object] = asyncio.Queue()
    wire.sender._updates_queue = queue
    await wire.sender._handle_rpc_result(TLMessage(400, 0, RpcResult(123, bytes(response), None)))
    return queue.get_nowait()


@pytest.mark.parametrize("combined", [False, True])
async def test_empty_updates_sidecars_cross_contact_response_intake(tmp_path: Path, combined: bool) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    lease = telegram_session_lease(tmp_path)
    client.bind_session_lease(lease.__enter__())
    wire = _Harness()
    try:

        def seed(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (-7,'Old group','group')")
                conn.execute("INSERT INTO entities(id,type,name,updated_at) VALUES (-7,'group','Old group',1)")

        await writer.run(seed)
        await client.start_update_delivery(99)
        _manager(client, reader, writer)
        users: list[types.TypeUser] = [types.User(55, access_hash=555, first_name="Fixture", contact=True)]
        chats: list[types.TypeChat] = [
            types.Chat(id=7, title="Group", photo=types.ChatPhotoEmpty(), participants_count=2, date=DATE, version=1)
        ]
        response = (
            types.UpdatesCombined([], users, chats, DATE, 0, 0)
            if combined
            else types.Updates([], users, chats, DATE, 0)
        )
        envelope = await _wire_response(client, wire, response)
        processed: list[object] = []
        supplied_users, supplied_chats = client._message_box.process_updates(
            envelope, client._mb_entity_cache, processed
        )
        assert processed == [envelope]
        assert len(supplied_users) == len(supplied_chats) == 1
        admitted = await client._preprocess_updates(processed, supplied_users, supplied_chats)
        await asyncio.gather(*(client._dispatch_update(update) for update in admitted))
        assert _pending(path) is None
        assert reader.execute("SELECT id FROM entities WHERE id=55").fetchone() == (55,)
        assert reader.execute(
            "SELECT entity_id FROM entity_profile_semantic_snapshots WHERE owner_account_id=99 AND entity_id=55"
        ).fetchone() == (55,)
        assert (
            reader.execute(
                "SELECT entity_id FROM entity_profile_semantic_snapshots WHERE owner_account_id=100 AND entity_id=55"
            ).fetchone()
            is None
        )
        cached = cast(SQLiteSession, client.session).get_input_entity(types.PeerUser(55))
        assert isinstance(cached, types.InputPeerUser) and cached.user_id == 55
    finally:
        await wire.close()
        cast(SQLiteSession, client.session).close()
        assert client._session_pin is not None
        client._session_pin._release()
        lease.__exit__(None, None, None)
        reader.close()
        await writer.aclose()

    with closing(sqlite3.connect(path)) as reopened:
        assert reopened.execute("SELECT id FROM entities WHERE id=55").fetchone() == (55,)


async def test_empty_contact_envelope_is_rejected_after_auth_transition(tmp_path: Path) -> None:
    path = _database(tmp_path)
    writer = await SQLiteWriter.open(path)
    reader = open_sync_db_reader(path)
    client = _client(tmp_path / "session", writer, channel=False)
    lease = telegram_session_lease(tmp_path)
    client.bind_session_lease(lease.__enter__())
    wire = _Harness()
    try:
        await client.start_update_delivery(99)
        _manager(client, reader, writer)
        response = types.Updates([], [types.User(55, access_hash=555, first_name="Fixture")], [], DATE, 0)
        envelope = await _wire_response(client, wire, response)
        client._image_scope_cell.begin_change()
        with pytest.raises(GapError):
            client._message_box.process_updates(envelope, client._mb_entity_cache, [])
        assert _pending(path) is None
        assert reader.execute("SELECT id FROM entities WHERE id=55").fetchone() is None
    finally:
        await wire.close()
        cast(SQLiteSession, client.session).close()
        assert client._session_pin is not None
        client._session_pin._release()
        lease.__exit__(None, None, None)
        reader.close()
        await writer.aclose()
