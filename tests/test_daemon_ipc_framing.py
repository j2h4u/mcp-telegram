"""Large pages, malformed frames, and separate-process navigation contracts."""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest

from mcp_telegram import daemon_ipc
from mcp_telegram.daemon_client import DaemonConnection, DaemonNotRunningError
from mcp_telegram.daemon_ipc import REQUEST_LIMIT, RESPONSE_FRAME_LIMIT, read_response, write_response
from mcp_telegram.pagination import decode_navigation_token
from mcp_telegram.tools.reading import SearchMessages, search_messages


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [100, 500])
async def test_full_cyrillic_page_round_trip(count: int) -> None:
    payload = {"ok": True, "data": {"messages": [{"text": "Ж" * 4000} for _ in range(count)]}}
    writer = _Writer()
    await write_response(cast(asyncio.StreamWriter, writer), payload)
    assert writer.data.startswith(b'{"_ipc_frames":1}\n')
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(writer.data)
    reader.feed_eof()
    assert json.loads(await read_response(reader)) == payload


@pytest.mark.asyncio
async def test_frames_preserve_sequential_requests_and_legacy_lines() -> None:
    writer = _Writer()
    for payload in ({"text": "x" * (REQUEST_LIMIT + 1000)}, {"ok": True}):
        await write_response(cast(asyncio.StreamWriter, writer), payload)
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(writer.data)
    reader.feed_data(json.dumps({"text": "x" * 100000}).encode() + b"\n")
    reader.feed_eof()
    assert json.loads(await read_response(reader)) == {"text": "x" * (REQUEST_LIMIT + 1000)}
    assert json.loads(await read_response(reader)) == {"ok": True}
    assert json.loads(await read_response(reader)) == {"text": "x" * 100000}


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [100 * 1024, REQUEST_LIMIT])
async def test_existing_json_line_clients_read_responses_through_2mib(size: int) -> None:
    overhead = len(json.dumps({"text": ""}, separators=(",", ":"))) + 1
    payload = {"text": "x" * (size - overhead)}
    writer = _Writer()
    await write_response(cast(asyncio.StreamWriter, writer), payload)
    assert len(writer.data) == size
    assert not writer.data.startswith(b'{"_ipc_frames":1}\n')
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(writer.data)
    reader.feed_eof()
    assert json.loads(await reader.readline()) == payload


@pytest.mark.asyncio
async def test_one_byte_beyond_legacy_line_limit_uses_frames() -> None:
    overhead = len(json.dumps({"text": ""}, separators=(",", ":"))) + 1
    payload = {"text": "x" * (REQUEST_LIMIT - overhead + 1)}
    writer = _Writer()
    await write_response(cast(asyncio.StreamWriter, writer), payload)
    assert writer.data.startswith(b'{"_ipc_frames":1}\n')
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(writer.data)
    reader.feed_eof()
    assert json.loads(await read_response(reader)) == payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [bytes(4), (RESPONSE_FRAME_LIMIT + 1).to_bytes(4, "big"), b"\x00\x00", (3).to_bytes(4, "big") + b"x"],
)
async def test_malformed_frames_close_client_connection(body: bytes) -> None:
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(b'{"_ipc_frames":1}\n' + body)
    reader.feed_eof()
    writer = _Writer()
    with pytest.raises(DaemonNotRunningError) as exc:
        await DaemonConnection(reader, cast(asyncio.StreamWriter, writer)).request({"method": "get_sync_status"})
    assert exc.value.kind == "malformed_response"
    assert writer.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("line", [b'{"_ipc_frames":2}\n', b'{ "_ipc_frames": 1 }\n', b'{"\\u005fipc_frames":2}\n', b""])
async def test_unknown_frame_headers_and_eof_close_connection(line: bytes) -> None:
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(line)
    reader.feed_eof()
    writer = _Writer()
    with pytest.raises(DaemonNotRunningError):
        await DaemonConnection(reader, cast(asyncio.StreamWriter, writer)).request({"method": "get_sync_status"})
    assert writer.closed


@pytest.mark.asyncio
async def test_oversize_response_is_explicit_before_frames(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_ipc, "RESPONSE_LIMIT", RESPONSE_FRAME_LIMIT * 3)
    writer = _Writer()
    await write_response(cast(asyncio.StreamWriter, writer), {"text": "x" * (RESPONSE_FRAME_LIMIT * 4)})
    assert json.loads(writer.data)["error"] == "response_too_large"
    assert b"_ipc_frames" not in writer.data


@pytest.mark.asyncio
async def test_cancelled_partial_response_closes_connection() -> None:
    reader = asyncio.StreamReader(limit=REQUEST_LIMIT)
    reader.feed_data(b'{"_ipc_frames":1}\n' + (10).to_bytes(4, "big") + b"x")
    writer = _Writer()
    task = asyncio.create_task(
        DaemonConnection(reader, cast(asyncio.StreamWriter, writer)).request({"method": "get_sync_status"})
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed


_CHILD = r"""
import asyncio, json, sqlite3, sys
from pathlib import Path
from mcp_telegram.sync_db import ensure_sync_schema
from mcp_telegram.fts import INSERT_FTS_SQL, stem_text
from mcp_telegram.daemon_ipc import REQUEST_LIMIT
from tests.test_daemon_api import make_server

async def main():
    ensure_sync_schema(Path(sys.argv[2]))
    db = sqlite3.connect(sys.argv[2])
    for mid in range(1, 5):
        db.execute("INSERT INTO messages(dialog_id,message_id,sent_at,text) VALUES (10,?,?,?)", (mid,1700000000+mid,"needle Ж"))
        db.execute(INSERT_FTS_SQL,(10,mid,stem_text("needle Ж")))
    db.execute("INSERT INTO dialogs(dialog_id,name,type) VALUES (10,'Test','User')")
    db.commit()
    api = make_server(db)
    service = api._get_reading_service()
    async def unread(req):
        return {"ok":True,"data":{"groups":[{"dialog_id":10,"display_name":"Test","category":"user","dialog_type":"User","total_in_chat":100,"messages":[{"message_id":i,"dialog_id":10,"sent_at":1700000000+i,"text":"Ж"*40000,"content_kind":"message_text"} for i in range(100)]}],"read_position_pending_count":0,"read_position_pending_entities":[]}}
    service.list_unread_messages = unread
    socket = await asyncio.start_unix_server(api.handle_client, path=sys.argv[1], limit=REQUEST_LIMIT)
    print("ready", flush=True)
    try:
        async with socket:
            await socket.serve_forever()
    finally:
        await api.shutdown()
        db.close()
asyncio.run(main())
"""


@pytest.mark.asyncio
async def test_separate_process_search_navigation_and_bounded_inbox(tmp_path: Path) -> None:
    socket_path = tmp_path / "daemon.sock"
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _CHILD,
        str(socket_path),
        str(tmp_path / "sync.db"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None and child.stderr is not None
        ready = await asyncio.wait_for(child.stdout.readline(), 60)
        if ready != b"ready\n":
            raise AssertionError((await child.stderr.read()).decode())
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=REQUEST_LIMIT)
        conn = DaemonConnection(reader, writer)

        @asynccontextmanager
        async def connection():
            yield conn

        try:
            with patch("mcp_telegram.tools.reading.daemon_connection", connection):
                first = await search_messages(SearchMessages(query="needle", limit=2))
                assert first.structured_content is not None
                token = first.structured_content["next_navigation"]
                assert isinstance(token, str)
                # The HTTP process cannot verify the daemon's process-local key.
                with pytest.raises(ValueError):
                    decode_navigation_token(token)
                second = await search_messages(SearchMessages(query="needle", limit=2, navigation=token))
                assert not second.is_error
                assert second.structured_content is not None
                assert cast(dict[str, object], second.structured_content["navigation"])["offset"] == 2
                assert first.structured_content["results"] != second.structured_content["results"]
                for kwargs in (
                    {"query": "changed"},
                    {"dialog": "10"},
                    {"message_state": "all"},
                    {"since_utc": "2020-01-01T00:00:00Z"},
                    {"until_utc": "2030-01-01T00:00:00Z"},
                    {"navigation": token[:-1] + ("A" if token[-1] != "A" else "B")},
                ):
                    args = {"query": "needle", "limit": 2, "navigation": token, **kwargs}
                    rejected = await search_messages(SearchMessages.model_validate(args))
                    assert rejected.is_error, kwargs
            inbox = await conn.get_inbox(limit=100, preview_chars=4000, response_chars=24000)
            assert inbox["inbox_projected"] is True
            assert "groups" not in inbox["data"]
            assert len(json.dumps(inbox["data"], separators=(",", ":"))) <= 24000
            assert inbox["data"]["content_truncated_count"] > 0
            raw = await conn.get_inbox(limit=100)
            assert len(raw["data"]["groups"][0]["messages"][0]["text"]) == 40000
        finally:
            writer.close()
            await writer.wait_closed()
    finally:
        if child.returncode is None:
            child.terminate()
        await child.wait()
