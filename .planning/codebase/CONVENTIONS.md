# Coding Conventions

**Analysis Date:** 2026-03-11

## Naming Patterns

**Files:**
- Package name: `snake_case` (e.g., `mcp_telegram`, `mcp-telegram` in project name)
- Module files: `snake_case.py` (e.g., `server.py`, `telegram.py`, `tools.py`)

**Functions:**
- Async functions: `snake_case` with `async` keyword
- Private/internal functions: Prefixed with underscore (e.g., `_run()` in `__init__.py`)
- Tool runner functions: Named after their tool class (lowercase) with `@tool_runner.register` decorator (e.g., `list_dialogs`, `get_message`)

**Variables:**
- Local variables: `snake_case` (e.g., `user_session`, `response`, `iter_messages_args`)
- Constants/configuration: UPPER_CASE (e.g., in settings classes)
- Private module variables: Underscore prefix where appropriate

**Types:**
- Classes: `PascalCase` (e.g., `ToolArgs`, `ListDialogs`, `TelegramSettings`)
- Type hints: Use modern Python type syntax with `|` for unions (e.g., `str | None`, `dict[str, t.Any]`)
- Generic imports: `import typing as t` for type module (seen in `server.py`, `tools.py`)

## Code Style

**Formatting:**
- Tool: `ruff` (v0.8.2)
- Line length: 120 characters (configured in `ruff.toml`)
- Indentation: 4 spaces (configured in `ruff.toml`)
- Quote style: Double quotes for strings (configured in `ruff.toml`)

**Linting:**
- Tool: `ruff` with ALL rules enabled except: `D` (docstrings), `TRY003`, `EM101`, `EM102`, `TCH` (type checking imports)
- All fixable rules enabled (`fixable = ["ALL"]`)
- Dummy variable pattern: `^(_+|(_+[a-zA-Z0-9_]*[a-zA-Z0-9]+?))$`

**Type Checking:**
- Tool: `mypy` (v1.13.0+) with Pydantic plugin enabled
- Executed in pre-commit hooks (`local` hook in `.pre-commit-config.yaml`)
- Untyped third-party imports explicitly handled with `# type: ignore[import-untyped]` comments
- Example: `from telethon import TelegramClient  # type: ignore[import-untyped]`

## Import Organization

**Order:**
1. Future imports: `from __future__ import annotations` (seen at top of most modules)
2. Standard library: `import asyncio`, `import logging`, `import sys`, `import typing as t`
3. Third-party: `from mcp.*`, `from pydantic*`, `from telethon*`, `from typer*`, `from xdg_base_dirs*`
4. Local/relative: `from . import tools`, `from .telegram import create_client`

**Path Aliases:**
- Relative imports used for local modules (e.g., `from . import tools`, `from .server import run_mcp_server`)
- No absolute path aliases configured

## Error Handling

**Patterns:**
- Explicit exception types raised: `TypeError`, `ValueError`, `RuntimeError`, `NotImplementedError`
- Specific exception handling: Catch specific exceptions (e.g., `SessionPasswordNeededError` in `telegram.py`)
- Broad exception catching only when necessary: `except Exception as e:` in `server.py` call_tool handler
- Exception context preserved with `raise ... from None` or `raise ... from e` as appropriate
- Examples from `tools.py`:
  - `raise ValueError(f"Channel not found: {args.dialog_id}")` (line 135)
  - `raise TypeError(f"Unexpected result: {type(result)}")` (line 138)

## Logging

**Framework:** Standard library `logging` module

**Patterns:**
- Logger instantiation: `logger = logging.getLogger(__name__)` in each module
- Log levels used:
  - `logger.debug()`: For discovering available tools (server.py, line 31)
  - `logger.info()`: For tool execution start with method name and args (tools.py, lines 84, 129, 174, 205, 235)
  - `logger.exception()`: For unexpected errors with context (server.py, line 83)
- Log format: Structured messages with method name in brackets: `"method[ToolName] args[%s]"`
- Note: Security concern fixed (commit 8532917) - message content no longer logged to avoid leaking sensitive data

## Comments

**When to Comment:**
- Inline comments for non-obvious logic (e.g., caching rationale, algorithm explanations)
- Header comments before tool implementations: `### ToolName ###` (lines 68-69, 101-102, 159-160, etc. in tools.py)

**JSDoc/TSDoc:**
- Python docstrings follow Google/PEP 257 style
- Tool docstrings used to describe tool purpose and behavior for MCP schema generation
- Docstrings become tool descriptions via `tool_description()` function (server.py, lines 56-61)
- Extended docstrings include usage notes (e.g., ListMessages explains pagination, SearchMessages explains sorting)

**Example docstring (tools.py, lines 105-116):**
```python
class ListMessages(ToolArgs):
    """
    List messages in a given dialog, chat or channel. The messages are listed in order from newest to oldest.

    If `unread` is set to `True`, only unread messages will be listed. Once a message is read, it will not be
    listed again.

    If `limit` is set, only the last `limit` messages will be listed. If `unread` is set, the limit will be
    the minimum between the unread messages and the limit.

    If `before_id` is set, only messages older than the given message ID will be listed. Use this for
    pagination: pass the ID of the oldest message from the previous page to get the next page.
    """
```

## Function Design

**Size:**
- Functions generally keep to single responsibilities
- Async context managers used for resource management (`async with create_client() as client:`)
- Single dispatch pattern for tool runners allows separation of concerns per tool

**Parameters:**
- Annotated parameters with descriptions using Typer `Option()` (in `__init__.py`, lines 17-20)
- Single class argument pattern for tool runners: `args: SpecificToolArgs` (singledispatch pattern)
- Dictionary unpacking when passing function arguments (server.py, line 80)

**Return Values:**
- Explicit return types: `-> t.Sequence[TextContent | ImageContent | EmbeddedResource]` (async tool runners)
- List comprehension returns with type specified: `response: list[TextContent] = []`
- Consistent return type across all tool implementations

## Module Design

**Exports:**
- Entry point: `mcp_telegram:app` (Typer CLI app from `__init__.py`)
- Server entry: `mcp_telegram:app` or `mcp_telegram:run` functions
- No explicit `__all__` declarations; all public names are exports
- Private names prefixed with `_` (e.g., `_run()` helper in `__init__.py`)

**Barrel Files:**
- Import-based composition: `from . import tools` (server.py imports tools module for introspection)
- Server module maintains singleton pattern: `app = Server("mcp-telegram")` (server.py, line 24)

**Singledispatch Pattern:**
- Base dispatcher: `@singledispatch async def tool_runner(args)` (tools.py, lines 49-53)
- Tool-specific implementations: `@tool_runner.register async def list_dialogs(args: ListDialogs)` (tools.py, line 79)
- Allows adding new tools without modifying existing dispatcher code
- Tools discovered via `inspect.getmembers()` for automatic registration (server.py, lines 28-33)

---

*Convention analysis: 2026-03-11*
