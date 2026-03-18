# Coding Conventions

**Updated:** 2026-03-18

## Naming Patterns

**Files:**
- Package: `snake_case` (`mcp_telegram`)
- Modules: `snake_case.py` (`server.py`, `telegram.py`, `cache.py`)
- Private modules: `_base.py` (tools package internals)

**Functions:**
- Async tool runners: `snake_case`, named after tool class (`list_messages`, `get_user_info`)
- Private/internal: underscore prefix (`_track_tool_telemetry`, `_collect_unread_dialogs`)
- Tool registration: `@mcp_tool("primary")` or `@mcp_tool("secondary/helper")`

**Variables:**
- Local: `snake_case` (`entity_id`, `unread_chats`, `display_name`)
- Module constants: `UPPER_CASE` (`USER_TTL`, `GROUP_TTL`, `TOOL_REGISTRY`, `REACTION_NAMES_THRESHOLD`)

**Types:**
- Classes: `PascalCase` (`ToolArgs`, `ListDialogs`, `EntityCache`, `TopicMetadata`)
- Type hints: modern `|` syntax (`str | None`, `dict[int, str]`)
- Generic imports: `import typing as t`

## Code Style

**Formatting:**
- Tool: `ruff` (v0.8.2+)
- Line length: 120 characters
- Indentation: 4 spaces
- Quote style: double quotes

**Linting:**
- Tool: `ruff` with ALL rules except: `D` (docstrings), `TRY003`, `EM101`, `EM102`, `TCH`
- All fixable rules enabled

**Type Checking:**
- Tool: `mypy` with Pydantic plugin
- Untyped third-party imports: `# type: ignore[import-untyped]`

## Import Organization

1. `from __future__ import annotations`
2. Standard library: `import logging`, `import sqlite3`, `import typing as t`
3. Third-party: `from mcp.*`, `from pydantic*`, `from telethon*`
4. Local/relative: `from ..cache import EntityCache`, `from ._base import mcp_tool`

## Tool Registration Pattern

Every tool follows this pattern in a domain module under `src/mcp_telegram/tools/`:

```python
from ._base import ToolArgs, ToolResult, _text_response, mcp_tool

class MyTool(ToolArgs):
    """Tool description — becomes the LLM-visible description, prefixed with [posture]."""
    field: str = Field(max_length=500)

@mcp_tool("primary")
async def my_tool(args: MyTool) -> ToolResult:
    # Implementation
    return ToolResult(content=_text_response("result"), result_count=1)
```

`@mcp_tool(posture)` handles three things in one decorator:
1. `@tool_runner.register` — singledispatch registration
2. `@_track_tool_telemetry("MyTool")` — timing + analytics recording
3. `TOOL_REGISTRY["MyTool"] = (MyTool, posture)` — explicit registry entry

Posture values: `"primary"` (core tools), `"secondary/helper"` (analytics, diagnostics).

## Logging

- Logger: `logger = logging.getLogger(__name__)` in each module
- `logger.info()`: tool execution start (`"method[ToolName]"`)
- `logger.debug()`: args (guarded with `isEnabledFor`), connect/disconnect timing
- `logger.warning()`: non-fatal errors (cache write failures, fetch errors)
- `logger.error()`: telemetry recording failures, query failures
- Security: never log Telegram message content or sensitive data

## Error Handling

- Tool errors return `ToolResult` with action-oriented text (not exceptions)
- Error text functions in `errors.py` — format: `"Description.\nAction: what to do next."`
- `ValueError` for invalid arguments and unexpected schema state
- Specific exception types caught where possible (`sqlite3.Error`, `RPCError`)
- `exc_info=True` on warnings where stack traces aid debugging

## Return Values

- Tool runners return `ToolResult` (content + telemetry metadata)
- `_track_tool_telemetry` wrapper extracts `.content` for MCP
- Capability orchestration functions return union types (pattern-matched by callers)
