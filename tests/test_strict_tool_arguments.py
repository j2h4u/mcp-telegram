"""The shared MCP boundary rejects complete invalid requests before dispatch."""

from datetime import datetime
from typing import cast
from unittest.mock import AsyncMock

import pytest
from mcp.types import TextContent
from pydantic import BaseModel

from mcp_telegram import server, tools
from mcp_telegram.models import DialogType
from mcp_telegram.tools._base import ToolArgs, ToolRegistryEntry, ToolResult, tool_args, tool_description
from mcp_telegram.tools.unread import GetInbox

VALID_ARGUMENTS: dict[str, dict[str, object]] = {
    "trace_account_messages": {"exact_account_id": 123},
    "get_my_recent_activity": {},
    "list_conversation_changes": {},
    "list_dialogs": {},
    "list_topics": {"dialog": "123"},
    "get_entity_info": {"entity": "123"},
    "submit_feedback": {"message": "test"},
    "list_messages": {"exact_dialog_id": 123},
    "search_messages": {"query": "test"},
    "get_usage_stats": {},
    "get_dialog_stats": {"dialog": "123"},
    "mark_dialog_for_sync": {"dialog_id": 123},
    "get_sync_status": {"dialog_id": 123},
    "get_inbox": {},
    "get_unread_summary": {},
}


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    runner = AsyncMock(return_value=ToolResult(structured_content={"accepted": True}))
    monkeypatch.setattr(tools, "tool_runner", runner)
    monkeypatch.setattr(server, "_schedule_telemetry", lambda event: None)
    monkeypatch.setattr(
        server, "_account_protection_status", AsyncMock(return_value=server._account_protection_unavailable())
    )
    return runner


def test_cases_cover_the_entire_registry() -> None:
    assert set(VALID_ARGUMENTS) == set(tools.TOOL_REGISTRY)


@pytest.mark.parametrize("name", VALID_ARGUMENTS)
@pytest.mark.parametrize(
    "invalid", [{"unsupported": "private-value"}, {"timezone": 123}, {"timezone": "private-value"}]
)
async def test_all_tools_reject_then_accept_without_executing_invalid_call(
    name: str, invalid: dict[str, object], handler: AsyncMock
) -> None:
    arguments = {**VALID_ARGUMENTS[name], **invalid}
    result = await server.call_tool(name, arguments)
    assert result.is_error is True
    payload = cast(dict[str, object], result.structured_content)
    assert cast(dict[str, object], payload["error"])["code"] == "validation_error"
    text_content = result.content[0]
    assert isinstance(text_content, TextContent)
    assert "Action:" in text_content.text
    assert "private-value" not in text_content.text
    handler.assert_not_awaited()
    assert arguments == {**VALID_ARGUMENTS[name], **invalid}

    valid_result = await server.call_tool(name, VALID_ARGUMENTS[name])
    assert valid_result.is_error is False
    assert valid_result.content == []
    assert cast(dict[str, object], valid_result.structured_content)["accepted"] is True
    handler.assert_awaited_once()


@pytest.mark.parametrize(
    ("name", "invalid"),
    [("get_sync_status", {"dialog_id": value}) for value in (True, False, 123.0, 1.5, "123")]
    + [("mark_dialog_for_sync", {"enable": value}) for value in (1, 0, "true", "false")]
    + [
        ("search_messages", {"limit": 0}),
        ("search_messages", {"limit": 201}),
        ("search_messages", {"message_state": "draft"}),
        ("search_messages", {"query": ""}),
        ("search_messages", {"exact_dialog_id": 123}),
        ("search_messages", {"dialog": 123}),
        ("get_my_recent_activity", {"dialog_kinds": "group"}),
        ("get_my_recent_activity", {"dialog_kinds": [123]}),
        ("get_inbox", {"include_dialog_types": ["invalid"]}),
        ("get_inbox", {"include_dialog_types": []}),
        ("list_conversation_changes", {"kinds": ["invalid"]}),
        ("submit_feedback", {"severity": "critical"}),
        ("list_messages", {"since_utc": "yesterday"}),
        ("list_messages", {"dialog": "123"}),
        ("list_dialogs", {"view": "folders", "folder_id": 1}),
        ("get_entity_info", {"exact_entity_id": 123}),
        ("search_messages", {"dialog": None}),
    ],
)
async def test_invalid_scalar_collection_constraint_and_scope_arguments_never_dispatch(
    name: str, invalid: dict[str, object], handler: AsyncMock
) -> None:
    result = await server.call_tool(name, {**VALID_ARGUMENTS[name], **invalid})
    assert result.is_error is True
    handler.assert_not_awaited()


async def test_json_enum_and_utc_time_forms_are_accepted(handler: AsyncMock) -> None:
    result = await server.call_tool(
        "get_inbox", {"include_dialog_types": ["user"], "since_utc": "2026-10-10T00:00:00Z"}
    )
    assert result.is_error is False
    args = cast(GetInbox, handler.call_args.args[0])
    assert args.include_dialog_types == [DialogType.USER]
    assert args.since_utc == "2026-10-10T00:00:00Z"


class NestedInput(BaseModel):
    count: int
    at: datetime


class NestedToolArgs(ToolArgs):
    nested: NestedInput
    metadata: dict[str, object]


class TypedMappingArgs(ToolArgs):
    metadata: dict[str, int]


def test_defined_nested_objects_are_closed_and_arbitrary_mappings_remain_open(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = ToolRegistryEntry(
        cls=NestedToolArgs, posture="primary", annotations=None, exported_name="nested_test", title="Nested Test"
    )
    monkeypatch.setitem(tools.TOOL_REGISTRY, "nested_test", entry)
    tool = tool_description("nested_test", NestedToolArgs, entry)
    assert tool.input_schema["$defs"]["NestedInput"]["additionalProperties"] is False
    assert tool.input_schema["properties"]["metadata"]["additionalProperties"] is True
    valid = {"nested": {"count": 1, "at": "2026-10-10T00:00:00Z"}, "metadata": {"anything": {"other": True}}}
    args = tool_args(tool, **valid)
    assert isinstance(args, NestedToolArgs)
    assert isinstance(args.nested.at, datetime)
    assert args.metadata == valid["metadata"]
    for nested in (
        {**valid["nested"], "unknown": True},
        {**valid["nested"], "count": True},
        {**valid["nested"], "count": "1"},
        {**valid["nested"], "count": 1.0},
        {**valid["nested"], "at": 123},
        {**valid["nested"], "at": "yesterday"},
    ):
        with pytest.raises(ValueError):
            tool_args(tool, **{**valid, "nested": nested})


def test_workflows_prompt_uses_supported_search_scope() -> None:
    search_workflow = next(line for line in server._WORKFLOWS_PROMPT_TEXT.splitlines() if "SEARCH THEN READ" in line)
    search_guidance, read_guidance = search_workflow.split("Use list_messages")
    assert "exact_dialog_id" not in search_guidance
    assert 'dialog="N"' in search_guidance
    assert "exact_dialog_id=N" in read_guidance


def test_invalid_dynamic_mapping_keys_are_not_echoed(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = ToolRegistryEntry(
        cls=TypedMappingArgs, posture="primary", annotations=None, exported_name="mapping_test", title="Mapping Test"
    )
    monkeypatch.setitem(tools.TOOL_REGISTRY, "mapping_test", entry)
    tool = tool_description("mapping_test", TypedMappingArgs, entry)
    with pytest.raises(ValueError) as caught:
        tool_args(tool, metadata={"private-key": "private-value"})
    assert "private-key" not in str(caught.value)
    assert "private-value" not in str(caught.value)
