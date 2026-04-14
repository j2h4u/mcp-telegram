"""MCP server entrypoint — tool registration, request dispatch, stdio transport.

Wires tool_runner (singledispatch) to the MCP Server, tracks per-request IDs
via _request_ids ContextVar for cross-process log correlation, and runs the
stdio transport loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
import typing as t
from collections.abc import Sequence
from functools import cache

from mcp.server import Server

from .daemon_client import _request_ids
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
    return [(name, tools.tool_description(cls)) for name, (cls, _posture, _annotations) in tools.TOOL_REGISTRY.items()]


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
    return []


@app.list_resources()
async def list_resources() -> list[Resource]:
    return []


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available tools."""
    return list(tool_by_name.values())


@app.list_resource_templates()
async def list_resource_templates() -> list[ResourceTemplate]:
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
    rids: list[str] = []
    token = _request_ids.set(rids)
    try:
        try:
            args = tools.tool_args(tool, **arguments)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception("call_tool[%s] validation_failed after %.3fs", name, elapsed)
            raise RuntimeError(_safe_boundary_error_text(tool_name=name, stage="validation", exc=exc)) from exc

        try:
            result = await tools.tool_runner(args)
        except Exception as exc:
            elapsed = time.monotonic() - t0
            logger.exception("call_tool[%s] runtime failed after %.3fs", name, elapsed)
            raise RuntimeError(_safe_boundary_error_text(tool_name=name, stage="runtime", exc=exc)) from exc

        elapsed = time.monotonic() - t0
        rid_str = ",".join(rids) if rids else "-"
        logger.info("call_tool[%s] completed in %.3fs rids=%s", name, elapsed, rid_str)
        return result
    finally:
        _request_ids.reset(token)


async def _build_server_instructions() -> str:
    """Fetch account info from daemon and build server instructions string.

    Falls back to a generic message if the daemon is unavailable — the
    client can still use GetMyAccount explicitly.
    """
    from .daemon_client import daemon_connection, DaemonNotRunningError

    base = (
        "Read-only access to a Telegram account's message history via a local sync cache.\n\n"
        "Key workflows:\n"
        "- SEARCH THEN READ: Use SearchMessages (omit dialog= for global, add dialog= to scope) "
        "to find messages. Results include msg_id: anchors. "
        "Use ListMessages(exact_dialog_id=N, anchor_message_id=M) to read context around any hit.\n"
        "- BROWSE: Use ListMessages with navigation=\"newest\"/\"oldest\" "
        "or a next_navigation token from a previous response. "
        "To read an entire channel or chat: call ListMessages repeatedly, passing the next_navigation "
        "token from each response into the next call. Continue until next_navigation is absent. "
        "Do NOT use WebFetch or web scraping for Telegram content — use these tools instead.\n"
        "- T.ME LINKS: Pass https://t.me/username links directly as dialog= — they are resolved "
        "automatically. For message links (t.me/channel/123), use the username part as dialog.\n"
        "- FIND DIALOG IDS: Use ListDialogs to get exact numeric dialog ids for direct reads.\n"
        "- SYNC STATUS: Only synced dialogs support SearchMessages and anchor-based reading. "
        "Plain ListMessages browsing works on any dialog without syncing. "
        "Use GetSyncStatus / GetSyncAlerts to check coverage.\n"
    )
    try:
        async with daemon_connection() as conn:
            response = await conn.get_me()
        if response.get("ok"):
            data = response["data"]
            name = " ".join(filter(None, [data.get("first_name"), data.get("last_name")]))
            username = data.get("username") or "none"
            base += (
                f" Connected account: id={data['id']}, name=\"{name}\", @{username}."
            )
    except (DaemonNotRunningError, Exception) as exc:
        logger.debug("server_instructions: could not fetch account info: %s", exc)
    return base


async def run_mcp_server() -> None:
    # Deferred: stdio_server touches the event loop at import time in some envs
    from mcp.server.stdio import stdio_server

    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )

    logger.info("MCP server starting — routing through daemon API")

    app.instructions = await _build_server_instructions()

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main() -> None:
    asyncio.run(run_mcp_server())
