from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
from devtools.mcp_client import client as mcp_client_module
from devtools.mcp_client.client import HttpMcpClient, McpClientError
from mcp.client import streamable_http


@pytest.mark.parametrize(
    "blocked", ["initialize", "cancel_initialize", "tools/list", "prompts/list", "prompts/get", "tools/call", "DELETE"]
)
async def test_http_client_bounds_sdk_requests_and_cleanup(monkeypatch: pytest.MonkeyPatch, blocked: str) -> None:
    cancelled: list[str] = []
    initializing = asyncio.Event()

    async def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content) if request.method == "POST" else {}
        method = payload.get("method", request.method)
        if method == blocked or (blocked == "cancel_initialize" and method == "initialize"):
            initializing.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(method)
        if request.method == "GET":
            return httpx2.Response(405)
        if request.method == "DELETE":
            return httpx2.Response(204)
        if "id" not in payload:
            return httpx2.Response(202)
        result = (
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "deadline-test", "version": "1"},
            }
            if method == "initialize"
            else {"tools": [{"name": "example", "inputSchema": {"type": "object"}}]}
        )
        return httpx2.Response(
            200,
            headers={"mcp-session-id": "deadline-test"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(streamable_http, "create_mcp_http_client", lambda: http_client)
    client = HttpMcpClient("http://deadline-test/mcp", timeout_seconds=0.1)
    existing_tasks = asyncio.all_tasks()
    started = asyncio.get_running_loop().time()

    async def use_client() -> None:
        async with client:
            actions = {
                "tools/list": client.list_tools,
                "prompts/list": client.list_prompts,
                "prompts/get": lambda: client.get_prompt("example"),
                "tools/call": lambda: client.call_tool("example"),
            }
            if blocked in actions:
                await actions[blocked]()

    if blocked == "cancel_initialize":
        task = asyncio.create_task(use_client())
        await initializing.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(McpClientError):
            async with asyncio.timeout(1):
                await use_client()
    assert asyncio.get_running_loop().time() - started < 1
    assert cancelled == ["initialize" if blocked == "cancel_initialize" else blocked]
    assert http_client.is_closed
    assert client._session is None
    await client.stop()
    await asyncio.sleep(0)
    assert asyncio.all_tasks() == existing_tasks


async def test_tool_result_arrival_is_logged_when_output_validation_times_out(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    arrived = asyncio.Event()
    validation_schema_fetch = asyncio.Event()
    cancelled: list[str] = []

    async def respond(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content) if request.method == "POST" else {}
        method = payload.get("method", request.method)
        if method == "tools/list":
            validation_schema_fetch.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(method)
        if request.method == "GET":
            return httpx2.Response(405)
        if request.method == "DELETE":
            return httpx2.Response(204)
        if "id" not in payload:
            return httpx2.Response(202)
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "deadline-test", "version": "1"},
            }
        elif method == "tools/call":
            arrived.set()
            result = {"content": [], "structuredContent": {"ok": True}}
        else:
            result = {"tools": [{"name": "example", "inputSchema": {"type": "object"}}]}
        return httpx2.Response(
            200,
            headers={"mcp-session-id": "deadline-test"},
            json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
        )

    http_client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    monkeypatch.setattr(streamable_http, "create_mcp_http_client", lambda: http_client)
    client = HttpMcpClient("http://deadline-test/mcp", timeout_seconds=0.1)
    existing_tasks = asyncio.all_tasks()

    with caplog.at_level("DEBUG", logger=mcp_client_module.__name__):
        with pytest.raises(McpClientError, match="tools/call timed out"):
            async with client:
                await client.call_tool("example")

    assert arrived.is_set()
    assert validation_schema_fetch.is_set()
    assert cancelled == ["tools/list"]
    assert "mcp_tool_result_received tool=example" in caplog.text
    assert "mcp_tool_result_validation_complete tool=example" in caplog.text
    assert "outcome=cancelled" in caplog.text
    assert any(record.levelname == "WARNING" and "outcome=cancelled" in record.message for record in caplog.records)
    assert http_client.is_closed
    assert client._session is None
    await asyncio.sleep(0)
    assert asyncio.all_tasks() == existing_tasks


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_http_client_requires_a_finite_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        HttpMcpClient("http://deadline-test/mcp", timeout_seconds=timeout)
