from __future__ import annotations

import asyncio
import json

import httpx2
import pytest
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


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_http_client_requires_a_finite_positive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        HttpMcpClient("http://deadline-test/mcp", timeout_seconds=timeout)
