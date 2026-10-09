"""Bounded Unix IPC requests and incrementally encoded response frames.

Requests and replies fitting the legacy 2 MiB line budget use JSON lines.
Larger replies begin with {"_ipc_frames":1} plus LF, followed by 4-byte big-endian
lengths and ASCII JSON fragments <=64 KiB. A zero length ends the logical JSON
reply. A 64 MiB logical cap covers 500 full Telegram message bodies with margin;
oversize replies return an explicit response_too_large error before any frames.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast

REQUEST_LIMIT = 2 * 1024 * 1024
RESPONSE_FRAME_LIMIT = 64 * 1024
RESPONSE_LIMIT = 64 * 1024 * 1024
_FRAME_HEADER = b'{"_ipc_frames":1}\n'


def get_daemon_socket_path(state_dir: Path) -> Path:
    """Return the daemon Unix socket path below an explicit state directory."""
    return state_dir / "daemon.sock"


def _response_chunks(response: dict) -> Iterator[bytes]:
    buffer = bytearray()
    total = 0
    for token in json.JSONEncoder(ensure_ascii=True, separators=(",", ":")).iterencode(response):
        for start in range(0, len(token), RESPONSE_FRAME_LIMIT):
            part = token[start : start + RESPONSE_FRAME_LIMIT].encode("ascii")
            total += len(part)
            if total > RESPONSE_LIMIT:
                raise ValueError("Daemon response exceeds logical response limit")
            buffer.extend(part)
            while len(buffer) >= RESPONSE_FRAME_LIMIT:
                yield bytes(buffer[:RESPONSE_FRAME_LIMIT])
                del buffer[:RESPONSE_FRAME_LIMIT]
    if buffer:
        yield bytes(buffer)


async def write_response(writer: asyncio.StreamWriter, response: dict) -> None:
    """Keep small replies as JSON lines; stream large replies as bounded frames."""
    chunks = iter(_response_chunks(response))
    prefix: list[bytes] = []
    prefix_size = 0
    try:
        for chunk in chunks:
            prefix.append(chunk)
            prefix_size += len(chunk)
            if prefix_size >= REQUEST_LIMIT:
                break
        else:
            writer.write(b"".join(prefix) + b"\n")
            await writer.drain()
            return
        # Complete the bounded-memory preflight before publishing any frames.
        for _ in chunks:
            pass
    except ValueError:
        writer.write(
            b'{"ok":false,"error":"response_too_large","message":"Daemon response exceeds 64 MiB logical limit; narrow the request."}\n'
        )
        await writer.drain()
        return
    del prefix
    writer.write(_FRAME_HEADER)
    for chunk in _response_chunks(response):
        writer.write(len(chunk).to_bytes(4, "big") + chunk)
        await writer.drain()
    writer.write(bytes(4))
    await writer.drain()


async def read_response(reader: asyncio.StreamReader) -> bytes:
    """Read one complete reply, rejecting oversized or interrupted frames."""
    line = await reader.readline()
    if line == _FRAME_HEADER:
        return await _read_frames(reader)
    if len(line) > REQUEST_LIMIT:
        raise ValueError("Daemon legacy response line exceeds limit")
    try:
        candidate = cast(object, json.loads(line))
    except UnicodeDecodeError, json.JSONDecodeError:
        return line  # The caller reports malformed JSON with its transport context.
    if isinstance(candidate, dict) and "_ipc_frames" in candidate:
        raise ValueError("Daemon returned unsupported or malformed frame header")
    return line


async def _read_frames(reader: asyncio.StreamReader) -> bytes:
    """Reassemble bounded frames until the explicit logical response terminator."""
    result = bytearray()
    while True:
        size = int.from_bytes(await reader.readexactly(4), "big")
        if size == 0:
            if not result:
                raise ValueError("Daemon returned empty framed response")
            return bytes(result)
        if size > RESPONSE_FRAME_LIMIT or len(result) + size > RESPONSE_LIMIT:
            raise ValueError("Daemon response exceeds frame or logical response limit")
        result.extend(await reader.readexactly(size))
