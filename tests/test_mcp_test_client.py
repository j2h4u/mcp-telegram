from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast

import pytest
from devtools.mcp_client.cli import print_json, redact_script_output
from devtools.mcp_client.client import (
    _assert_step_expectations,
    load_script_steps,
)

from mcp_telegram import server


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_mcp_test_client_prints_utf8_without_json_ascii_escapes(capsys: pytest.CaptureFixture[str]) -> None:
    print_json({"text": "Привет"}, compact=False)

    captured = capsys.readouterr()
    assert "Привет" in captured.out
    assert "\\u041f" not in captured.out


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


def test_integration_smoke_calls_only_registered_read_only_tools() -> None:
    script = cast(
        dict[str, object],
        json.loads((_repo_root() / "devtools/mcp_client/smoke-integration.json").read_text(encoding="utf-8")),
    )

    for step in cast(list[dict[str, object]], script["steps"]):
        if step.get("action") != "call_tool":
            continue
        tool_name = step.get("name")
        assert isinstance(tool_name, str)
        tool = server.tool_by_name.get(tool_name)
        assert tool is not None, tool_name
        assert tool.annotations is not None, tool_name
        assert tool.annotations.read_only_hint is True, tool_name


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
