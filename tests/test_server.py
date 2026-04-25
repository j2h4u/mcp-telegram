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
    tool = server.tool_by_name["ListMessages"]
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
    tool = server.tool_by_name["SearchMessages"]
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
    monkeypatch.setitem(server.tool_by_name, "ListDialogs", _tool("ListDialogs"))

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
    monkeypatch.setitem(server.tool_by_name, "ListDialogs", _tool("ListDialogs"))
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
    monkeypatch.setitem(server.tool_by_name, "GetEntityInfo", _tool("GetEntityInfo"))

    expected = [
        TextContent(
            type="text",
            text='Could not fetch entity info for \'Iris\' (boom).\nAction: Retry GetEntityInfo later.',
        )
    ]

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr("mcp_telegram.server.tools.tool_runner", AsyncMock(return_value=expected))

    result = await server.call_tool("GetEntityInfo", {"entity": "Iris"})

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
    for name in ("ListMessages", "SearchMessages", "GetEntityInfo"):
        tool = server.tool_by_name[name]
        assert tool.description.startswith("[primary]"), f"{name} missing [primary] prefix"


def test_posture_secondary_tools_reflected_in_descriptions() -> None:
    """Secondary/helper tools should have [secondary/helper] tag in descriptions."""
    for name in ("ListDialogs", "ListTopics", "GetMyAccount", "GetUsageStats"):
        tool = server.tool_by_name[name]
        assert tool.description.startswith("[secondary/helper]"), f"{name} missing [secondary/helper] prefix"


def test_posture_covers_all_registered_tools() -> None:
    """Every registered tool must have a posture classification."""
    from mcp_telegram.tools import TOOL_REGISTRY

    for name in server.tool_by_name:
        assert name in TOOL_REGISTRY, f"{name} not in TOOL_REGISTRY"
        _cls, posture, _annotations = TOOL_REGISTRY[name]
        assert posture, f"{name} has empty posture"


def test_posture_get_entity_info_classified_as_primary() -> None:
    """GetEntityInfo must be classified as primary, not helper."""
    from mcp_telegram.tools import TOOL_REGISTRY

    assert TOOL_REGISTRY["GetEntityInfo"][1] == "primary", "GetEntityInfo should be a primary user-task tool"
    tool = server.tool_by_name["GetEntityInfo"]
    assert tool.description.startswith("[primary]"), "GetEntityInfo missing [primary] prefix"


def test_primary_tools_have_core_read_search_schema() -> None:
    """Primary read and search tools expose the direct-access schema patterns from Phase 17."""
    # ListMessages: must have exact_dialog_id for direct reads
    list_messages = server.tool_by_name["ListMessages"]
    lm_props = list_messages.inputSchema["properties"]
    assert "exact_dialog_id" in lm_props, "ListMessages missing exact_dialog_id for direct dialog access"
    assert "exact_topic_id" in lm_props, "ListMessages missing exact_topic_id for direct topic access"
    assert "navigation" in lm_props, "ListMessages missing shared navigation field"

    # SearchMessages: must keep dialog + query shape for direct scoping
    search_messages = server.tool_by_name["SearchMessages"]
    sm_props = search_messages.inputSchema["properties"]
    assert "dialog" in sm_props, "SearchMessages missing dialog for exact numeric ID pattern"
    assert "query" in sm_props, "SearchMessages missing query"
    assert "navigation" in sm_props, "SearchMessages missing shared navigation field"

    # GetEntityInfo: must have entity field for universal entity lookup
    get_entity_info = server.tool_by_name["GetEntityInfo"]
    gei_props = get_entity_info.inputSchema["properties"]
    assert "entity" in gei_props, "GetEntityInfo missing entity field for direct lookup"


def test_helper_tools_remain_available_not_hidden() -> None:
    """Secondary/helper tools remain accessible in the tool surface; they are not removed or marked unavailable."""
    from mcp_telegram.tools import TOOL_REGISTRY

    helper_tools = [
        ("ListDialogs", "secondary/helper"),
        ("ListTopics", "secondary/helper"),
        ("GetMyAccount", "secondary/helper"),
        ("GetUsageStats", "secondary/helper"),
    ]

    for tool_name, expected_posture in helper_tools:
        # Must exist in TOOL_REGISTRY mapping
        assert tool_name in TOOL_REGISTRY, f"{tool_name} missing from TOOL_REGISTRY"
        assert TOOL_REGISTRY[tool_name][1] == expected_posture, f"{tool_name} has wrong posture classification"

        # Must be registered in server mapping (not hidden/unavailable)
        assert tool_name in server.tool_by_name, f"{tool_name} not registered in server"
        tool = server.tool_by_name[tool_name]

        # Must be marked as secondary in description
        assert tool.description.startswith("[secondary/helper]"), (
            f"{tool_name} description missing [secondary/helper] prefix"
        )


# ---------------------------------------------------------------------------
# Tool-layer archived warning tests (Plan 36-02, Task 2)
# ---------------------------------------------------------------------------


def _make_mock_conn(list_messages_response: dict):
    """Build a mock daemon connection that returns a preset list_messages response."""
    mock_conn = AsyncMock()
    mock_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_conn.__aexit__ = AsyncMock(return_value=False)
    mock_conn.list_messages = AsyncMock(return_value=list_messages_response)
    return mock_conn


@pytest.mark.asyncio
async def test_list_messages_tool_archived_warning_with_coverage(monkeypatch):
    """ListMessages tool output includes archived warning with coverage pct."""
    mock_conn = _make_mock_conn(
        {
            "ok": True,
            "data": {
                "messages": [],
                "source": "sync_db",
                "next_navigation": None,
                "dialog_access": "archived",
                "access_lost_at": 1704067200,
                "last_synced_at": 1699990000,  # 2023-11-14
                "last_event_at": 1699999000,
                "sync_coverage_pct": 80,
            },
        }
    )
    monkeypatch.setattr("mcp_telegram.tools.reading.daemon_connection", lambda: mock_conn)

    result = await server.call_tool("ListMessages", {"exact_dialog_id": 123})
    text = result[0].text
    assert "⚠" in text
    assert "archive" in text.lower()
    assert "2023-11-14" in text
    assert "80%" in text


@pytest.mark.asyncio
async def test_list_messages_tool_archived_warning_unknown_coverage(monkeypatch):
    """ListMessages tool output shows 'N messages archived locally' when coverage unknown."""
    mock_conn = _make_mock_conn(
        {
            "ok": True,
            "data": {
                "messages": [],
                "source": "sync_db",
                "next_navigation": None,
                "dialog_access": "archived",
                "access_lost_at": 1700000000,
                "last_synced_at": None,
                "last_event_at": 1699999000,
                "sync_coverage_pct": None,
                "archived_message_count": 150,
            },
        }
    )
    monkeypatch.setattr("mcp_telegram.tools.reading.daemon_connection", lambda: mock_conn)

    result = await server.call_tool("ListMessages", {"exact_dialog_id": 123})
    text = result[0].text
    assert "⚠" in text
    assert "150 messages archived locally" in text


@pytest.mark.asyncio
async def test_list_messages_tool_uses_last_synced_at_not_access_lost_at(monkeypatch):
    """Verify tool uses last_synced_at for the archive date, NOT access_lost_at."""
    mock_conn = _make_mock_conn(
        {
            "ok": True,
            "data": {
                "messages": [],
                "source": "sync_db",
                "next_navigation": None,
                "dialog_access": "archived",
                "access_lost_at": 1704067200,  # 2024-01-01
                "last_synced_at": 1699990000,  # 2023-11-14
                "last_event_at": 1699999000,
                "sync_coverage_pct": None,
            },
        }
    )
    monkeypatch.setattr("mcp_telegram.tools.reading.daemon_connection", lambda: mock_conn)

    result = await server.call_tool("ListMessages", {"exact_dialog_id": 123})
    text = result[0].text
    # Must show 2023-11-14 (last_synced_at), NOT 2024-01-01 (access_lost_at)
    assert "2023-11-14" in text
    assert "2024-01-01" not in text
