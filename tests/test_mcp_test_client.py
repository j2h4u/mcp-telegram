from __future__ import annotations

import sys
from pathlib import Path

import pytest

from devtools.mcp_client.client import McpClientError, StdioMcpClient, execute_script_steps


def _fake_server_command() -> list[str]:
    fixture_path = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    return [sys.executable, str(fixture_path)]


@pytest.mark.asyncio
async def test_mcp_test_client_lists_tools() -> None:
    async with StdioMcpClient(_fake_server_command()) as client:
        tools = await client.list_tools()

    tool_names = [tool["name"] for tool in tools]
    assert tool_names == ["Echo", "Fail"]


@pytest.mark.asyncio
async def test_mcp_test_client_calls_tool() -> None:
    async with StdioMcpClient(_fake_server_command()) as client:
        result = await client.call_tool("Echo", {"value": "hello"})

    assert result["isError"] is False
    assert result["content"][0]["text"] == '{"value": "hello"}'


@pytest.mark.asyncio
async def test_mcp_test_client_surfaces_tool_errors() -> None:
    async with StdioMcpClient(_fake_server_command()) as client:
        with pytest.raises(McpClientError, match="tool failed: Fail"):
            await client.call_tool("Fail", {})


@pytest.mark.asyncio
async def test_mcp_test_client_executes_script_steps() -> None:
    steps = [
        {"action": "list_tools"},
        {"action": "call_tool", "name": "Echo", "arguments": {"value": "script"}},
    ]

    async with StdioMcpClient(_fake_server_command()) as client:
        results = await execute_script_steps(client, steps)

    assert results[0]["action"] == "list_tools"
    assert results[0]["result"][0]["name"] == "Echo"
    assert results[1]["name"] == "Echo"
    assert results[1]["result"]["content"][0]["text"] == '{"value": "script"}'
