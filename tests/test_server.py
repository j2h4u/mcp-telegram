from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent, Tool

from mcp_telegram import server


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"{name} test tool",
        inputSchema={"type": "object", "properties": {}},
    )


@pytest.mark.asyncio
async def test_call_tool_validation_failure_escaped_error_includes_actionable_guidance(monkeypatch) -> None:
    monkeypatch.setitem(server.mapping, "ListDialogs", _tool("ListDialogs"))

    def _raise_validation_error(tool: Tool, **kwargs) -> object:
        raise ValueError("dialog must be a string")

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", _raise_validation_error)

    with pytest.raises(RuntimeError, match="ListDialogs") as exc_info:
        await server.call_tool("ListDialogs", {"dialog": 123})

    message = str(exc_info.value)
    assert "validation" in message.lower() or "argument" in message.lower()
    assert "dialog" in message.lower()
    assert "action:" in message.lower() or "retry" in message.lower() or "check" in message.lower()
    assert message != "Tool ListDialogs failed"


@pytest.mark.asyncio
async def test_call_tool_runtime_failure_escaped_error_includes_actionable_guidance(monkeypatch) -> None:
    monkeypatch.setitem(server.mapping, "ListDialogs", _tool("ListDialogs"))
    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr(
        "mcp_telegram.server.tools.tool_runner",
        AsyncMock(side_effect=RuntimeError("telegram backend timed out")),
    )

    with pytest.raises(RuntimeError, match="ListDialogs") as exc_info:
        await server.call_tool("ListDialogs", {})

    message = str(exc_info.value)
    assert "runtime" in message.lower() or "execution" in message.lower()
    assert "timed out" in message.lower() or "timeout" in message.lower()
    assert "action:" in message.lower() or "retry" in message.lower() or "check" in message.lower()
    assert message != "Tool ListDialogs failed"


@pytest.mark.asyncio
async def test_call_tool_passthrough_action_text_contract(monkeypatch) -> None:
    monkeypatch.setitem(server.mapping, "GetUserInfo", _tool("GetUserInfo"))

    expected = [
        TextContent(
            type="text",
            text='Could not fetch info for user "Iris" (boom).\nAction: Retry GetUserInfo later.',
        )
    ]

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr("mcp_telegram.server.tools.tool_runner", AsyncMock(return_value=expected))

    result = await server.call_tool("GetUserInfo", {"user": "Iris"})

    assert result == expected
    assert result[0].text == expected[0].text
    assert "Action:" in result[0].text
    assert "failed" not in result[0].text


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_control_contract() -> None:
    with pytest.raises(ValueError, match="Unknown tool: MissingTool"):
        await server.call_tool("MissingTool", {})
