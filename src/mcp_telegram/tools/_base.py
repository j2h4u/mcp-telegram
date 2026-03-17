from __future__ import annotations

import functools
import logging
import time
import typing as t
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import cache as functools_cache
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict
from xdg_base_dirs import xdg_state_home

from ..cache import EntityCache
from ..resolver import (
    Candidates,
    NotFound,
    Resolved,
    ResolvedWithMessage,
    resolve_dialog,
)
from .. import telegram as _telegram_mod

# Fetch reactor names only when total reactions per message are at or below this limit.
# Covers personal chats (always ≤ a few) while skipping expensive lookups on busy groups.
REACTION_NAMES_THRESHOLD = 15

logger = logging.getLogger(__name__)


class ToolArgs(BaseModel):
    model_config = ConfigDict()


def _text_response(text: str) -> list[TextContent]:
    """Wrap a plain string in the MCP TextContent envelope."""
    return [TextContent(type="text", text=text)]


@dataclass
class ToolResult:
    """Internal wrapper carrying MCP content plus telemetry metadata."""
    content: t.Sequence[TextContent | ImageContent | EmbeddedResource]
    result_count: int = 0
    has_cursor: bool = False
    page_depth: int = 1
    has_filter: bool = False


def _track_tool_telemetry(tool_name: str):
    """Decorator that wraps an async tool runner with timing + telemetry recording.

    Must be applied BETWEEN @tool_runner.register (outer) and the function def (inner)
    so singledispatch sees the original type annotation via __wrapped__.
    """
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(args):
            logger.info("method[%s]", tool_name)
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug("method[%s] args[%s]", tool_name, args)
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
                    from ..analytics import TelemetryEvent
                    collector = _get_analytics_collector()
                    collector.record_event(TelemetryEvent(
                        tool_name=tool_name,
                        timestamp=time.time(),
                        duration_ms=duration_ms,
                        result_count=tool_result.result_count if tool_result else 0,
                        has_cursor=tool_result.has_cursor if tool_result else False,
                        page_depth=tool_result.page_depth if tool_result else 1,
                        has_filter=tool_result.has_filter if tool_result else False,
                        error_type=error_type,
                    ))
                except Exception as e:
                    logger.error("Failed to record telemetry for %s: %s", tool_name, e, exc_info=True)
        return wrapper
    return decorator


@singledispatch
async def tool_runner(
    args,  # noqa: ANN001
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    raise NotImplementedError(f"Unsupported type: {type(args)}")


# ---------------------------------------------------------------------------
# Explicit tool registry — replaces class introspection + sys.modules lookup
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, tuple[type[ToolArgs], str]] = {}


def mcp_tool(posture: str = "primary"):
    """Register runner with singledispatch + telemetry + tool registry.

    Replaces the 3-step manual registration:
      1. @tool_runner.register
      2. @_track_tool_telemetry("ToolName")
      3. TOOL_REGISTRY["ToolName"] = (ToolClass, posture)

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
    """Return MCP-friendly JSON schema without explicit null unions."""
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
    entry = TOOL_REGISTRY.get(tool.name)
    if entry is None:
        raise ValueError(f"Unknown tool: {tool.name}")
    cls = entry[0]
    return cls(*args, **kwargs)


@asynccontextmanager
async def connected_client():
    """Reentrant connection wrapper: only the outermost caller disconnects.

    Safe to nest — inner calls see the client already connected and skip
    both connect and disconnect, so the outer block retains ownership.
    """
    client = _telegram_mod.create_client()
    owns_connection = not client.is_connected()
    if owns_connection:
        t0 = time.monotonic()
        await client.connect()
        logger.debug("tg_connect: %.1fms", (time.monotonic() - t0) * 1000)
    try:
        yield client
    finally:
        if owns_connection:
            t0 = time.monotonic()
            await client.disconnect()
            logger.debug("tg_disconnect: %.1fms", (time.monotonic() - t0) * 1000)


@functools_cache
def get_entity_cache() -> EntityCache:
    """Return the shared EntityCache instance (opened once per process)."""
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "entity_cache.db"
    return EntityCache(db_path)


def _get_analytics_collector():
    """Lazy-init analytics collector — creates state dir + DB on first call."""
    from ..analytics import TelemetryCollector
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_dir / "analytics.db"
    return TelemetryCollector.get_instance(db_path)


async def _resolve_dialog(cache: EntityCache, query: str) -> Resolved | ResolvedWithMessage | Candidates | NotFound:
    """Resolve one dialog via the consolidated resolver (with warmup and API fallback)."""
    return await resolve_dialog(query, cache, connected_client)


def verify_tool_registry() -> None:
    """Startup check: every registry entry has a matching class name, runner, and telemetry decorator.

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
