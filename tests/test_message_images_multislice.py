"""One bounded 4 MiB foreign-DC acquisition across coordinator slices."""

import asyncio
import sqlite3
import struct
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from telethon import functions, types
from telethon.crypto import AuthKey
from telethon.network import authenticator
from telethon.network.connection.tcpfull import FullPacketCodec

from mcp_telegram.image_authorization import ActiveSources
from mcp_telegram.message_images.contracts import ImageScope
from mcp_telegram.message_images.service import MessageImageService
from mcp_telegram.message_images.worker import MessageImageWorker
from mcp_telegram.sqlite_writer import SQLiteWriter
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.sync_transactions import write_transaction
from mcp_telegram.telegram_image_download import TelegramImageGateway
from mcp_telegram.telegram_transport import (
    GuardedConnectionTcpFull,
    GuardedMTProtoPlainSender,
    GuardedMTProtoSender,
    _RequestState,
)
from tests.demand_adapter_helpers import run_ready_slice
from tests.test_telegram_rpc_wire_integration import _WireHarness
from tests.test_telegram_rpc_wire_integration import image_wire as image_wire
from tests.test_telegram_rpc_wire_integration import wire as wire
from tests.test_telegram_transport import _Writer


async def _scripted_auth(plain: object) -> tuple[AuthKey, int]:
    sender = cast(GuardedMTProtoPlainSender, plain)
    await sender.send(functions.ReqPqMultiRequest(1))
    await sender.send(functions.ReqDHParamsRequest(1, 2, b"p", b"q", 3, b"encrypted"))
    await sender.send(functions.SetClientDHParamsRequest(1, 2, b"encrypted"))
    return AuthKey(bytes(reversed(range(256)))), 0


def _install_aux_socket(gate: object, monkeypatch: pytest.MonkeyPatch, sink: _Writer, ready: asyncio.Event) -> None:
    async def dc(_session: object, _lease: object) -> object:
        return SimpleNamespace(ip_address="127.0.0.1", port=443, id=3)

    async def connect(connection: GuardedConnectionTcpFull, *_args: object, **_kwargs: object) -> None:
        connection._owned_epoch += 1
        connection._connected = True
        connection._writer = cast(asyncio.StreamWriter, sink)
        connection._codec = FullPacketCodec(connection)
        connection._send_task = asyncio.create_task(connection._send_loop())
        ready.set()

    async def parked_sender(_sender: GuardedMTProtoSender) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(gate, "_photo_dc_option", dc)
    monkeypatch.setattr(GuardedConnectionTcpFull, "connect", connect)
    monkeypatch.setattr(GuardedMTProtoSender, "_send_loop", parked_sender)
    monkeypatch.setattr(authenticator, "do_authentication", _scripted_auth)


async def _answer_plain(gate: object, sink: _Writer) -> None:
    from mcp_telegram.telegram_rpc import TelegramRpcGate

    rpc_gate = cast(TelegramRpcGate, gate)
    assert rpc_gate._photo_aux_lease is not None and rpc_gate._photo_aux_lease.sender is not None
    connection = cast(GuardedConnectionTcpFull, rpc_gate._photo_aux_lease.sender._connection)
    responses = [types.ResPQ(1, 2, b"pq", []), types.ServerDHParamsOk(1, 2, b"answer"), types.DhGenOk(1, 2, 3)]
    for response in responses:
        await asyncio.wait_for(sink.written.get(), 5)
        data = bytes(response)
        connection._recv_queue.put_nowait((struct.pack("<qqi", 0, 4, len(data)) + data, None))


async def _wire_request(sender: object, connection: GuardedConnectionTcpFull) -> _RequestState:
    owned = cast(GuardedMTProtoSender, sender)
    state = await asyncio.wait_for(owned._send_queue.next_state(), 5)
    await owned._prepare_state(state)
    assert owned.write_packet(connection._send_queue.get_nowait(), connection._send)
    return state


async def _drive_wire(image_wire: _WireHarness, sink: _Writer, ready: asyncio.Event, image: bytes) -> None:
    gate, main, main_conn, _ = image_wire
    state = await _wire_request(main, main_conn)
    now = datetime.now(UTC)
    photo = types.Photo(5, 3, b"private", now, [types.PhotoSize("m", 8, 8, len(image))], 3)
    message = types.Message(7, types.PeerUser(100), date=now, message="", media=types.MessageMediaPhoto(photo=photo))
    state.future.set_result(types.messages.Messages(messages=[message], chats=[], users=[], topics=[]))
    await asyncio.wait_for(ready.wait(), 5)
    await _answer_plain(gate, sink)
    assert gate._photo_aux_lease is not None and gate._photo_aux_lease.sender is not None
    aux_conn = cast(GuardedConnectionTcpFull, gate._photo_aux_lease.sender._connection)
    assert aux_conn._send_task is not None
    aux_conn._send_task.cancel()
    await asyncio.gather(aux_conn._send_task, return_exceptions=True)
    state = await _wire_request(main, main_conn)
    state.future.set_result(types.auth.ExportedAuthorization(id=12, bytes=b"private"))
    assert gate._photo_aux_lease is not None and gate._photo_aux_lease.sender is not None
    aux = gate._photo_aux_lease.sender
    aux_conn = cast(GuardedConnectionTcpFull, aux._connection)
    state = await _wire_request(aux, aux_conn)
    state.future.set_result(types.auth.Authorization(user=types.User(id=42)))
    for offset in range(0, len(image), 512 * 1024):
        state = await _wire_request(aux, aux_conn)
        state.future.set_result(
            types.upload.File(type=types.storage.FileJpeg(), mtime=0, bytes=image[offset : offset + 512 * 1024])
        )


@pytest.mark.asyncio
async def test_foreign_four_mebibytes_preserves_aux_lease_across_four_slices(
    image_wire: _WireHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate, _, _, main_sink = image_wire
    assert gate.session is not None
    monkeypatch.setattr(gate.session, "get_input_entity", lambda _id: types.InputPeerUser(100, 9))
    aux_sink, ready = _Writer(), asyncio.Event()
    _install_aux_socket(gate, monkeypatch, aux_sink, ready)
    db_path = tmp_path / "owner.db"
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
    image = b"\xff\xd8\xff" + b"x" * (4 * 1024 * 1024 - 5) + b"\xff\xd9"

    async def drain() -> None:
        for _ in range(4):
            await run_ready_slice(worker, limit=32, now=100)

    task: asyncio.Task[None] | None = None
    try:
        await service.prepare(100, 7)
        task = asyncio.create_task(drain())
        await _drive_wire(image_wire, aux_sink, ready, image)
        await asyncio.wait_for(task, 10)
        assert (await service.read(100, 7)).image_bytes == image
        assert conn.execute("SELECT state,remaining,setup_remaining FROM message_image_jobs").fetchone() == (
            "complete",
            18,
            10,
        )
        assert len(main_sink.frames) == 2 and len(aux_sink.frames) == 12
        assert gate._photo_aux_lease is None
    finally:
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        await writer.run(sources.unbind)
        await writer.aclose()
        conn.close()
