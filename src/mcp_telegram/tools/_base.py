from __future__ import annotations

import asyncio
import functools
import logging
import time
import typing as t
from dataclasses import dataclass
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict

from ..daemon_client import DaemonConnection, DaemonNotRunningError, daemon_connection

# Fetch reactor names only when total reactions per message are at or below this limit.
# Covers personal chats (always ≤ a few) while skipping expensive lookups on busy groups.
REACTION_NAMES_THRESHOLD = 15

logger = logging.getLogger(__name__)


class ToolArgs(BaseModel):
    model_config = ConfigDict()


def _text_response(text: str) -> list[TextContent]:
    """Wrap a plain string in the MCP TextContent envelope."""
    return [TextContent(type="text", text=text)]


def _daemon_not_running_text() -> str:
    """Return user-facing error message when the sync daemon is not running."""
    return (
        "Sync daemon is not running.\n"
        "Action: Start it with: mcp-telegram sync"
    )


@dataclass
class ToolResult:
    """Internal wrapper carrying MCP content plus telemetry metadata."""
    content: t.Sequence[TextContent | ImageContent | EmbeddedResource]
    result_count: int = 0
    has_cursor: bool = False
    page_depth: int = 1
    has_filter: bool = False


async def _send_telemetry_event(event_dict: dict) -> None:
    """Fire-and-forget: send telemetry to daemon. Never raises."""
    try:
        async with daemon_connection() as conn:
            await conn.record_telemetry(event=event_dict)
    except Exception as exc:
        logger.debug("telemetry_send_failed: %s", exc)


def _telemetry_done_callback(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("telemetry_event_failed error=%s", exc)


def _track_tool_telemetry(tool_name: str):
    """Decorator that wraps an async tool runner with timing + telemetry recording.

    Must be applied BETWEEN @tool_runner.register (outer) and the function def (inner)
    so singledispatch sees the original type annotation via __wrapped__.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(args):
            logger.debug("method[%s]", tool_name)
            t0 = time.monotonic()
            error_type = None
            tool_result: ToolResult | None = None
            try:
                tool_result = await fn(args)
                return tool_result.content
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                duration_ms = (time.monotonic() - t0) * 1000
                try:
                    task = asyncio.create_task(
                        _send_telemetry_event({
                            "tool_name": tool_name,
                            "timestamp": time.time(),
                            "duration_ms": duration_ms,
                            "result_count": tool_result.result_count if tool_result else 0,
                            "has_cursor": tool_result.has_cursor if tool_result else False,
                            "page_depth": tool_result.page_depth if tool_result else 1,
                            "has_filter": tool_result.has_filter if tool_result else False,
                            "error_type": error_type,
                        })
                    )
                    task.add_done_callback(_telemetry_done_callback)
                except Exception as e:
                    logger.debug("telemetry_send_skipped: %s", e)
        return wrapper
    return decorator


@singledispatch
async def tool_runner(
    args,  # noqa: ANN001
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    """Dispatch a ToolArgs instance to its registered async handler."""
    raise NotImplementedError(f"Unsupported type: {type(args)}")


# ---------------------------------------------------------------------------
# Explicit tool registry — replaces class introspection + sys.modules lookup
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, tuple[type[ToolArgs], str]] = {}


def mcp_tool(posture: str = "primary"):
    """Register runner with singledispatch + telemetry + tool registry.

    ``posture`` is a free-form label prepended to the tool description so the
    LLM can gauge how central the tool is.  Current values used in the codebase:

    * ``"primary"`` — core tools the LLM should reach for first.
    * ``"secondary/helper"`` — supporting tools (e.g. analytics, diagnostics).

    Replaces the 3-step manual registration:
      1. @tool_runner.register
      2. @_track_tool_telemetry("ToolName")
      3. TOOL_REGISTRY["ToolName"] = (ToolClass, posture)

    The decorated function must have a parameter annotated as ``args: YourToolArgs``
    — the parameter name ``args`` is required (used for type hint introspection).

    Usage:
        class MyTool(ToolArgs): ...

        @mcp_tool("primary")
        async def my_tool(args: MyTool) -> ToolResult: ...
    """
    def decorator(fn):
        hints = t.get_type_hints(fn)
        cls = hints["args"]
        name = cls.__name__
        # Apply telemetry wrapper
        wrapped = _track_tool_telemetry(name)(fn)
        # Register with singledispatch
        tool_runner.register(cls, wrapped)
        # Add to registry
        TOOL_REGISTRY[name] = (cls, posture)
        return wrapped
    return decorator


def tool_description(args: type[ToolArgs]) -> Tool:
    """Build an MCP Tool descriptor from a ToolArgs subclass."""
    schema = _sanitize_tool_schema(args.model_json_schema())
    entry = TOOL_REGISTRY.get(args.__name__)
    posture = entry[1] if entry else ""
    prefix = f"[{posture}] " if posture else ""
    return Tool(
        name=args.__name__,
        description=f"{prefix}{args.__doc__}",
        inputSchema=schema,
    )


def _sanitize_tool_schema(value: t.Any) -> t.Any:
    """Return MCP-friendly JSON schema without explicit null unions.

    Transforms: single-item ``anyOf`` with null variant → merged into parent dict;
    strips ``default: None`` from non-null typed fields so MCP clients see optional
    params as truly optional rather than defaulting to null.
    """
    if isinstance(value, dict):
        sanitized = {key: _sanitize_tool_schema(item) for key, item in value.items()}

        any_of = sanitized.get("anyOf")
        if isinstance(any_of, list):
            non_null_variants = [
                item for item in any_of if not (isinstance(item, dict) and item.get("type") == "null")
            ]
            has_null_variant = len(non_null_variants) != len(any_of)
            if has_null_variant and len(non_null_variants) == 1:
                replacement = non_null_variants[0]
                if not isinstance(replacement, dict):
                    return replacement

                merged = {
                    key: item
                    for key, item in sanitized.items()
                    if key not in {"anyOf", "default"}
                }
                return {**replacement, **merged}

        schema_type = sanitized.get("type")
        if sanitized.get("default") is None and schema_type != "null":
            sanitized.pop("default", None)

        return sanitized

    if isinstance(value, list):
        return [_sanitize_tool_schema(item) for item in value]

    return value


def tool_args(tool: Tool, *args, **kwargs) -> ToolArgs:  # noqa: ANN002, ANN003
    """Instantiate the ToolArgs subclass registered for *tool*."""
    entry = TOOL_REGISTRY.get(tool.name)
    if entry is None:
        raise ValueError(f"Unknown tool: {tool.name}")
    cls = entry[0]
    return cls(*args, **kwargs)


def verify_tool_registry() -> None:
    """Startup check: every registry entry has a matching class name, runner, and telemetry decorator.

    Raises ``AssertionError`` on mismatch — called at module load time in
    ``server.py`` without a catch, so failures crash the process immediately.
    Note: silently skipped under ``python -O`` (assertions disabled).

    The expected decorator stack is @tool_runner.register (outer) then @_track_tool_telemetry (inner).
    singledispatch sees the original type annotation via __wrapped__ on the telemetry wrapper.
    """
    for name, (cls, _posture) in TOOL_REGISTRY.items():
        assert cls.__name__ == name, f"Registry key {name!r} != class {cls.__name__!r}"
        dispatched = tool_runner.dispatch(cls)
        assert dispatched is not tool_runner, f"No runner for {name}"
        # The telemetry decorator wraps the original function; __wrapped__ must be present
        assert hasattr(dispatched, "__wrapped__"), (
            f"Runner for {name} is missing __wrapped__ — ensure @_track_tool_telemetry is applied"
        )
