from __future__ import annotations

import asyncio
import logging
import sys
import time
import typing as t
from collections.abc import Sequence
from functools import cache

from mcp.server import Server
from mcp.types import (
    EmbeddedResource,
    ImageContent,
    Prompt,
    Resource,
    ResourceTemplate,
    TextContent,
    Tool,
)

from . import tools

logger = logging.getLogger(__name__)
app = Server("mcp-telegram")
_MAX_ERROR_DETAIL_LENGTH = 160


@cache
def enumerate_available_tools() -> list[tuple[str, Tool]]:
    tools.verify_tool_registry()
    return [(name, tools.tool_description(cls)) for name, (cls, _posture) in tools.TOOL_REGISTRY.items()]


tool_by_name: dict[str, Tool] = dict(enumerate_available_tools())


def _safe_boundary_error_text(*, tool_name: str, stage: str, exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        detail = " ".join(detail.split())
    if not detail or "traceback" in detail.lower():
        detail = type(exc).__name__
    if len(detail) > _MAX_ERROR_DETAIL_LENGTH:
        detail = f"{detail[:_MAX_ERROR_DETAIL_LENGTH - 3]}..."

    if stage == "validation":
        action = "Check the tool arguments against the exported schema and retry."
        return f"Tool {tool_name} argument validation failed: {detail}. Action: {action}"

    action = "Retry the tool. If this persists, inspect the server logs for the underlying exception type."
    return f"Tool {tool_name} runtime execution failed: {detail}. Action: {action}"


@app.list_prompts()
async def list_prompts() -> list[Prompt]:
    """Return empty list — prompts not implemented."""
    return []


@app.list_resources()
async def list_resources() -> list[Resource]:
    """Return empty list — resources not implemented."""
    return []


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return list(tool_by_name.values())


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
    """Return empty list — resource templates not implemented."""
    return []


@app.progress_notification()
async def progress_notification(progress: str | int, p: float, s: float | None) -> None:
    """No-op handler required by MCP protocol."""


@app.call_tool()
async def call_tool(name: str, arguments: t.Any) -> Sequence[TextContent | ImageContent | EmbeddedResource]:  # noqa: ANN401
    """Handle tool calls for command line run."""

    if not isinstance(arguments, dict):
        raise TypeError("arguments must be dictionary")

    tool = tool_by_name.get(name)
    if not tool:
        raise ValueError(f"Unknown tool: {name}")

    t0 = time.monotonic()
    try:
        args = tools.tool_args(tool, **arguments)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception("call_tool[%s] validation failed after %.3fs", name, elapsed)
        raise RuntimeError(_safe_boundary_error_text(tool_name=name, stage="validation", exc=exc)) from exc

    try:
        result = await tools.tool_runner(args)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.exception("call_tool[%s] runtime failed after %.3fs", name, elapsed)
        raise RuntimeError(_safe_boundary_error_text(tool_name=name, stage="runtime", exc=exc)) from exc

    elapsed = time.monotonic() - t0
    logger.info("call_tool[%s] completed in %.3fs", name, elapsed)
    return result


async def run_mcp_server() -> None:
    # Import here to avoid issues with event loops
    from mcp.server.stdio import stdio_server

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    logger.info("MCP server starting — routing through daemon API")

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    asyncio.run(run_mcp_server())
