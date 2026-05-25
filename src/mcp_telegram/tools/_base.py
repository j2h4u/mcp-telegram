import asyncio
import functools
import logging
import re
import time
import typing as t
from collections.abc import Mapping
from dataclasses import dataclass
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import BaseModel, ConfigDict

from ..daemon_client import DaemonConnection, DaemonNotRunningError, daemon_connection

__all__ = ["DaemonConnection", "DaemonNotRunningError", "daemon_connection"]

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
    return "Telegram backend is not running.\nAction: Start it with: mcp-telegram sync"


def _check_daemon_response(
    response: dict,
    *,
    action: str = "Retry with corrected arguments or inspect the relevant status/list tool for valid ids.",
    **extra_kwargs: t.Any,
) -> ToolResult | None:
    """Return a ToolResult with error text if response is not ok, else None.

    Callers use: ``if err := _check_daemon_response(response): return err``
    """
    if response.get("ok"):
        return None
    error_detail = response.get("message", "Request failed.")
    error_code = response.get("error")
    if isinstance(error_code, str) and error_code and error_code not in str(error_detail):
        text = f"Error: {error_code}: {error_detail}"
    else:
        text = f"Error: {error_detail}"
    if "action:" not in text.lower():
        text = f"{text}\nAction: {action}"
    return error_result(text, **extra_kwargs)


@dataclass
class ToolResult:
    """Internal wrapper carrying MCP content plus telemetry metadata."""

    content: t.Sequence[TextContent | ImageContent | EmbeddedResource] = ()
    is_error: bool = False
    structured_content: dict[str, t.Any] | None = None
    result_count: int = 0
    has_cursor: bool = False
    page_depth: int = 1
    has_filter: bool = False


@dataclass(frozen=True)
class ToolRegistryEntry:
    cls: type[ToolArgs]
    posture: str
    annotations: ToolAnnotations | None
    exported_name: str
    title: str
    output_schema: dict[str, t.Any] | None = None

    def __iter__(self) -> t.Iterator[object]:
        """Preserve tuple-unpack compatibility while callers migrate."""
        yield self.cls
        yield self.posture
        yield self.annotations

    def __getitem__(self, index: int) -> object:
        return (self.cls, self.posture, self.annotations)[index]


def structured_result(structured_content: Mapping[str, t.Any], **metadata: t.Any) -> ToolResult:
    """Return a successful structured-only tool result."""
    return ToolResult(content=(), structured_content=dict(structured_content), **metadata)


def error_result(text: str, **metadata: t.Any) -> ToolResult:
    """Return recoverable error text as an MCP tool result."""
    return ToolResult(content=_text_response(text), is_error=True, **metadata)


# Strong references to fire-and-forget tasks prevent GC before completion.
_background_tasks: set[asyncio.Task] = set()


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

    Applied automatically by @mcp_tool() — do not use directly.
    """

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapper(args):
            logger.debug("method[%s]", tool_name)
            start_time = time.monotonic()
            error_type = None
            tool_result: ToolResult | None = None
            try:
                tool_result = await fn(args)
                return tool_result
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                duration_ms = (time.monotonic() - start_time) * 1000
                try:
                    task = asyncio.create_task(
                        _send_telemetry_event(
                            {
                                "tool_name": tool_name,
                                "timestamp": time.time(),
                                "duration_ms": duration_ms,
                                "result_count": tool_result.result_count if tool_result else 0,
                                "has_cursor": tool_result.has_cursor if tool_result else False,
                                "page_depth": tool_result.page_depth if tool_result else 1,
                                "has_filter": tool_result.has_filter if tool_result else False,
                                "error_type": error_type,
                            }
                        )
                    )
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                    task.add_done_callback(_telemetry_done_callback)
                except Exception as e:
                    logger.debug("telemetry_send_skipped: %s", e)

        return wrapper

    return decorator


@singledispatch
async def tool_runner(
    args,
) -> ToolResult:
    """Dispatch a ToolArgs instance to its registered async handler."""
    raise NotImplementedError(f"Unsupported type: {type(args)}")


# ---------------------------------------------------------------------------
# Explicit tool registry — replaces class introspection + sys.modules lookup
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, ToolRegistryEntry] = {}


def _class_name_to_snake(name: str) -> str:
    first_pass = re.sub("(.)([A-Z][a-z]+)", r"\1_\2", name)
    return re.sub("([a-z0-9])([A-Z])", r"\1_\2", first_pass).lower()


def mcp_tool(
    name: str,
    title: str | None = None,
    *,
    posture: str = "primary",
    annotations: ToolAnnotations | None = None,
    output_schema: dict[str, t.Any] | None = None,
):
    """Register runner with singledispatch + telemetry + tool registry.

    ``posture`` is a free-form label prepended to the tool description so the
    LLM can gauge how central the tool is.  Current values used in the codebase:

    * ``"primary"`` — core tools the LLM should reach for first.
    * ``"secondary/helper"`` — supporting tools (e.g. analytics, diagnostics).

    ``annotations`` is an optional ``ToolAnnotations`` instance carrying MCP
    behavioural hints (``readOnlyHint``, ``destructiveHint``, etc.) that
    clients and orchestrators use to gauge safety before invoking the tool.

    Replaces the 3-step manual registration:
      1. @tool_runner.register
      2. @_track_tool_telemetry("ToolName")
      3. TOOL_REGISTRY["ToolName"] = (ToolClass, posture, annotations)

    The decorated function must have a parameter annotated as ``args: YourToolArgs``
    — the parameter name ``args`` is required (used for type hint introspection).

    Usage:
        class MyTool(ToolArgs): ...

        @mcp_tool(name="my_tool", title="My Tool", annotations=ToolAnnotations(readOnlyHint=True))
        async def my_tool(args: MyTool) -> ToolResult: ...
    """

    def decorator(fn):
        hints = t.get_type_hints(fn)
        cls = hints["args"]
        exported_name = name
        exported_title = title
        exported_posture = posture
        if title is None and name in {"primary", "secondary/helper"}:
            exported_name = _class_name_to_snake(cls.__name__)
            exported_title = cls.__name__
            exported_posture = name
        # Apply telemetry wrapper
        wrapped = _track_tool_telemetry(exported_name)(fn)
        # Register with singledispatch
        tool_runner.register(cls, wrapped)
        # Add to registry
        TOOL_REGISTRY[exported_name] = ToolRegistryEntry(
            cls=cls,
            posture=exported_posture,
            annotations=annotations,
            exported_name=exported_name,
            title=exported_title or cls.__name__,
            output_schema=output_schema,
        )
        return wrapped

    return decorator


def tool_description(exported_name: str, cls: type[ToolArgs], entry: ToolRegistryEntry) -> Tool:
    """Build an MCP Tool descriptor from registry metadata."""
    schema = _sanitize_tool_schema(cls.model_json_schema())
    posture = entry.posture
    prefix = f"[{posture}] " if posture else ""
    return Tool(
        name=exported_name,
        title=entry.title,
        description=f"{prefix}{cls.__doc__}",
        inputSchema=schema,
        outputSchema=entry.output_schema,
        annotations=entry.annotations,
    )


def _sanitize_tool_schema(value: t.Any) -> t.Any:
    """Return MCP-friendly JSON schema without explicit null unions.

    Why: Claude Desktop and other MCP clients reject or misrender ``anyOf``
    with null variants as required fields. This strips those patterns so
    optional params appear as truly optional.

    Transforms: single-item ``anyOf`` with null variant → merged into parent dict;
    strips ``default: None`` from non-null typed fields.
    """
    if isinstance(value, dict):
        sanitized = {key: _sanitize_tool_schema(item) for key, item in value.items()}

        any_of = sanitized.get("anyOf")
        if isinstance(any_of, list):
            non_null_variants = [item for item in any_of if not (isinstance(item, dict) and item.get("type") == "null")]
            has_null_variant = len(non_null_variants) != len(any_of)
            if has_null_variant and len(non_null_variants) == 1:
                replacement = non_null_variants[0]
                if not isinstance(replacement, dict):
                    return replacement

                merged = {key: item for key, item in sanitized.items() if key not in {"anyOf", "default"}}
                return {**replacement, **merged}

        schema_type = sanitized.get("type")
        if sanitized.get("default") is None and schema_type != "null":
            sanitized.pop("default", None)

        return sanitized

    if isinstance(value, list):
        return [_sanitize_tool_schema(item) for item in value]

    return value


def tool_args(tool: Tool, *args, **kwargs) -> ToolArgs:
    """Instantiate the ToolArgs subclass registered for *tool*."""
    entry = TOOL_REGISTRY.get(tool.name)
    if entry is None:
        raise ValueError(f"Unknown tool: {tool.name}")
    cls = entry.cls
    return cls(*args, **kwargs)


def verify_tool_registry() -> None:
    """Startup check: every registry entry has a valid exported name, runner, and telemetry decorator.

    Raises ``RuntimeError`` on mismatch — called at module load time in
    ``server.py`` without a catch, so failures crash the process immediately.

    The expected decorator stack is @tool_runner.register (outer) then @_track_tool_telemetry (inner).
    singledispatch sees the original type annotation via __wrapped__ on the telemetry wrapper.
    """
    name_pattern = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
    for name, entry in TOOL_REGISTRY.items():
        if not name_pattern.match(name):
            raise RuntimeError(f"Registry key {name!r} is not a valid snake_case tool name")
        if entry.exported_name != name:
            raise RuntimeError(f"Registry key {name!r} != exported name {entry.exported_name!r}")
        title_words = entry.title.split()
        if not 1 <= len(title_words) <= 3:
            raise RuntimeError(f"Tool {name} title must be 1-3 words")
        cls = entry.cls
        dispatched = tool_runner.dispatch(cls)
        if dispatched is tool_runner:
            raise RuntimeError(f"No runner for {name}")
        if not hasattr(dispatched, "__wrapped__"):
            raise RuntimeError(f"Runner for {name} is missing __wrapped__ — ensure @_track_tool_telemetry is applied")
