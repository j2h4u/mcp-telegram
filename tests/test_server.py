from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent, Tool

from mcp_telegram import server
from mcp_telegram.tools._base import ToolRegistryEntry, ToolResult, tool_description
from mcp_telegram.tools.discovery import ListDialogs

INVENTORY_PATH = Path(__file__).parent / "fixtures" / "52-TOOL-OUTPUT-INVENTORY.md"


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        title=name.replace("_", " ").title(),
        description=f"{name} test tool",
        inputSchema={"type": "object", "properties": {}},
    )


def test_list_messages_reflection_exposes_shared_navigation_schema() -> None:
    tool = server.tool_by_name["list_messages"]
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
    result = await server.call_tool("list_messages", {"dialog": "Backend", "exact_dialog_id": 701})

    assert result.isError is True
    message = result.content[0].text
    assert "validation" in message.lower()
    assert "mutually exclusive" in message.lower()
    assert "exact_dialog_id" in message


def test_search_messages_reflection_exposes_shared_navigation_schema() -> None:
    tool = server.tool_by_name["search_messages"]
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
    monkeypatch.setitem(server.tool_by_name, "list_dialogs", _tool("list_dialogs"))

    def _raise_validation_error(tool: Tool, **kwargs) -> object:
        raise ValueError("dialog must be a string")

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", _raise_validation_error)

    result = await server.call_tool("list_dialogs", {"dialog": 123})

    assert result.isError is True
    message = result.content[0].text
    assert "validation" in message.lower() or "argument" in message.lower()
    assert "dialog" in message.lower()
    assert "action:" in message.lower() or "retry" in message.lower() or "check" in message.lower()
    assert message != "Tool list_dialogs failed"


@pytest.mark.asyncio
async def test_call_tool_runtime_failure_escaped_error_includes_actionable_guidance(monkeypatch) -> None:
    monkeypatch.setitem(server.tool_by_name, "list_dialogs", _tool("list_dialogs"))
    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr(
        "mcp_telegram.server.tools.tool_runner",
        AsyncMock(side_effect=RuntimeError("telegram backend timed out")),
    )

    result = await server.call_tool("list_dialogs", {})

    assert result.isError is True
    message = result.content[0].text
    assert "runtime" in message.lower() or "execution" in message.lower()
    assert "timed out" in message.lower() or "timeout" in message.lower()
    assert "action:" in message.lower() or "retry" in message.lower() or "check" in message.lower()
    assert message != "Tool list_dialogs failed"


@pytest.mark.asyncio
async def test_call_tool_passthrough_action_text_contract(monkeypatch) -> None:
    monkeypatch.setitem(server.tool_by_name, "get_entity_info", _tool("get_entity_info"))

    expected = [
        TextContent(
            type="text",
            text='Could not fetch entity info for \'Iris\' (boom).\nAction: Retry get_entity_info later.',
        )
    ]

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr("mcp_telegram.server.tools.tool_runner", AsyncMock(return_value=ToolResult(content=expected)))

    result = await server.call_tool("get_entity_info", {"entity": "Iris"})

    assert result.content == expected
    assert result.isError is False
    assert result.content[0].text == expected[0].text
    assert "Action:" in result.content[0].text
    assert "failed" not in result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_control_contract() -> None:
    with pytest.raises(ValueError, match="Unknown tool: MissingTool"):
        await server.call_tool("MissingTool", {})


@pytest.mark.asyncio
async def test_call_tool_non_dict_arguments_control_contract() -> None:
    with pytest.raises(TypeError, match="arguments must be dictionary"):
        await server.call_tool("list_dialogs", [])


def test_posture_primary_tools_reflected_in_descriptions() -> None:
    """Primary tools should have [primary] tag in their reflected descriptions."""
    for name in ("list_messages", "search_messages", "get_entity_info", "trace_account_messages"):
        tool = server.tool_by_name[name]
        assert tool.description.startswith("[primary]"), f"{name} missing [primary] prefix"


def test_posture_secondary_tools_reflected_in_descriptions() -> None:
    """Secondary/helper tools should have [secondary/helper] tag in descriptions."""
    for name in ("list_dialogs", "list_topics", "get_usage_stats"):
        tool = server.tool_by_name[name]
        assert tool.description.startswith("[secondary/helper]"), f"{name} missing [secondary/helper] prefix"


def test_list_tools_exposes_snake_case_names_titles_and_annotations() -> None:
    expected_titles = {
        "list_dialogs": "List Dialogs",
        "list_topics": "List Topics",
        "list_messages": "List Messages",
        "search_messages": "Search Messages",
        "get_usage_stats": "Usage Stats",
        "get_dialog_stats": "Dialog Stats",
        "mark_dialog_for_sync": "Mark Sync",
        "get_sync_status": "Sync Status",
        "get_sync_alerts": "Sync Alerts",
        "get_my_recent_activity": "Recent Activity",
        "get_inbox": "Inbox",
        "get_entity_info": "Entity Info",
        "submit_feedback": "Submit Feedback",
        "trace_account_messages": "Account Trace",
    }

    assert set(expected_titles).issubset(server.tool_by_name)
    for name, tool in server.tool_by_name.items():
        assert re.match(r"^[a-z][a-z0-9_]{0,63}$", tool.name)
        assert tool.name == name
        assert 1 <= len(tool.title.split()) <= 3
        assert tool.annotations is not None
    for name, title in expected_titles.items():
        assert server.tool_by_name[name].title == title

    assert server.tool_by_name["list_messages"].annotations.readOnlyHint is True
    assert server.tool_by_name["mark_dialog_for_sync"].annotations.readOnlyHint is False
    assert server.tool_by_name["mark_dialog_for_sync"].annotations.idempotentHint is True
    assert server.tool_by_name["submit_feedback"].annotations.readOnlyHint is False
    assert server.tool_by_name["trace_account_messages"].annotations.readOnlyHint is False
    assert server.tool_by_name["trace_account_messages"].annotations.idempotentHint is True
    assert all(not any(part[:1].isupper() for part in name.split("_")) for name in server.tool_by_name)


def test_phase_52_tool_output_inventory_covers_registered_tools() -> None:
    inventory_text = INVENTORY_PATH.read_text(encoding="utf-8")
    inventory_tools = {
        columns[0].strip().strip("`")
        for line in inventory_text.splitlines()
        if line.startswith("| `")
        for columns in [line.strip("|").split("|")]
    }

    missing_tools = sorted(set(server.tool_by_name) - inventory_tools)

    assert not missing_tools, (
        f"{INVENTORY_PATH.name} is missing registered tool(s): {', '.join(missing_tools)}"
    )


def test_tool_descriptor_preserves_registry_output_schema() -> None:
    output_schema = {
        "type": "object",
        "properties": {
            "dialogs": {"type": "array", "items": {"type": "object"}},
            "count": {"type": "integer"},
        },
        "required": ["dialogs", "count"],
    }
    entry = ToolRegistryEntry(
        cls=ListDialogs,
        posture="secondary/helper",
        annotations=None,
        exported_name="list_dialogs",
        title="List Dialogs",
        output_schema=output_schema,
    )

    tool = tool_description("list_dialogs", ListDialogs, entry)

    assert tool.inputSchema["type"] == "object"
    assert tool.outputSchema == output_schema
    assert tool.title == "List Dialogs"


def test_list_tools_exposes_list_dialogs_output_schema() -> None:
    tool = server.tool_by_name["list_dialogs"]

    assert tool.outputSchema is not None
    assert "dialogs" in tool.outputSchema["properties"]
    assert "count" in tool.outputSchema["required"]


def test_list_tools_structured_output_schema_surface_is_explicit() -> None:
    schema_tools = {name for name, tool in server.tool_by_name.items() if tool.outputSchema is not None}

    assert schema_tools == {
        "list_dialogs",
        "list_topics",
        "list_messages",
        "search_messages",
        "mark_dialog_for_sync",
        "get_sync_status",
        "get_sync_alerts",
        "get_inbox",
        "get_my_recent_activity",
        "get_usage_stats",
        "get_dialog_stats",
        "trace_account_messages",
    }


def test_list_tools_exposes_account_trace_schema_and_title() -> None:
    tool = server.tool_by_name["trace_account_messages"]

    assert tool.title == "Account Trace"
    assert tool.outputSchema is not None
    assert "coverage" in tool.outputSchema["required"]
    assert "coverage_bounds" in tool.outputSchema["properties"]["provenance"]["properties"]
    assert "authorship_basis" in (
        tool.outputSchema["properties"]["groups"]["items"]["properties"]["evidence"]["items"]["properties"]
    )
    assert tool.inputSchema["properties"]["exact_topic_id"]["type"] == "integer"


@pytest.mark.asyncio
async def test_call_tool_preserves_structuredContent_and_text_content(monkeypatch) -> None:
    monkeypatch.setitem(server.tool_by_name, "list_dialogs", _tool("list_dialogs"))
    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr(
        "mcp_telegram.server.tools.tool_runner",
        AsyncMock(
            return_value=ToolResult(
                content=[TextContent(type="text", text="1 dialog")],
                structured_content={"dialogs": [{"id": 1, "name": "Alice"}], "count": 1},
            )
        ),
    )

    result = await server.call_tool("list_dialogs", {})

    assert result.isError is False
    assert result.structuredContent == {"dialogs": [{"id": 1, "name": "Alice"}], "count": 1}
    assert result.content
    assert isinstance(result.content[0], TextContent)
    assert result.content[0].text


@pytest.mark.asyncio
async def test_call_tool_validation_rejects_trace_topic_without_dialog_scope() -> None:
    result = await server.call_tool("trace_account_messages", {"account": "@alice", "exact_topic_id": 7})

    assert result.isError is True
    assert "exact_topic_id requires" in result.content[0].text


@pytest.mark.asyncio
async def test_server_instructions_mention_account_trace(monkeypatch) -> None:
    class _Conn:
        async def get_me(self) -> dict:
            return {"ok": False}

    @asynccontextmanager
    async def _conn_cm():
        yield _Conn()

    monkeypatch.setattr("mcp_telegram.daemon_client.daemon_connection", _conn_cm)

    instructions = await server._build_server_instructions()

    assert "trace_account_messages" in instructions
    assert "exact_topic_id" in instructions
    assert "best_effort_visible" in instructions


def test_posture_covers_all_registered_tools() -> None:
    """Every registered tool must have a posture classification."""
    from mcp_telegram.tools import TOOL_REGISTRY

    for name in server.tool_by_name:
        assert name in TOOL_REGISTRY, f"{name} not in TOOL_REGISTRY"
        _cls, posture, _annotations = TOOL_REGISTRY[name]
        assert posture, f"{name} has empty posture"


def test_posture_get_entity_info_classified_as_primary() -> None:
    """get_entity_info must be classified as primary, not helper."""
    from mcp_telegram.tools import TOOL_REGISTRY

    assert TOOL_REGISTRY["get_entity_info"][1] == "primary", "get_entity_info should be a primary user-task tool"
    tool = server.tool_by_name["get_entity_info"]
    assert tool.description.startswith("[primary]"), "get_entity_info missing [primary] prefix"


def test_primary_tools_have_core_read_search_schema() -> None:
    """Primary read and search tools expose the direct-access schema patterns from Phase 17."""
    # list_messages: must have exact_dialog_id for direct reads
    list_messages = server.tool_by_name["list_messages"]
    lm_props = list_messages.inputSchema["properties"]
    assert "exact_dialog_id" in lm_props, "list_messages missing exact_dialog_id for direct dialog access"
    assert "exact_topic_id" in lm_props, "list_messages missing exact_topic_id for direct topic access"
    assert "navigation" in lm_props, "list_messages missing shared navigation field"

    # search_messages: must keep dialog + query shape for direct scoping
    search_messages = server.tool_by_name["search_messages"]
    sm_props = search_messages.inputSchema["properties"]
    assert "dialog" in sm_props, "search_messages missing dialog for exact numeric ID pattern"
    assert "query" in sm_props, "search_messages missing query"
    assert "navigation" in sm_props, "search_messages missing shared navigation field"

    # get_entity_info: must have entity field for universal entity lookup
    get_entity_info = server.tool_by_name["get_entity_info"]
    gei_props = get_entity_info.inputSchema["properties"]
    assert "entity" in gei_props, "get_entity_info missing entity field for direct lookup"

    trace_account = server.tool_by_name["trace_account_messages"]
    trace_props = trace_account.inputSchema["properties"]
    assert "exact_account_id" in trace_props, "trace_account_messages missing exact_account_id"
    assert "exact_topic_id" in trace_props, "trace_account_messages missing exact_topic_id"
    assert "coverage_goal" in trace_props, "trace_account_messages missing coverage_goal"


def test_helper_tools_remain_available_not_hidden() -> None:
    """Secondary/helper tools remain accessible in the tool surface; they are not removed or marked unavailable."""
    from mcp_telegram.tools import TOOL_REGISTRY

    helper_tools = [
        ("list_dialogs", "secondary/helper"),
        ("list_topics", "secondary/helper"),
        ("get_usage_stats", "secondary/helper"),
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
    """list_messages tool output includes archived warning with coverage pct."""
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

    result = await server.call_tool("list_messages", {"exact_dialog_id": 123})
    text = result.content[0].text
    assert "⚠" in text
    assert "archive" in text.lower()
    assert "2023-11-14" in text
    assert "80%" in text


@pytest.mark.asyncio
async def test_list_messages_tool_archived_warning_unknown_coverage(monkeypatch):
    """list_messages tool output shows 'N messages archived locally' when coverage unknown."""
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

    result = await server.call_tool("list_messages", {"exact_dialog_id": 123})
    text = result.content[0].text
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

    result = await server.call_tool("list_messages", {"exact_dialog_id": 123})
    text = result.content[0].text
    # Must show 2023-11-14 (last_synced_at), NOT 2024-01-01 (access_lost_at)
    assert "2023-11-14" in text
    assert "2024-01-01" not in text
