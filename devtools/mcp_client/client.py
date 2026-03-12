from __future__ import annotations

import json
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client

DEFAULT_TIMEOUT_SECONDS = 10.0


class McpClientError(RuntimeError):
    """Raised when the external MCP server process or protocol misbehaves."""


class StdioMcpClient:
    """Tiny async wrapper around the official MCP stdio client transport."""

    def __init__(
        self,
        command: list[str],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        cwd: Path | str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must contain at least one program name")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._server = StdioServerParameters(
            command=command[0],
            args=command[1:],
            cwd=cwd,
            env=env,
        )
        self._timeout_seconds = timeout_seconds
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> StdioMcpClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._session is not None:
            return

        exit_stack = AsyncExitStack()
        try:
            read_stream, write_stream = await exit_stack.enter_async_context(stdio_client(self._server))
            session = await exit_stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except Exception as exc:
            await exit_stack.aclose()
            raise McpClientError(str(exc)) from exc

        self._exit_stack = exit_stack
        self._session = session

    async def stop(self) -> None:
        exit_stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if exit_stack is not None:
            await exit_stack.aclose()

    async def list_tools(self) -> list[dict[str, Any]]:
        session = self._require_session()
        try:
            result = await session.list_tools()
        except Exception as exc:
            raise McpClientError(str(exc)) from exc
        return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._require_session()
        try:
            result = await session.call_tool(
                name,
                arguments or {},
                read_timeout_seconds=timedelta(seconds=self._timeout_seconds),
            )
        except Exception as exc:
            raise McpClientError(str(exc)) from exc
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    def _require_session(self) -> ClientSession:
        session = self._session
        if session is None:
            raise McpClientError("client session is not initialized")
        return session


def load_script_steps(script_path: Path) -> list[dict[str, Any]]:
    """Load one JSON script file with MCP client steps."""
    payload = json.loads(script_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        steps = payload
    elif isinstance(payload, dict):
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("script JSON object must contain a list field named 'steps'")
        steps = raw_steps
    else:
        raise ValueError("script JSON must be a list or an object with a 'steps' field")

    normalized_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            raise ValueError(f"script step {index} must be an object")
        normalized_steps.append(step)
    return normalized_steps


async def execute_script_steps(client: StdioMcpClient, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run one list of client actions inside a single MCP session."""
    results: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        action = step.get("action")
        if action == "list_tools":
            result = await client.list_tools()
            _assert_step_expectations(index=index, action=action, result=result, expect=step.get("expect"))
            results.append({
                "step": index,
                "action": action,
                "result": result,
            })
            continue

        if action == "call_tool":
            name = step.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"script step {index} is missing string field 'name'")

            arguments = step.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"script step {index} field 'arguments' must be an object")

            result = await client.call_tool(name, arguments)
            _assert_step_expectations(index=index, action=action, result=result, expect=step.get("expect"))
            results.append({
                "step": index,
                "action": action,
                "name": name,
                "result": result,
            })
            continue

        raise ValueError(f"unsupported script action at step {index}: {action!r}")

    return results


def _assert_step_expectations(
    *,
    index: int,
    action: str,
    result: Any,
    expect: Any,
) -> None:
    if expect is None:
        return
    if not isinstance(expect, dict):
        raise ValueError(f"script step {index} field 'expect' must be an object")

    path_equals = expect.get("path_equals")
    if path_equals is not None:
        if not isinstance(path_equals, dict):
            raise ValueError(f"script step {index} field 'expect.path_equals' must be an object")
        for path, expected_value in path_equals.items():
            actual_value = _lookup_path(result, path)
            if actual_value != expected_value:
                raise McpClientError(
                    f"script step {index} expected path {path!r} to equal {expected_value!r}, got {actual_value!r}"
                )

    if action == "list_tools":
        _assert_list_tools_expectations(index=index, result=result, expect=expect)
    elif action == "call_tool":
        _assert_call_tool_expectations(index=index, result=result, expect=expect)


def _assert_list_tools_expectations(*, index: int, result: Any, expect: dict[str, Any]) -> None:
    if not isinstance(result, list):
        raise McpClientError(f"script step {index} list_tools result is not a list")

    tool_names_include = expect.get("tool_names_include")
    if tool_names_include is not None:
        if not isinstance(tool_names_include, list) or not all(isinstance(item, str) for item in tool_names_include):
            raise ValueError(f"script step {index} field 'expect.tool_names_include' must be a list of strings")
        tool_names = {tool.get("name") for tool in result if isinstance(tool, dict)}
        missing_names = [name for name in tool_names_include if name not in tool_names]
        if missing_names:
            raise McpClientError(f"script step {index} is missing tools: {missing_names}")

    tool_expectations = expect.get("tool_expectations")
    if tool_expectations is None:
        return
    if not isinstance(tool_expectations, dict):
        raise ValueError(f"script step {index} field 'expect.tool_expectations' must be an object")

    tools_by_name = {
        tool.get("name"): tool
        for tool in result
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    for tool_name, path_map in tool_expectations.items():
        tool_payload = tools_by_name.get(tool_name)
        if tool_payload is None:
            raise McpClientError(f"script step {index} expected tool {tool_name!r} to exist")
        if not isinstance(path_map, dict):
            raise ValueError(
                f"script step {index} field 'expect.tool_expectations.{tool_name}' must be an object"
            )
        for path, expected_value in path_map.items():
            actual_value = _lookup_path(tool_payload, path)
            if actual_value != expected_value:
                raise McpClientError(
                    f"script step {index} expected tool {tool_name!r} path {path!r} "
                    f"to equal {expected_value!r}, got {actual_value!r}"
                )


def _assert_call_tool_expectations(*, index: int, result: Any, expect: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise McpClientError(f"script step {index} call_tool result is not an object")

    expected_is_error = expect.get("is_error")
    if expected_is_error is not None:
        if not isinstance(expected_is_error, bool):
            raise ValueError(f"script step {index} field 'expect.is_error' must be a boolean")
        actual_is_error = result.get("isError")
        if actual_is_error != expected_is_error:
            raise McpClientError(
                f"script step {index} expected isError={expected_is_error!r}, got {actual_is_error!r}"
            )

    content_text = _extract_text_content(result)
    _assert_text_membership(
        index=index,
        field_name="content_text_contains",
        haystack=content_text,
        expected=expect.get("content_text_contains"),
        negate=False,
    )
    _assert_text_membership(
        index=index,
        field_name="content_text_not_contains",
        haystack=content_text,
        expected=expect.get("content_text_not_contains"),
        negate=True,
    )


def _assert_text_membership(
    *,
    index: int,
    field_name: str,
    haystack: str,
    expected: Any,
    negate: bool,
) -> None:
    if expected is None:
        return
    if not isinstance(expected, list) or not all(isinstance(item, str) for item in expected):
        raise ValueError(f"script step {index} field 'expect.{field_name}' must be a list of strings")

    for item in expected:
        contains = item in haystack
        if negate and contains:
            raise McpClientError(f"script step {index} unexpectedly contained text fragment: {item!r}")
        if not negate and not contains:
            raise McpClientError(f"script step {index} is missing expected text fragment: {item!r}")


def _extract_text_content(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


def _lookup_path(payload: Any, path: str) -> Any:
    current = payload
    for segment in path.split("."):
        if isinstance(current, list):
            try:
                index = int(segment)
            except ValueError as exc:
                raise McpClientError(f"cannot use non-numeric segment {segment!r} on list path {path!r}") from exc
            try:
                current = current[index]
            except IndexError as exc:
                raise McpClientError(f"list index {index} out of range for path {path!r}") from exc
            continue

        if isinstance(current, dict):
            if segment not in current:
                raise McpClientError(f"missing path segment {segment!r} in path {path!r}")
            current = current[segment]
            continue

        raise McpClientError(f"cannot descend into non-container value at segment {segment!r} for path {path!r}")

    return current
