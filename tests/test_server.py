from __future__ import annotations

import inspect
import re
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import pytest
from mcp.types import CallToolResult, TextContent, Tool

from mcp_telegram import server
from mcp_telegram.tools._base import ToolRegistryEntry, ToolResult, tool_description
from mcp_telegram.tools.discovery import ListDialogs

INVENTORY_PATH = Path(__file__).parent / "fixtures" / "52-TOOL-OUTPUT-INVENTORY.md"


def _tool_input_schema(tool: Tool) -> dict[str, object]:
    return cast(dict[str, object], tool.inputSchema)


def _tool_output_schema(tool: Tool) -> dict[str, object]:
    assert tool.outputSchema is not None
    return cast(dict[str, object], tool.outputSchema)


class _HasContent(Protocol):
    content: list[object]


def _call_tool_text(result: object) -> str:
    content = cast(_HasContent, result).content
    assert content
    first_content = content[0]
    assert isinstance(first_content, TextContent)
    return first_content.text


def _call_tool_result(result: object) -> CallToolResult:
    assert hasattr(result, "content")
    assert hasattr(result, "isError")
    return cast(CallToolResult, result)


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        title=name.replace("_", " ").title(),
        description=f"{name} test tool",
        inputSchema={"type": "object", "properties": {}},
    )


def test_list_messages_reflection_exposes_shared_navigation_schema() -> None:
    tool = server.tool_by_name["list_messages"]
    properties = cast(dict[str, object], _tool_input_schema(tool)["properties"])
    required = cast(list[str], _tool_input_schema(tool).get("required", []))

    assert "navigation" in properties
    assert "exact_dialog_id" in properties
    assert "exact_topic_id" in properties
    assert "cursor" not in properties
    assert "from_beginning" not in properties
    assert "response_order" not in properties
    assert "reply_context_mode" not in properties
    navigation = cast(dict[str, object], properties["navigation"])
    exact_dialog_id = cast(dict[str, object], properties["exact_dialog_id"])
    exact_topic_id = cast(dict[str, object], properties["exact_topic_id"])
    assert navigation["type"] == "string"
    assert exact_dialog_id["type"] == "integer"
    assert exact_topic_id["type"] == "integer"
    assert '"latest"' in cast(str, navigation["description"])
    assert '"start"' in cast(str, navigation["description"])
    assert "already known" in cast(str, exact_dialog_id["description"])
    assert "Mutually exclusive with dialog" in cast(str, exact_dialog_id["description"])
    assert "full topic catalog" in cast(str, exact_topic_id["description"])
    assert "dialog" not in required


@pytest.mark.asyncio
async def test_call_tool_validation_rejects_conflicting_list_messages_selectors() -> None:
    result = _call_tool_result(await server.call_tool("list_messages", {"dialog": "Backend", "exact_dialog_id": 701}))

    assert result.isError is True
    message = _call_tool_text(result)
    assert "validation" in message.lower()
    assert "mutually exclusive" in message.lower()
    assert "exact_dialog_id" in message


def test_search_messages_reflection_exposes_shared_navigation_schema() -> None:
    tool = server.tool_by_name["search_messages"]
    properties = cast(dict[str, object], _tool_input_schema(tool)["properties"])

    assert "dialog" in properties
    assert "navigation" in properties
    assert "offset" not in properties
    assert "exact_dialog_id" not in properties
    dialog = cast(dict[str, object], properties["dialog"])
    navigation = cast(dict[str, object], properties["navigation"])
    assert dialog["type"] == "string"
    assert "exact numeric dialog id" in cast(str, dialog["description"])
    assert navigation["type"] == "string"
    assert "first search page" in cast(str, navigation["description"])
    assert "next_navigation" in cast(str, navigation["description"])


@pytest.mark.asyncio
async def test_call_tool_validation_failure_escaped_error_includes_actionable_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(server.tool_by_name, "list_dialogs", _tool("list_dialogs"))

    def _raise_validation_error(tool: Tool, **kwargs: object) -> object:
        raise ValueError("dialog must be a string")

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", _raise_validation_error)

    result = _call_tool_result(await server.call_tool("list_dialogs", {"dialog": 123}))

    assert result.isError is True
    message = _call_tool_text(result)
    assert "validation" in message.lower() or "argument" in message.lower()
    assert "dialog" in message.lower()
    assert "action:" in message.lower() or "retry" in message.lower() or "check" in message.lower()
    assert message != "Tool list_dialogs failed"


@pytest.mark.asyncio
async def test_call_tool_runtime_failure_escaped_error_includes_actionable_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(server.tool_by_name, "list_dialogs", _tool("list_dialogs"))
    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr(
        "mcp_telegram.server.tools.tool_runner",
        AsyncMock(side_effect=RuntimeError("telegram backend timed out")),
    )

    result = _call_tool_result(await server.call_tool("list_dialogs", {}))

    assert result.isError is True
    message = _call_tool_text(result)
    assert "runtime" in message.lower() or "execution" in message.lower()
    assert "timed out" in message.lower() or "timeout" in message.lower()
    assert "action:" in message.lower() or "retry" in message.lower() or "check" in message.lower()
    assert message != "Tool list_dialogs failed"


@pytest.mark.asyncio
async def test_call_tool_passthrough_recoverable_error_text_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(server.tool_by_name, "get_entity_info", _tool("get_entity_info"))

    expected = [
        TextContent(
            type="text",
            text="Could not fetch entity info for 'Iris' (boom).\nAction: Retry get_entity_info later.",
        )
    ]

    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr(
        "mcp_telegram.server.tools.tool_runner",
        AsyncMock(return_value=ToolResult(content=expected, is_error=True)),
    )

    result = _call_tool_result(await server.call_tool("get_entity_info", {"entity": "Iris"}))

    assert result.content == expected
    assert result.isError is True
    assert _call_tool_text(result) == expected[0].text
    assert "Action:" in _call_tool_text(result)
    assert "failed" not in _call_tool_text(result)


@pytest.mark.asyncio
async def test_call_tool_unknown_tool_control_contract() -> None:
    with pytest.raises(ValueError, match="Unknown tool: MissingTool"):
        await server.call_tool("MissingTool", {})


@pytest.mark.asyncio
async def test_call_tool_non_dict_arguments_control_contract() -> None:
    with pytest.raises(TypeError, match="arguments must be dictionary"):
        await server.call_tool("list_dialogs", [])


def test_posture_tags_are_not_reflected_in_descriptions() -> None:
    """Posture is internal metadata and must not consume agent-facing description budget."""
    for tool in server.tool_by_name.values():
        description = tool.description
        assert description is not None
        assert not description.startswith("[primary]"), f"{tool.name} leaks primary posture"
        assert not description.startswith("[secondary/helper]"), f"{tool.name} leaks helper posture"


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
        title = tool.title
        assert title is not None
        assert 1 <= len(title.split()) <= 3
        annotations = tool.annotations
        assert annotations is not None
        assert annotations.readOnlyHint is not None
        assert annotations.destructiveHint is not None
        assert annotations.idempotentHint is not None
        assert annotations.openWorldHint is not None
    for name, title in expected_titles.items():
        assert server.tool_by_name[name].title == title

    list_messages_annotations = server.tool_by_name["list_messages"].annotations
    mark_annotations = server.tool_by_name["mark_dialog_for_sync"].annotations
    submit_annotations = server.tool_by_name["submit_feedback"].annotations
    trace_annotations = server.tool_by_name["trace_account_messages"].annotations
    assert list_messages_annotations is not None
    assert mark_annotations is not None
    assert submit_annotations is not None
    assert trace_annotations is not None
    assert list_messages_annotations.readOnlyHint is True
    assert mark_annotations.readOnlyHint is False
    assert mark_annotations.idempotentHint is True
    assert submit_annotations.readOnlyHint is False
    assert submit_annotations.destructiveHint is False
    assert trace_annotations.readOnlyHint is False
    assert trace_annotations.destructiveHint is False
    assert trace_annotations.idempotentHint is True
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

    assert not missing_tools, f"{INVENTORY_PATH.name} is missing registered tool(s): {', '.join(missing_tools)}"


def test_phase_52_tool_output_inventory_marks_baseline_columns() -> None:
    inventory_text = INVENTORY_PATH.read_text(encoding="utf-8")

    assert "Current Phase 52 completion status:" in inventory_text
    assert "pre-implementation baseline" in inventory_text
    assert "Baseline outputSchema" in inventory_text
    assert "Baseline successful structuredContent" in inventory_text


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

    assert cast(dict[str, object], tool.inputSchema)["type"] == "object"
    assert tool.outputSchema == output_schema
    assert tool.title == "List Dialogs"


def test_http_server_defaults_to_loopback_bind() -> None:
    signature = inspect.signature(server.run_mcp_http_server)

    default = cast(object, signature.parameters["host"].default)
    assert default == "127.0.0.1"


def test_http_server_rejects_non_loopback_bind_without_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_TELEGRAM_HTTP_ALLOW_UNSAFE", raising=False)

    with pytest.raises(RuntimeError, match="Refusing to bind MCP HTTP transport"):
        server._assert_http_exposure_allowed("0.0.0.0")


def test_http_server_allows_non_loopback_bind_with_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_ALLOW_UNSAFE", "1")

    server._assert_http_exposure_allowed("0.0.0.0")


def test_http_transport_security_allows_loopback_and_configured_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_ALLOWED_HOSTS", "mcp-telegram:3100")
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_ALLOWED_ORIGINS", "http://gateway.local")

    hosts = server._http_allowed_hosts(host="127.0.0.1", port=3100)
    origins = server._http_allowed_origins()

    assert "127.0.0.1:*" in hosts
    assert "localhost:*" in hosts
    assert "mcp-telegram:3100" in hosts
    assert "http://localhost:*" in origins
    assert any(item.scheme == "http" and item.hostname == "gateway.local" for item in map(urlparse, origins))


def test_safe_boundary_error_text_validation_uses_sanitized_detail() -> None:
    text = server._safe_boundary_error_text(
        tool_name="list_dialogs",
        stage="validation",
        exc=ValueError("   dialog  \tis\n   blank   "),
    )

    assert text == (
        "Tool list_dialogs argument validation failed: dialog is blank. "
        "Action: Check the tool arguments against the exported schema and retry."
    )


def test_safe_boundary_error_text_runtime_falls_back_to_exception_type_when_detail_is_empty_or_traceback() -> None:
    empty = server._safe_boundary_error_text(tool_name="get_entity_info", stage="runtime", exc=RuntimeError(""))

    assert "Tool get_entity_info runtime execution failed: RuntimeError." in empty

    tb = server._safe_boundary_error_text(
        tool_name="search_messages", stage="runtime", exc=RuntimeError("Traceback: boom")
    )

    assert "Tool search_messages runtime execution failed: RuntimeError." in tb


def test_safe_boundary_error_text_truncates_verbose_error() -> None:
    long_error = "x" * 220
    text = server._safe_boundary_error_text(
        tool_name="list_messages",
        stage="runtime",
        exc=RuntimeError(long_error),
    )

    assert text.startswith("Tool list_messages runtime execution failed: ")
    assert "..." in text
    assert text.count("...") == 1


def test_normalize_bind_host_strips_brackets_and_lowercases() -> None:
    assert server._normalize_bind_host(" [::1] ") == "::1"
    assert server._normalize_bind_host("LOCALHOST") == "localhost"
    assert server._normalize_bind_host("127.0.0.1") == "127.0.0.1"


def test_is_loopback_http_host_recognizes_loopbacks_and_rejects_public_host() -> None:
    assert server._is_loopback_http_host("LOCALHOST") is True
    assert server._is_loopback_http_host("[::1]") is True
    assert server._is_loopback_http_host("::1") is True
    assert server._is_loopback_http_host("127.0.0.1") is True
    assert server._is_loopback_http_host("192.168.1.1") is False


def test_assert_http_exposure_allowed_allows_loopback_like_hosts_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MCP_TELEGRAM_HTTP_ALLOW_UNSAFE", raising=False)

    server._assert_http_exposure_allowed("127.0.0.1")
    server._assert_http_exposure_allowed("localhost")
    server._assert_http_exposure_allowed("[::1]")


def test_http_allowed_hosts_ignores_unsafebind_placeholders_and_dedupes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MCP_TELEGRAM_HTTP_ALLOWED_HOSTS", "0.0.0.0, 0.0.0.0, ::, mcp-telegram:3100")

    hosts = server._http_allowed_hosts(host="0.0.0.0", port=3100)

    assert "0.0.0.0" in hosts
    assert "::" in hosts
    assert "mcp-telegram:3100" in hosts
    assert "127.0.0.1" in hosts
    assert hosts.count("mcp-telegram:3100") == 1


def test_http_allowed_hosts_includes_ipv6_bind_host_and_port_variant() -> None:
    hosts = server._http_allowed_hosts(host="[::1]", port=4100)

    assert "[::1]" in hosts
    assert "[::1]:4100" in hosts
    assert "[::1]:*" in hosts


def test_list_prompts_resources_tools_and_progress_routes_exist() -> None:
    import asyncio
    from collections.abc import Awaitable, Callable

    async def runner() -> tuple[object, object, object, object]:
        list_prompts = cast(Callable[[], Awaitable[object]], server.list_prompts)
        list_resources = cast(Callable[[], Awaitable[object]], server.list_resources)
        list_tools = cast(Callable[[], Awaitable[object]], server.list_tools)
        list_resource_templates = cast(Callable[[], Awaitable[object]], server.list_resource_templates)
        prompts = await list_prompts()
        resources = await list_resources()
        tools = await list_tools()
        templates = await list_resource_templates()
        await server.progress_notification(0, 0.0, None, None)
        return prompts, resources, tools, templates

    prompts, resources, tools, templates = asyncio.run(runner())

    assert prompts == []
    assert resources == []
    assert isinstance(tools, list)
    assert templates == []


@pytest.mark.asyncio
async def test_run_mcp_server_invokes_stdio_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    calls = {"enter": 0, "exit": 0, "run": 0}

    @asynccontextmanager
    async def fake_stdio_server():
        calls["enter"] += 1
        yield object(), object()
        calls["exit"] += 1

    async def fake_run(*_args: object, **_kwargs: object) -> None:
        calls["run"] += 1

    monkeypatch.setattr("mcp.server.stdio.stdio_server", fake_stdio_server)
    monkeypatch.setattr(server.app, "run", fake_run)
    monkeypatch.setattr(server.app, "create_initialization_options", lambda: "INIT")
    monkeypatch.setattr("logging.basicConfig", lambda *args, **kwargs: None)
    monkeypatch.setattr("mcp_telegram.server._build_server_instructions", AsyncMock(return_value="Built"))

    await server.run_mcp_server()

    assert calls["enter"] == 1
    assert calls["exit"] == 1
    assert calls["run"] == 1
    assert server.app.instructions == "Built"


class _FakeTransportSecuritySettings:
    def __init__(self, captured: dict[str, object], **kwargs: object) -> None:
        captured["security"] = kwargs


class _FakeSessionManager:
    def __init__(self, captured: dict[str, object], app: object, security_settings: object) -> None:
        captured["session_manager"] = {"app": app, "security_settings": security_settings}
        self._captured = captured

    @asynccontextmanager
    async def run(self):
        self._captured["session_manager_entered"] = True
        yield
        self._captured["session_manager_exited"] = True


class _FakeRoute:
    def __init__(
        self,
        captured: dict[str, object],
        path: str,
        endpoint: object,
        methods: list[str] | None = None,
    ) -> None:
        routes = cast(list[tuple[str, str, list[str] | None]], captured.setdefault("routes", []))
        routes.append(("route", path, methods))


class _FakeMount:
    def __init__(self, captured: dict[str, object], path: str, app: object) -> None:
        routes = cast(list[tuple[str, str, object]], captured.setdefault("routes", []))
        routes.append(("mount", path, app))


class _FakeStarlette:
    def __init__(self, captured: dict[str, object], *, debug: bool, routes: list[object], lifespan: object) -> None:
        captured["starlette"] = {"debug": debug, "routes": routes, "lifespan": lifespan}


class _FakeConfig:
    def __init__(self, captured: dict[str, object], asgi_app: object, *args: object, **kwargs: object) -> None:
        captured["config"] = {
            "asgi_app": asgi_app,
            "host": kwargs["host"],
            "port": kwargs["port"],
            "log_level": kwargs["log_level"],
        }


class _FakeServer:
    def __init__(self, captured: dict[str, object], config: object) -> None:
        captured["server"] = config
        self._captured = captured

    async def serve(self) -> None:
        self._captured["serve"] = True


def _make_fake_uvicorn_server(captured: dict[str, object]):
    class FakeServer(_FakeServer):
        def __init__(self, config: object) -> None:
            super().__init__(captured, config)

    return FakeServer


async def _fake_build_server_instructions() -> str:
    return "Built"


def _fake_assert_exposure_allowed(captured: dict[str, object], host: str) -> None:
    captured["assert"] = host


@pytest.mark.asyncio
async def test_run_mcp_http_server_normalizes_mount_and_builds_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "mcp.server.transport_security.TransportSecuritySettings", partial(_FakeTransportSecuritySettings, captured)
    )
    monkeypatch.setattr(
        "mcp.server.streamable_http_manager.StreamableHTTPSessionManager", partial(_FakeSessionManager, captured)
    )
    monkeypatch.setattr("starlette.applications.Starlette", partial(_FakeStarlette, captured))
    monkeypatch.setattr("starlette.routing.Route", partial(_FakeRoute, captured))
    monkeypatch.setattr("starlette.routing.Mount", partial(_FakeMount, captured))
    monkeypatch.setattr("uvicorn.Config", partial(_FakeConfig, captured))
    monkeypatch.setattr("uvicorn.Server", _make_fake_uvicorn_server(captured))
    monkeypatch.setattr("logging.basicConfig", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_http_allowed_hosts", lambda host, port: [f"{host}:{port}"])
    monkeypatch.setattr(server, "_http_allowed_origins", lambda: ["https://example.com"])
    monkeypatch.setattr("mcp_telegram.server._build_server_instructions", _fake_build_server_instructions)
    monkeypatch.setattr(server, "_assert_http_exposure_allowed", partial(_fake_assert_exposure_allowed, captured))

    await server.run_mcp_http_server(host="127.0.0.1", port=4100, mount_path="mcp")

    assert captured["assert"] == "127.0.0.1"
    assert captured["security"] == {
        "enable_dns_rebinding_protection": True,
        "allowed_hosts": ["127.0.0.1:4100"],
        "allowed_origins": ["https://example.com"],
    }
    assert captured["server"]
    routes = cast(list[tuple[str, str]], captured.get("routes", []))
    assert routes[0][0] == "mount"
    assert routes[0][1] == "/mcp"
    assert routes[1][0] == "route"
    assert routes[1][1] == "/health"
    assert server.app.instructions == "Built"


def test_list_tools_exposes_list_dialogs_output_schema() -> None:
    tool = server.tool_by_name["list_dialogs"]

    assert tool.outputSchema is not None
    properties = cast(dict[str, object], _tool_output_schema(tool)["properties"])
    assert "dialogs" in properties
    assert "count" in cast(list[str], cast(dict[str, object], tool.outputSchema)["required"])


def test_all_registered_tools_declare_output_schema() -> None:
    schema_tools = {name for name, tool in server.tool_by_name.items() if tool.outputSchema is not None}

    assert schema_tools == set(server.tool_by_name)


def test_phase_52_agent_metadata_fields_are_in_output_schemas() -> None:
    def assert_nested_item_fields(
        output_schema: object,
        *,
        collection_name: str,
        required_fields: tuple[str, ...],
        property_fields: tuple[str, ...] = (),
    ) -> None:
        schema = cast(dict[str, object], output_schema)
        items = cast(
            dict[str, object],
            cast(dict[str, object], cast(dict[str, object], schema["properties"])[collection_name])["items"],
        )
        item_required = cast(list[str], items["required"])
        item_properties = cast(dict[str, object], items["properties"])
        for field in required_fields:
            assert field in item_required
        for field in property_fields:
            assert field in item_properties

    list_messages_schema = server.tool_by_name["list_messages"].outputSchema
    assert list_messages_schema is not None
    list_messages_dict = cast(dict[str, object], list_messages_schema)
    assert "presentation" in cast(list[str], list_messages_dict["required"])
    assert_nested_item_fields(
        list_messages_schema,
        collection_name="messages",
        required_fields=("reply_context_ref",),
        property_fields=("reply_context_ref",),
    )

    list_dialogs_schema = server.tool_by_name["list_dialogs"].outputSchema
    assert list_dialogs_schema is not None
    assert_nested_item_fields(
        list_dialogs_schema,
        collection_name="dialogs",
        required_fields=("draft_content",),
        property_fields=("draft_content",),
    )

    list_topics_schema = server.tool_by_name["list_topics"].outputSchema
    assert list_topics_schema is not None
    assert_nested_item_fields(
        list_topics_schema,
        collection_name="topics",
        required_fields=("title_content",),
        property_fields=("title_content",),
    )

    sync_alerts_schema = server.tool_by_name["get_sync_alerts"].outputSchema
    assert sync_alerts_schema is not None
    assert_nested_item_fields(
        sync_alerts_schema,
        collection_name="alerts",
        required_fields=("kind", "message_id", "deleted_at", "version", "edit_date", "access_lost_at"),
    )


def test_list_tools_exposes_account_trace_schema_and_title() -> None:
    tool = server.tool_by_name["trace_account_messages"]

    assert tool.title == "Account Trace"
    assert tool.outputSchema is not None
    output_schema = _tool_output_schema(tool)
    assert "coverage" in cast(list[str], output_schema["required"])
    assert "result_count_semantics" in cast(list[str], output_schema["required"])
    properties = cast(dict[str, object], output_schema["properties"])
    assert "preview" in properties
    assert "warnings" in properties
    assert "limits" in properties
    assert "navigation" in properties
    provenance = cast(dict[str, object], properties["provenance"])
    assert "coverage_bounds" in cast(dict[str, object], provenance["properties"])
    groups_item = cast(dict[str, object], cast(dict[str, object], properties["groups"])["items"])
    evidence_item = cast(dict[str, object], cast(dict[str, object], groups_item["properties"])["evidence"])["items"]
    evidence_properties = cast(dict[str, object], cast(dict[str, object], evidence_item)["properties"])
    assert "authorship_basis" in evidence_properties
    assert "content" in evidence_properties
    assert cast(dict[str, object], _tool_input_schema(tool)["properties"])["exact_topic_id"] is not None
    exact_topic_id = cast(
        dict[str, object], cast(dict[str, object], _tool_input_schema(tool)["properties"])["exact_topic_id"]
    )
    assert exact_topic_id["type"] == "integer"


def test_list_tools_exposes_feedback_and_entity_info_output_schemas() -> None:
    feedback_tool = server.tool_by_name["submit_feedback"]
    entity_tool = server.tool_by_name["get_entity_info"]

    assert feedback_tool.outputSchema is not None
    feedback_schema = cast(dict[str, object], feedback_tool.outputSchema)
    entity_schema = cast(dict[str, object], entity_tool.outputSchema)
    assert "accepted" in cast(list[str], feedback_schema["required"])
    assert "tracking_id" in cast(list[str], feedback_schema["required"])
    assert "type_specific" in cast(list[str], entity_schema["required"])
    assert "content_fields" in cast(list[str], entity_schema["required"])


@pytest.mark.asyncio
async def test_call_tool_returns_structuredContent_with_empty_success_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(server.tool_by_name, "list_dialogs", _tool("list_dialogs"))
    monkeypatch.setattr("mcp_telegram.server.tools.tool_args", lambda tool, **kwargs: object())
    monkeypatch.setattr(
        "mcp_telegram.server.tools.tool_runner",
        AsyncMock(
            return_value=ToolResult(
                content=[TextContent(type="text", text="legacy success preview")],
                structured_content={"dialogs": [{"id": 1, "name": "Alice"}], "count": 1},
            )
        ),
    )

    result = _call_tool_result(await server.call_tool("list_dialogs", {}))

    assert result.isError is False
    assert result.structuredContent == {"dialogs": [{"id": 1, "name": "Alice"}], "count": 1}
    assert result.content == []


@pytest.mark.asyncio
async def test_call_tool_validation_rejects_trace_topic_without_dialog_scope() -> None:
    result = _call_tool_result(
        await server.call_tool("trace_account_messages", {"account": "@alice", "exact_topic_id": 7})
    )

    assert result.isError is True
    assert "exact_topic_id requires" in _call_tool_text(result)


@pytest.mark.asyncio
async def test_server_instructions_mention_account_trace(monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.asyncio
async def test_server_instructions_describe_structured_only_response_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def get_me(self) -> dict:
            return {"ok": False}

    @asynccontextmanager
    async def _conn_cm():
        yield _Conn()

    monkeypatch.setattr("mcp_telegram.daemon_client.daemon_connection", _conn_cm)

    instructions = await server._build_server_instructions()
    normalized = instructions.lower()

    assert "structuredContent" in instructions
    assert "structured-only" in normalized
    assert "content may be empty" in normalized
    assert "iserror=true" in normalized
    assert "untrusted content" in normalized


@pytest.mark.asyncio
async def test_server_instructions_describe_identity_model(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Conn:
        async def get_me(self) -> dict:
            return {"ok": False}

    @asynccontextmanager
    async def _conn_cm():
        yield _Conn()

    monkeypatch.setattr("mcp_telegram.daemon_client.daemon_connection", _conn_cm)

    instructions = await server._build_server_instructions()

    assert "Identity model" in instructions
    assert "out=true" in instructions
    assert "sender_id" in instructions
    assert "effective_sender_id" in instructions


@pytest.mark.asyncio
async def test_server_instructions_successfully_enriches_account_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Conn:
        async def get_me(self) -> dict:
            return {
                "ok": True,
                "data": {
                    "id": 777,
                    "first_name": "Ilya",
                    "last_name": "Petrov",
                    "username": "i_petrov",
                },
            }

    @asynccontextmanager
    async def _conn_cm():
        yield _Conn()

    monkeypatch.setattr("mcp_telegram.daemon_client.daemon_connection", _conn_cm)

    instructions = await server._build_server_instructions()

    assert 'Connected account: id=777, name="Ilya Petrov", @i_petrov.' in instructions


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


def test_primary_tools_have_core_read_search_schema() -> None:
    """Primary read and search tools expose the direct-access schema patterns from Phase 17."""
    # list_messages: must have exact_dialog_id for direct reads
    list_messages = server.tool_by_name["list_messages"]
    lm_props = cast(dict[str, object], _tool_input_schema(list_messages)["properties"])
    assert "exact_dialog_id" in lm_props, "list_messages missing exact_dialog_id for direct dialog access"
    assert "exact_topic_id" in lm_props, "list_messages missing exact_topic_id for direct topic access"
    assert "navigation" in lm_props, "list_messages missing shared navigation field"

    # search_messages: must keep dialog + query shape for direct scoping
    search_messages = server.tool_by_name["search_messages"]
    sm_props = cast(dict[str, object], _tool_input_schema(search_messages)["properties"])
    assert "dialog" in sm_props, "search_messages missing dialog for exact numeric ID pattern"
    assert "query" in sm_props, "search_messages missing query"
    assert "navigation" in sm_props, "search_messages missing shared navigation field"

    # get_entity_info: must have entity field for universal entity lookup
    get_entity_info = server.tool_by_name["get_entity_info"]
    gei_props = cast(dict[str, object], _tool_input_schema(get_entity_info)["properties"])
    assert "entity" in gei_props, "get_entity_info missing entity field for direct lookup"

    trace_account = server.tool_by_name["trace_account_messages"]
    trace_props = cast(dict[str, object], _tool_input_schema(trace_account)["properties"])
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

        # Posture is internal metadata; agent-facing descriptions stay natural.
        assert not tool.description.startswith("[secondary/helper]"), (
            f"{tool_name} leaks helper posture into its description"
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
async def test_list_messages_tool_archived_warning_with_coverage(monkeypatch: pytest.MonkeyPatch):
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

    result = _call_tool_result(await server.call_tool("list_messages", {"exact_dialog_id": 123}))
    assert result.content == []
    payload = cast(dict[str, object], result.structuredContent)
    warning = cast(dict[str, object], cast(list[object], payload["warnings"])[0])
    message = cast(str, warning["message"])
    assert warning["kind"] == "archived_dialog"
    assert "archive" in message.lower()
    assert "2023-11-14" in message
    assert "80%" in message


@pytest.mark.asyncio
async def test_list_messages_tool_archived_warning_unknown_coverage(monkeypatch: pytest.MonkeyPatch):
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

    result = _call_tool_result(await server.call_tool("list_messages", {"exact_dialog_id": 123}))
    assert result.content == []
    payload = cast(dict[str, object], result.structuredContent)
    warning = cast(dict[str, object], cast(list[object], payload["warnings"])[0])
    message = cast(str, warning["message"])
    assert "150 messages archived locally" in message


@pytest.mark.asyncio
async def test_list_messages_tool_uses_last_synced_at_not_access_lost_at(
    monkeypatch: pytest.MonkeyPatch,
):
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

    result = _call_tool_result(await server.call_tool("list_messages", {"exact_dialog_id": 123}))
    payload = cast(dict[str, object], result.structuredContent)
    warning = cast(dict[str, object], cast(list[object], payload["warnings"])[0])
    # Must show 2023-11-14 (last_synced_at), NOT 2024-01-01 (access_lost_at)
    message = cast(str, warning["message"])
    assert "2023-11-14" in message
    assert "2024-01-01" not in message
