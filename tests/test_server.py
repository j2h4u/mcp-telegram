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


def test_list_messages_reflection_exposes_shared_navigation_schema() -> None:
    tool = server.mapping["ListMessages"]
    properties = tool.inputSchema["properties"]
    required = tool.inputSchema.get("required", [])

    assert "navigation" in properties
    assert "exact_dialog_id" in properties
    assert "exact_topic_id" in properties
    assert "cursor" not in properties
    assert "from_beginning" not in properties
    assert properties["navigation"]["type"] == "string"
    assert properties["exact_dialog_id"]["type"] == "integer"
    assert properties["exact_topic_id"]["type"] == "integer"
    assert '"newest"' in properties["navigation"]["description"]
    assert '"oldest"' in properties["navigation"]["description"]
    assert "already known" in properties["exact_dialog_id"]["description"]
    assert "Mutually exclusive with dialog" in properties["exact_dialog_id"]["description"]
    assert "full topic catalog" in properties["exact_topic_id"]["description"]
    assert "dialog" not in required


@pytest.mark.asyncio
async def test_call_tool_validation_rejects_conflicting_list_messages_selectors() -> None:
    with pytest.raises(RuntimeError, match="ListMessages") as exc_info:
        await server.call_tool("ListMessages", {"dialog": "Backend", "exact_dialog_id": 701})

    message = str(exc_info.value)
    assert "validation" in message.lower()
    assert "mutually exclusive" in message.lower()
    assert "exact_dialog_id" in message


def test_search_messages_reflection_exposes_shared_navigation_schema() -> None:
    tool = server.mapping["SearchMessages"]
    properties = tool.inputSchema["properties"]

    assert "dialog" in properties
    assert "navigation" in properties
    assert "offset" not in properties
    assert "exact_dialog_id" not in properties
    assert properties["dialog"]["type"] == "string"
    assert "exact numeric dialog id" in properties["dialog"]["description"]
    assert properties["navigation"]["type"] == "string"
    assert "first search page" in properties["navigation"]["description"]
    assert "next_navigation" in properties["navigation"]["description"]


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


def test_posture_primary_tools_reflected_in_descriptions() -> None:
    """Primary tools should have [primary] tag in their reflected descriptions."""
    for name in ("ListMessages", "SearchMessages", "GetUserInfo"):
        tool = server.mapping[name]
        assert tool.description.startswith("[primary]"), f"{name} missing [primary] prefix"


def test_posture_secondary_tools_reflected_in_descriptions() -> None:
    """Secondary/helper tools should have [secondary/helper] tag in descriptions."""
    for name in ("ListDialogs", "ListTopics", "GetMyAccount", "GetUsageStats"):
        tool = server.mapping[name]
        assert tool.description.startswith("[secondary/helper]"), f"{name} missing [secondary/helper] prefix"


def test_posture_covers_all_registered_tools() -> None:
    """Every registered tool must have a posture classification."""
    from mcp_telegram.tools import TOOL_POSTURE
    for name in server.mapping:
        assert name in TOOL_POSTURE, f"{name} not in TOOL_POSTURE"

