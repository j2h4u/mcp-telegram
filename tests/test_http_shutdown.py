from __future__ import annotations

import asyncio
import base64
import signal
import sys

import httpx2
import pytest
from mcp.client import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import ImageContent, TextContent

_SERVER = r"""
import asyncio
import base64
import os
import signal
import socket
import sys

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
import mcp_telegram.server as http_server


async def list_tools(_ctx, _params):
    return types.ListToolsResult(
        tools=[
            types.Tool(name="hold", inputSchema={"type": "object"}),
            types.Tool(name="read", inputSchema={"type": "object"}),
            types.Tool(name="get_message_image", inputSchema={"type": "object"}),
        ]
    )


async def call_tool(_ctx, _params):
    if _params.name == "get_message_image":
        image = base64.b64encode(b"\xff\xd8\xfflocal image\xff\xd9").decode()
        return types.CallToolResult(content=[types.ImageContent(type="image", data=image, mimeType="image/jpeg")])
    if _params.name != "hold":
        return types.CallToolResult(content=[types.TextContent(type="text", text="completed")])
    release_tool = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_reader(sys.stdin.fileno(), lambda: (os.read(sys.stdin.fileno(), 1), release_tool.set()))
    print("TOOL_STARTED", flush=True)
    try:
        await release_tool.wait()
    finally:
        loop.remove_reader(sys.stdin.fileno())
    return types.CallToolResult(content=[types.TextContent(type="text", text="completed")])


mcp_server = Server("shutdown-probe", on_list_tools=list_tools, on_call_tool=call_tool)
http_server.bootstrap_server = lambda: mcp_server


async def build_instructions():
    return "Local shutdown test"


http_server._build_server_instructions = build_instructions


class ProbeServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        print(f"READY {port}", flush=True)

    def handle_exit(self, sig, frame):
        super().handle_exit(sig, frame)
        if sig == signal.SIGTERM:
            print("SHUTDOWN_STARTED", flush=True)


uvicorn.Server = ProbeServer
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
asyncio.run(http_server.run_mcp_http_server(host="127.0.0.1", port=port))
"""


async def _read_line(process: asyncio.subprocess.Process) -> str:
    assert process.stdout is not None
    line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
    return line.decode().rstrip()


async def _start_server() -> tuple[asyncio.subprocess.Process, str]:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _SERVER,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        ready_line = await _read_line(process)
        assert ready_line.startswith("READY "), ready_line
        return process, f"http://127.0.0.1:{ready_line.removeprefix('READY ')}/mcp/"
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def _stop(process: asyncio.subprocess.Process) -> bytes:
    process.terminate()
    assert await _read_line(process) == "SHUTDOWN_STARTED"
    _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    assert process.returncode == -signal.SIGTERM
    return stderr


@pytest.mark.asyncio
@pytest.mark.parametrize("last_event_id", [None, "17"])
async def test_get_is_rejected_after_security_validation_and_shutdown_is_clean(last_event_id: str | None) -> None:
    process, url = await _start_server()
    try:
        async with httpx2.AsyncClient(timeout=5) as client:
            headers = {"Accept": "text/event-stream"}
            if last_event_id is not None:
                headers["Last-Event-ID"] = last_event_id
            response = await client.get(url, headers=headers)
            assert response.status_code == 405
            assert response.headers["allow"] == "POST"
            assert response.content == b""

            invalid_host = await client.get(url, headers={"Host": "untrusted.invalid"})
            assert invalid_host.status_code == 421
            invalid_origin = await client.get(url, headers={"Origin": "https://untrusted.invalid"})
            assert invalid_origin.status_code == 403

        stderr = await _stop(process)
        assert b"ASGI callable returned without completing response" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_inflight_stateless_json_post_completes_after_shutdown_begins() -> None:
    process, url = await _start_server()
    try:
        async with httpx2.AsyncClient(timeout=5) as client:
            initialize = await client.post(
                url,
                headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "shutdown-test", "version": "1"},
                    },
                },
            )
            assert initialize.status_code == 200

            call_task = asyncio.create_task(
                client.post(
                    url,
                    headers={
                        "Accept": "application/json, text/event-stream",
                        "Content-Type": "application/json",
                        "MCP-Protocol-Version": "2025-06-18",
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "hold", "arguments": {}},
                    },
                )
            )
            assert await _read_line(process) == "TOOL_STARTED"
            process.send_signal(signal.SIGTERM)
            assert await _read_line(process) == "SHUTDOWN_STARTED"
            assert process.stdin is not None
            process.stdin.write(b"R")
            await process.stdin.drain()
            response = await call_task
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == -signal.SIGTERM
        assert response.status_code == 200
        assert response.json()["result"]["content"][0]["text"] == "completed"
        assert b"ASGI callable returned without completing response" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_official_sdk_uses_stateless_post_for_tools_and_native_image() -> None:
    process, url = await _start_server()
    try:
        async with streamable_http_client(url, terminate_on_close=False) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                initialized = await client.initialize()
                assert initialized.server_info.name == "shutdown-probe"
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {"hold", "read", "get_message_image"}
                text_result = await client.call_tool("read", {})
                assert isinstance(text_result.content[0], TextContent)
                assert text_result.content[0].text == "completed"
                image_result = await client.call_tool("get_message_image", {})
                assert len(image_result.content) == 1
                assert isinstance(image_result.content[0], ImageContent)
                assert image_result.content[0].mime_type == "image/jpeg"
                assert base64.b64decode(image_result.content[0].data) == b"\xff\xd8\xfflocal image\xff\xd9"

        stderr = await _stop(process)
        assert b"ASGI callable returned without completing response" not in stderr
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
