from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import cast

import pytest

from devtools.mcp_client.cli import main, redact_script_output
from devtools.mcp_client.client import (
    McpClientError,
    StdioMcpClient,
    _assert_step_expectations,
    execute_script_steps,
    load_script_steps,
)
from mcp_telegram import server


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


def test_mcp_test_client_redacts_printed_script_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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

    exit_code = main(
        [
            "script",
            "--redact",
            "--file",
            str(script_path),
            "--",
            *_fake_server_command(),
        ]
    )
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

    redacted = cast(list[dict[str, object]], redact_script_output(payload))

    rendered = json.dumps(redacted, ensure_ascii=False)
    assert "[REDACTED " in rendered
    assert "[REDACTED structuredContent]" in rendered
    assert "sensitive rendered text" not in rendered
    assert "Sensitive Name" not in rendered
    assert "Sensitive structured text" not in rendered


def test_mcp_test_client_expands_env_placeholders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_mcp_test_client_missing_env_placeholder_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
        "devtools/mcp_client/smoke-account-trace.json",
    ):
        text = (_repo_root() / relative_path).read_text(encoding="utf-8")
        assert exposed_pascal_case.search(text) is None


def test_no_daemon_smoke_expects_all_registered_tools_and_output_schemas() -> None:
    script = cast(
        dict[str, object],
        json.loads((_repo_root() / "devtools/mcp_client/smoke-no-daemon.json").read_text(encoding="utf-8")),
    )
    steps = cast(list[dict[str, object]], script["steps"])
    list_tools_expect = cast(dict[str, object], steps[0]["expect"])
    registered_tools = set(server.tool_by_name)

    assert set(cast(list[str], list_tools_expect["tool_names_include"])) == registered_tools
    tool_expectations = cast(dict[str, dict[str, object]], list_tools_expect["tool_expectations"])
    assert set(tool_expectations) == registered_tools
    for tool_name, path_map in tool_expectations.items():
        assert path_map["outputSchema.type"] == "object", tool_name


def _expectation_branches(expect: dict[str, object]) -> list[dict[str, object]]:
    one_of = expect.get("one_of")
    if one_of is None:
        return [expect]
    branches = cast(list[object], one_of)
    return [branch for branch in branches if isinstance(branch, dict)]


def test_successful_smoke_steps_assert_structured_content_paths_not_text_parsing() -> None:
    for relative_path in (
        "devtools/mcp_client/smoke-no-daemon.json",
        "devtools/mcp_client/smoke-integration.json",
        "devtools/mcp_client/smoke-account-trace.json",
    ):
        script = cast(dict[str, object], json.loads((_repo_root() / relative_path).read_text(encoding="utf-8")))
        for step in cast(list[dict[str, object]], script["steps"]):
            if step.get("action") != "call_tool":
                continue
            for expect in _expectation_branches(cast(dict[str, object], step.get("expect", {}))):
                if expect.get("is_error") is not False:
                    continue
                structured_paths = [
                    path
                    for key in ("path_exists", "path_nonempty")
                    for path in cast(list[object], expect.get(key, []))
                    if isinstance(path, str)
                ]
                assert any(path.startswith("structuredContent") for path in structured_paths), (
                    relative_path,
                    step.get("name"),
                )
                assert "content_text_contains" not in expect, (relative_path, step.get("name"))


def test_no_daemon_smoke_expects_backend_errors() -> None:
    script = cast(
        dict[str, object],
        json.loads((_repo_root() / "devtools/mcp_client/smoke-no-daemon.json").read_text(encoding="utf-8")),
    )
    backend_tools = set(server.tool_by_name)

    steps = cast(list[dict[str, object]], script["steps"])
    assert "get_dialog_stats" in cast(list[str], cast(dict[str, object], steps[0]["expect"])["tool_names_include"])
    for step in steps:
        if step.get("action") == "call_tool" and step.get("name") in backend_tools:
            assert cast(dict[str, object], step["expect"])["is_error"] is True
