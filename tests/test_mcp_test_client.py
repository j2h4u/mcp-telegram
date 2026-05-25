from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

from devtools.mcp_client.cli import main, redact_script_output
from devtools.mcp_client.client import (
    McpClientError,
    StdioMcpClient,
    _assert_step_expectations,
    execute_script_steps,
    load_script_steps,
)


def _fake_server_command() -> list[str]:
    fixture_path = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"
    return [sys.executable, str(fixture_path)]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


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
        {
            "action": "list_tools",
            "expect": {
                "tool_names_include": ["Echo", "Fail"],
                "tool_expectations": {
                    "Echo": {
                        "inputSchema.properties.value.type": "string",
                    }
                },
            },
        },
        {
            "action": "call_tool",
            "name": "Echo",
            "arguments": {"value": "script"},
            "expect": {
                "is_error": False,
                "path_equals": {
                    "isError": False,
                    "content.0.type": "text",
                },
                "content_text_contains": ['{"value": "script"}'],
                "content_text_not_contains": ["missing"],
            },
        },
    ]

    async with StdioMcpClient(_fake_server_command()) as client:
        results = await execute_script_steps(client, steps)

    assert results[0]["action"] == "list_tools"
    assert results[0]["result"][0]["name"] == "Echo"
    assert results[1]["name"] == "Echo"
    assert results[1]["result"]["content"][0]["text"] == '{"value": "script"}'


@pytest.mark.asyncio
async def test_mcp_test_client_script_assertions_fail() -> None:
    steps = [
        {
            "action": "call_tool",
            "name": "Echo",
            "arguments": {"value": "script"},
            "expect": {
                "content_text_contains": ["not-there"],
            },
        },
    ]

    async with StdioMcpClient(_fake_server_command()) as client:
        with pytest.raises(McpClientError, match="missing expected text fragment"):
            await execute_script_steps(client, steps)


def test_mcp_test_client_script_asserts_structured_paths() -> None:
    result = {
        "content": [{"type": "text", "text": "1 dialog"}],
        "isError": False,
        "structuredContent": {
            "dialogs": [{"id": 123, "name": "Alice"}],
            "count": 1,
        },
    }

    _assert_step_expectations(
        index=1,
        action="call_tool",
        result=result,
        expect={
            "path_exists": ["structuredContent.dialogs.0.name"],
            "path_not_exists": ["structuredContent.dialogs.0.missing"],
            "path_nonempty": ["structuredContent.dialogs"],
        },
    )


@pytest.mark.asyncio
async def test_mcp_test_client_script_one_of_accepts_matching_branch() -> None:
    steps = [
        {
            "action": "call_tool",
            "name": "Echo",
            "arguments": {"value": "script"},
            "expect": {
                "one_of": [
                    {"content_text_contains": ["not-there"]},
                    {"is_error": False, "content_text_contains": ["script"]},
                ],
            },
        },
    ]

    async with StdioMcpClient(_fake_server_command()) as client:
        results = await execute_script_steps(client, steps)

    assert results[0]["result"]["isError"] is False


@pytest.mark.asyncio
async def test_mcp_test_client_script_one_of_fails_when_no_branch_matches() -> None:
    steps = [
        {
            "action": "call_tool",
            "name": "Echo",
            "arguments": {"value": "script"},
            "expect": {
                "one_of": [
                    {"content_text_contains": ["missing-a"]},
                    {"content_text_contains": ["missing-b"]},
                ],
            },
        },
    ]

    async with StdioMcpClient(_fake_server_command()) as client:
        with pytest.raises(McpClientError, match="did not match any expect.one_of branch"):
            await execute_script_steps(client, steps)


def test_mcp_test_client_redacts_printed_script_output(tmp_path, capsys) -> None:
    script_path = tmp_path / "script.json"
    script_path.write_text(
        """
        {
          "steps": [
            {
              "action": "call_tool",
              "name": "Echo",
              "arguments": {"value": "sensitive text"},
              "expect": {
                "is_error": false,
                "content_text_contains": ["sensitive text"]
              }
            }
          ]
        }
        """,
        encoding="utf-8",
    )

    exit_code = main([
        "script",
        "--redact",
        "--file",
        str(script_path),
        "--",
        *_fake_server_command(),
    ])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "[REDACTED " in captured.out
    assert "sensitive text" not in captured.out


def test_mcp_test_client_redacts_structured_content() -> None:
    payload = [
        {
            "action": "call_tool",
            "name": "get_inbox",
            "result": {
                "content": [{"type": "text", "text": "sensitive rendered text"}],
                "structuredContent": {
                    "dialogs": [
                        {
                            "name": "Sensitive Name",
                            "messages": [{"text": "Sensitive structured text"}],
                        }
                    ]
                },
                "isError": False,
            },
        }
    ]

    redacted = redact_script_output(payload)

    rendered = json.dumps(redacted, ensure_ascii=False)
    assert "[REDACTED " in rendered
    assert "[REDACTED structuredContent]" in rendered
    assert "sensitive rendered text" not in rendered
    assert "Sensitive Name" not in rendered
    assert "Sensitive structured text" not in rendered


def test_mcp_test_client_expands_env_placeholders(tmp_path, monkeypatch) -> None:
    script_path = tmp_path / "script.json"
    script_path.write_text(
        json.dumps(
            {
                "steps": [
                    {
                        "action": "call_tool",
                        "name": "trace_account_messages",
                        "arguments": {"account": "${MCP_TG_SMOKE_ACCOUNT}"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_TG_SMOKE_ACCOUNT", "12345")

    steps = load_script_steps(script_path)

    assert steps[0]["arguments"]["account"] == "12345"


def test_mcp_test_client_missing_env_placeholder_fails(tmp_path, monkeypatch) -> None:
    script_path = tmp_path / "script.json"
    script_path.write_text(
        json.dumps({"steps": [{"action": "call_tool", "name": "x", "arguments": {"account": "${MISSING_VAR}"}}]}),
        encoding="utf-8",
    )
    monkeypatch.delenv("MISSING_VAR", raising=False)

    with pytest.raises(ValueError, match="MISSING_VAR"):
        load_script_steps(script_path)


def test_smoke_scripts_use_snake_case_tool_names() -> None:
    exposed_pascal_case = re.compile(r"\b(?:Get|List|Search|Submit|Mark)[A-Z]\w*")
    for relative_path in (
        "devtools/mcp_client/smoke-no-daemon.json",
        "devtools/mcp_client/smoke-integration.json",
    ):
        text = (_repo_root() / relative_path).read_text(encoding="utf-8")
        assert exposed_pascal_case.search(text) is None


def test_no_daemon_smoke_expects_backend_errors() -> None:
    script = json.loads(
        (_repo_root() / "devtools/mcp_client/smoke-no-daemon.json").read_text(encoding="utf-8")
    )
    backend_tools = {
        "list_dialogs",
        "list_messages",
        "search_messages",
        "list_topics",
        "mark_dialog_for_sync",
        "get_sync_status",
        "get_entity_info",
        "get_inbox",
        "submit_feedback",
    }

    assert "get_dialog_stats" in script["steps"][0]["expect"]["tool_names_include"]
    for step in script["steps"]:
        if step.get("action") == "call_tool" and step.get("name") in backend_tools:
            assert step["expect"]["is_error"] is True
