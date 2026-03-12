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
            results.append({
                "step": index,
                "action": action,
                "result": await client.list_tools(),
            })
            continue

        if action == "call_tool":
            name = step.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"script step {index} is missing string field 'name'")

            arguments = step.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError(f"script step {index} field 'arguments' must be an object")

            results.append({
                "step": index,
                "action": action,
                "name": name,
                "result": await client.call_tool(name, arguments),
            })
            continue

        raise ValueError(f"unsupported script action at step {index}: {action!r}")

    return results
