"""The canonical image owner drains through actual guarded wire claims."""

import asyncio
import base64
import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from mcp.types import ImageContent
from telethon import types
from telethon.network.connection.tcpfull import FullPacketCodec

from mcp_telegram import server
from mcp_telegram.daemon_api import DaemonAPIServer
from mcp_telegram.daemon_client import DaemonConnection
from mcp_telegram.daemon_ipc import REQUEST_LIMIT, write_response
from mcp_telegram.image_authorization import ActiveSources
from mcp_telegram.message_images.contracts import ImageScope
from mcp_telegram.message_images.service import MessageImageService
from mcp_telegram.message_images.worker import MessageImageWorker
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_image_download import TelegramImageGateway
from mcp_telegram.telegram_transport import GuardedConnectionTcpFull, GuardedMTProtoSender
from mcp_telegram.tools import message_images
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import image_wire as image_wire
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_telegram_transport import _Writer


async def _read_registered_mcp(
    service: MessageImageService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, image: bytes
) -> None:
    api = DaemonAPIServer.__new__(DaemonAPIServer)
    api.bind_message_image_service(service)
    socket = tmp_path / "daemon-image.sock"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request = cast(dict[str, object], json.loads(await reader.readline()))
        await write_response(writer, await api._dispatch(request))
        writer.close()
        await writer.wait_closed()

    listener = await asyncio.start_unix_server(handle, path=socket, limit=REQUEST_LIMIT)

    @asynccontextmanager
    async def connection():
        reader, writer = await asyncio.open_unix_connection(socket, limit=REQUEST_LIMIT)
        try:
            yield DaemonConnection(reader, writer)
        finally:
            writer.close()
            await writer.wait_closed()

    monkeypatch.setattr(message_images, "daemon_connection", connection)
    try:
        result = await server.call_tool("get_message_image", {"exact_dialog_id": 100, "message_id": 7})
        assert not result.is_error
        assert len(result.content) == 1 and isinstance(result.content[0], ImageContent)
        assert base64.b64decode(result.content[0].data) == image
    finally:
        listener.close()
        await listener.wait_closed()


async def _deliver_wire(image_wire: _WireHarness, image: bytes, dc_id: int, connected: asyncio.Event) -> None:
    _, sender, connection, _ = image_wire
    state = await asyncio.wait_for(sender._send_queue.next_state(), timeout=5)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    now = datetime.now(UTC)
    photo = types.Photo(5, 3, b"private", now, [types.PhotoSize("m", 8, 8, len(image))], dc_id)
    message = types.Message(7, types.PeerUser(100), date=now, message="", media=types.MessageMediaPhoto(photo=photo))
    state.future.set_result(types.messages.Messages(messages=[message], chats=[], users=[], topics=[]))
    if dc_id == 3:
        await _authorize_aux(image_wire, connected)
        gate = image_wire[0]
        assert gate._photo_aux_lease is not None and gate._photo_aux_lease.sender is not None
        sender = gate._photo_aux_lease.sender
        connection = cast(GuardedConnectionTcpFull, sender._connection)
    state = await asyncio.wait_for(sender._send_queue.next_state(), timeout=5)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result(types.upload.File(type=types.storage.FileJpeg(), mtime=0, bytes=image))


async def _authorize_aux(image_wire: _WireHarness, connected: asyncio.Event) -> None:
    gate, main, main_conn, _ = image_wire
    await asyncio.wait_for(connected.wait(), 5)
    state = await asyncio.wait_for(main._send_queue.next_state(), 5)
    await main._prepare_state(state)
    assert main.write_packet(main_conn._send_queue.get_nowait(), main_conn._send)
    state.future.set_result(types.auth.ExportedAuthorization(id=12, bytes=b"private"))
    assert gate._photo_aux_lease is not None and gate._photo_aux_lease.sender is not None
    sender = gate._photo_aux_lease.sender
    connection = cast(GuardedConnectionTcpFull, sender._connection)
    state = await asyncio.wait_for(sender._send_queue.next_state(), 5)
    await sender._prepare_state(state)
    assert sender.write_packet(connection._send_queue.get_nowait(), connection._send)
    state.future.set_result(types.auth.Authorization(user=types.User(id=42)))


def _setup_aux(gate: object, monkeypatch: pytest.MonkeyPatch, sink: _Writer, connected: asyncio.Event) -> None:
    async def fake_dc(_session: object, _lease: object) -> object:
        return SimpleNamespace(ip_address="127.0.0.1", port=443, id=3)

    async def fake_connect(sender: GuardedMTProtoSender, connection: GuardedConnectionTcpFull) -> bool:
        connection.bind_owner(sender)
        connection._connected = True
        connection._writer = cast(asyncio.StreamWriter, sink)
        connection._codec = FullPacketCodec(connection)
        sender._connection = connection
        sender._user_connected = True
        sender.auth_key.key = bytes(reversed(range(256)))
        connected.set()
        return True

    monkeypatch.setattr(gate, "_photo_dc_option", fake_dc)
    monkeypatch.setattr(GuardedMTProtoSender, "connect", fake_connect)


@pytest.mark.parametrize("dc_id", [2, 3])
async def test_real_gate_and_writer_deliver_explicit_image(
    image_wire: _WireHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dc_id: int
) -> None:
    gate, _, _, sink = image_wire
    assert gate.session is not None
    monkeypatch.setattr(gate.session, "get_input_entity", lambda _id: types.InputPeerUser(100, 9))
    connected, aux_sink = asyncio.Event(), _Writer()
    if dc_id == 3:
        _setup_aux(gate, monkeypatch, aux_sink, connected)
    db_path = tmp_path / "image-owner.db"
    ensure_sync_schema(db_path)
    conn = sqlite3.connect(db_path)
    with write_transaction(conn):
        conn.execute("UPDATE dialog_directory_state SET account_id=42 WHERE singleton=1")
        conn.execute(
            "INSERT INTO messages(dialog_id,message_id,sent_at,media_kind,media_payload) VALUES(100,7,1,'other','{}')"
        )
        conn.execute("INSERT INTO message_observations(dialog_id,message_id,observation_order) VALUES(100,7,1)")
        conn.execute("INSERT INTO daemon_state(key,value) VALUES('canonical_observation_sequence','1')")
    writer = await SQLiteWriter.open(db_path)
    sources = ActiveSources()
    await writer.run(sources.bind)
    scope = ImageScope(gate.capture_image_scope, gate.claim_image_scope, sources)
    service = MessageImageService(conn, writer, tmp_path / "images", scope, lambda: None)
    worker = MessageImageWorker(conn, writer, tmp_path / "images", scope, cast(TelegramImageGateway, gate))
    image = b"\xff\xd8\xfftest\xff\xd9"
    task: asyncio.Task[object] | None = None
    try:
        assert (await service.prepare(100, 7))["acquisition"] == {"state": "queued"}
        task = asyncio.create_task(run_ready_slice(worker, limit=32, now=100))
        await _deliver_wire(image_wire, image, dc_id, connected)
        await asyncio.wait_for(task, timeout=5)
        result = await service.read(100, 7)
        assert result.image_bytes == image
        assert result.status["acquisition"] == {"state": "complete"}
        await _read_registered_mcp(service, tmp_path, monkeypatch, image)
        assert len(sink.frames) == 2
        assert len(aux_sink.frames) == (2 if dc_id == 3 else 0)
        assert conn.execute("SELECT remaining,setup_remaining FROM message_image_jobs").fetchone() == (
            (28, 13) if dc_id == 3 else (30, 15)
        )
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        await writer.run(sources.unbind)
        await writer.aclose()
        conn.close()
