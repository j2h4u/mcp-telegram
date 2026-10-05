from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import time
from collections.abc import Awaitable
from contextlib import AsyncExitStack
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

DEFAULT_TIMEOUT_SECONDS = 30.0
SLOW_TOOL_CALL_WARNING_MS = 1000.0
_ENV_PLACEHOLDER_RE = re.compile(r"^\$\{([A-Z_][A-Z0-9_]*)\}$")
logger = logging.getLogger(__name__)
_tool_call_started_at: ContextVar[float | None] = ContextVar("tool_call_started_at", default=None)


class _TimedClientSession(ClientSession):
    """Expose when a tool result reaches SDK output-schema validation."""

    async def validate_tool_result(self, name: str, result: types.CallToolResult) -> None:
        call_started = _tool_call_started_at.get()
        wire_elapsed_ms = (time.perf_counter() - call_started) * 1000 if call_started is not None else None
        wire_elapsed = f"{wire_elapsed_ms:.1f}" if wire_elapsed_ms is not None else "unknown"
        wire_logger = (
            logger.warning
            if wire_elapsed_ms is not None and wire_elapsed_ms >= SLOW_TOOL_CALL_WARNING_MS
            else logger.debug
        )
        wire_logger("mcp_tool_result_received tool=%s wire_elapsed_ms=%s", name, wire_elapsed)
        started = time.perf_counter()
        outcome = "incomplete"
        try:
            await super().validate_tool_result(name, result)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        else:
            outcome = "success"
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            completion_logger = (
                logger.warning if outcome != "success" or elapsed_ms >= SLOW_TOOL_CALL_WARNING_MS else logger.debug
            )
            completion_logger(
                "mcp_tool_result_validation_complete tool=%s elapsed_ms=%.1f outcome=%s",
                name,
                elapsed_ms,
                outcome,
            )


class McpClientError(RuntimeError):
    """Raised when the external MCP server process or protocol misbehaves."""


class McpTestClient(Protocol):
    """Protocol implemented by MCP test clients."""

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def list_prompts(self) -> list[dict[str, Any]]: ...

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]: ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]: ...


class HttpMcpClient:
    """Tiny async wrapper around the official MCP Streamable HTTP client transport."""

    def __init__(
        self,
        url: str,
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not url:
            raise ValueError("url must not be empty")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive")

        self._url = url
        self._timeout_seconds = timeout_seconds
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> HttpMcpClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    async def start(self) -> None:
        if self._session is not None:
            return

        exit_stack = AsyncExitStack()
        try:
            # Keep SDK task-group entry and exit in this task, including on timeout.
            async with asyncio.timeout(self._timeout_seconds):
                read_stream, write_stream = await exit_stack.enter_async_context(streamable_http_client(self._url))
                session = await exit_stack.enter_async_context(
                    _TimedClientSession(read_stream, write_stream, read_timeout_seconds=self._timeout_seconds)
                )
                await session.initialize()
        except BaseException as exc:
            try:
                await self._request("cleanup", exit_stack.aclose())
            except McpClientError as cleanup_exc:
                exc.add_note(str(cleanup_exc))
            if isinstance(exc, TimeoutError):
                raise McpClientError(f"MCP initialization timed out after {self._timeout_seconds:g}s") from exc
            if isinstance(exc, Exception):
                raise McpClientError(str(exc)) from exc
            raise

        self._exit_stack = exit_stack
        self._session = session

    async def stop(self) -> None:
        exit_stack = self._exit_stack
        self._exit_stack = None
        self._session = None
        if exit_stack is not None:
            await self._request("cleanup", exit_stack.aclose())

    async def list_tools(self) -> list[dict[str, Any]]:
        session = self._require_session()
        result = await self._request("tools/list", session.list_tools())
        return [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in result.tools]

    async def list_prompts(self) -> list[dict[str, Any]]:
        session = self._require_session()
        result = await self._request("prompts/list", session.list_prompts())
        return [prompt.model_dump(mode="json", by_alias=True, exclude_none=True) for prompt in result.prompts]

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        session = self._require_session()
        result = await self._request("prompts/get", session.get_prompt(name, arguments))
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._require_session()
        token = _tool_call_started_at.set(time.perf_counter())
        try:
            result = await self._request(
                "tools/call",
                session.call_tool(
                    name,
                    arguments or {},
                    read_timeout_seconds=self._timeout_seconds,
                ),
            )
        finally:
            _tool_call_started_at.reset(token)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def _request[T](self, operation: str, request: Awaitable[T]) -> T:
        try:
            async with asyncio.timeout(self._timeout_seconds):
                return await request
        except TimeoutError as exc:
            raise McpClientError(f"MCP {operation} timed out after {self._timeout_seconds:g}s") from exc
        except Exception as exc:
            raise McpClientError(str(exc)) from exc

    def _require_session(self) -> ClientSession:
        session = self._session
        if session is None:
            raise McpClientError("client session is not initialized")
        return session


def load_script_steps(script_path: Path) -> list[dict[str, Any]]:
    """Load one JSON script file with MCP client steps."""
    payload = _expand_script_env(json.loads(script_path.read_text(encoding="utf-8")))
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


def _expand_script_env(value: Any) -> Any:
    if isinstance(value, str):
        match = _ENV_PLACEHOLDER_RE.match(value)
        if match is None:
            return value
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"script requires environment variable {name}")
        return os.environ[name]
    if isinstance(value, list):
        return [_expand_script_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_script_env(item) for key, item in value.items()}
    return value


async def execute_script_steps(client: McpTestClient, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run one list of client actions inside a single MCP session."""
    results: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        action = step.get("action")
        if action == "list_tools":
            result = await client.list_tools()
            _assert_step_expectations(index=index, action=action, result=result, expect=step.get("expect"))
            results.append(
                {
                    "step": index,
                    "action": action,
                    "result": result,
                }
            )
            continue

        if action == "list_prompts":
            result = await client.list_prompts()
            _assert_step_expectations(index=index, action=action, result=result, expect=step.get("expect"))
            results.append(
                {
                    "step": index,
                    "action": action,
                    "result": result,
                }
            )
            continue

        if action == "get_prompt":
            name = step.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"script step {index} is missing string field 'name'")

            arguments = step.get("arguments")
            if arguments is not None and (
                not isinstance(arguments, dict)
                or not all(isinstance(k, str) and isinstance(v, str) for k, v in arguments.items())
            ):
                raise ValueError(f"script step {index} field 'arguments' must be an object with string values")

            prompt_result = await client.get_prompt(name, arguments)
            _assert_step_expectations(index=index, action=action, result=prompt_result, expect=step.get("expect"))
            results.append(
                {
                    "step": index,
                    "action": action,
                    "name": name,
                    "result": prompt_result,
                }
            )
            continue

        if action == "call_tool":
            name = step.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"script step {index} is missing string field 'name'")

            arguments = step.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"script step {index} field 'arguments' must be an object")

            tool_result = await client.call_tool(name, arguments)
            _assert_step_expectations(index=index, action=action, result=tool_result, expect=step.get("expect"))
            results.append(
                {
                    "step": index,
                    "action": action,
                    "name": name,
                    "result": tool_result,
                }
            )
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

    one_of = expect.get("one_of")
    if one_of is not None:
        if not isinstance(one_of, list) or not all(isinstance(item, dict) for item in one_of):
            raise ValueError(f"script step {index} field 'expect.one_of' must be a list of objects")
        errors: list[str] = []
        for candidate in one_of:
            try:
                _assert_step_expectations(index=index, action=action, result=result, expect=candidate)
                return
            except McpClientError as exc:
                errors.append(str(exc))
        raise McpClientError(f"script step {index} did not match any expect.one_of branch: {'; '.join(errors)}")

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

    _assert_path_exists(index=index, result=result, expected=expect.get("path_exists"), should_exist=True)
    _assert_path_exists(index=index, result=result, expected=expect.get("path_not_exists"), should_exist=False)
    _assert_path_nonempty(index=index, result=result, expected=expect.get("path_nonempty"))

    action_expectations = {
        "list_tools": _assert_list_tools_expectations,
        "list_prompts": _assert_list_prompts_expectations,
        "call_tool": _assert_call_tool_expectations,
        "get_prompt": _assert_get_prompt_expectations,
    }
    checker = action_expectations.get(action)
    if checker is not None:
        checker(index=index, result=result, expect=expect)


def _assert_list_prompts_expectations(*, index: int, result: Any, expect: dict[str, Any]) -> None:
    if not isinstance(result, list):
        raise McpClientError(f"script step {index} list_prompts result is not a list")

    prompt_names_include = expect.get("prompt_names_include")
    if prompt_names_include is None:
        return
    if not isinstance(prompt_names_include, list) or not all(isinstance(item, str) for item in prompt_names_include):
        raise ValueError(f"script step {index} field 'expect.prompt_names_include' must be a list of strings")
    prompt_names = {prompt.get("name") for prompt in result if isinstance(prompt, dict)}
    missing_names = [name for name in prompt_names_include if name not in prompt_names]
    if missing_names:
        raise McpClientError(f"script step {index} is missing prompts: {missing_names}")


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
        tool.get("name"): tool for tool in result if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    for tool_name, path_map in tool_expectations.items():
        tool_payload = tools_by_name.get(tool_name)
        if tool_payload is None:
            raise McpClientError(f"script step {index} expected tool {tool_name!r} to exist")
        if not isinstance(path_map, dict):
            raise ValueError(f"script step {index} field 'expect.tool_expectations.{tool_name}' must be an object")
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
            raise McpClientError(f"script step {index} expected isError={expected_is_error!r}, got {actual_is_error!r}")

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


def _assert_get_prompt_expectations(*, index: int, result: Any, expect: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise McpClientError(f"script step {index} get_prompt result is not an object")

    prompt_text = _extract_prompt_text(result)
    _assert_text_membership(
        index=index,
        field_name="prompt_text_contains",
        haystack=prompt_text,
        expected=expect.get("prompt_text_contains"),
        negate=False,
    )
    _assert_text_membership(
        index=index,
        field_name="prompt_text_not_contains",
        haystack=prompt_text,
        expected=expect.get("prompt_text_not_contains"),
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


def _assert_path_list(index: int, field_name: str, expected: Any) -> list[str] | None:
    if expected is None:
        return None
    if not isinstance(expected, list) or not all(isinstance(item, str) and item for item in expected):
        raise ValueError(f"script step {index} field 'expect.{field_name}' must be a list of non-empty strings")
    return expected


def _assert_path_exists(*, index: int, result: Any, expected: Any, should_exist: bool) -> None:
    field_name = "path_exists" if should_exist else "path_not_exists"
    paths = _assert_path_list(index, field_name, expected)
    if paths is None:
        return

    for path in paths:
        found, _value, _error = _try_lookup_path(result, path)
        if should_exist and not found:
            raise McpClientError(f"script step {index} expected path {path!r} to exist")
        if not should_exist and found:
            raise McpClientError(f"script step {index} expected path {path!r} not to exist")


def _assert_path_nonempty(*, index: int, result: Any, expected: Any) -> None:
    paths = _assert_path_list(index, "path_nonempty", expected)
    if paths is None:
        return

    for path in paths:
        found, value, _error = _try_lookup_path(result, path)
        if not found:
            raise McpClientError(f"script step {index} expected path {path!r} to exist and be non-empty")
        if not _is_nonempty_path_value(value):
            raise McpClientError(f"script step {index} expected path {path!r} to be non-empty")


def _is_nonempty_path_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str | list | dict | tuple | set):
        return bool(value)
    return True


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


def _extract_prompt_text(result: dict[str, Any]) -> str:
    messages = result.get("messages")
    if not isinstance(messages, list):
        return ""
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, dict):
            continue
        text = content.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def _lookup_path(payload: Any, path: str) -> Any:
    found, value, error = _try_lookup_path(payload, path)
    if found:
        return value
    raise McpClientError(error or f"missing path {path!r}")


def _try_lookup_path(payload: Any, path: str) -> tuple[bool, Any, str | None]:
    current: Any = payload
    for segment in path.split("."):
        if isinstance(current, list):
            if not segment.isdecimal():
                return False, None, f"cannot use non-numeric segment {segment!r} on list path {path!r}"
            index = int(segment)
            if index >= len(current):
                return False, None, f"list index {index} out of range for path {path!r}"
            current = current[index]
            continue

        if isinstance(current, dict):
            if segment not in current:
                return False, None, f"missing path segment {segment!r} in path {path!r}"
            current = current[segment]
            continue

        return False, None, f"cannot descend into non-container value at segment {segment!r} for path {path!r}"

    return True, current, None
