# Architecture

**Updated:** 2026-03-18

## Pattern Overview

**Overall:** MCP Server Bridge Pattern — Request/Response multiplexer

**Key Characteristics:**
- Explicit tool registry via `@mcp_tool` decorator (no reflection)
- Single-dispatch pattern for tool execution routing
- Async/await concurrency throughout
- Pydantic-based input validation and schema generation
- Two entry points: MCP server (stdio) and CLI debugging

## Layers

**Presentation Layer (MCP Protocol):**
- Location: `src/mcp_telegram/server.py`
- Contains: Server initialization, tool listing, tool execution handlers
- Depends on: `tools` package for tool definitions and runners
- Used by: MCP clients (Claude Desktop, Claude Code, Inspector tools)

**Tool Layer:**
- Location: `src/mcp_telegram/tools/` package
- Contains: `_base.py` (ToolArgs, ToolResult, mcp_tool decorator, TOOL_REGISTRY, singledispatch, telemetry, connected_client), plus domain modules: `discovery.py` (ListDialogs, GetMyAccount), `reading.py` (ListMessages, SearchMessages), `unread.py` (ListUnreadMessages), `user_info.py` (GetUserInfo), `stats.py` (GetUsageStats)
- Depends on: Capability layer, Telegram client, Pydantic, MCP types
- Used by: Server layer for dispatching requests

**Capability Layer:**
- Location: Split across `capability_history.py`, `capability_search.py`, `capability_topics.py`, with `capabilities.py` as a backwards-compatible re-export shim
- Contains: Orchestration functions (`execute_history_read_capability`, `execute_search_messages_capability`, `execute_list_topics_capability`) that compose dialog resolution, topic handling, message fetching, and pagination
- Supporting modules: `models.py` (shared dataclasses/TypedDicts), `budget.py` (unread tier classification, budget allocation), `dialog_target.py` (dialog resolution), `forum_topics.py` (topic catalog loading), `message_ops.py` (message fetch helpers, reply/reaction maps), `errors.py` (action-oriented error text)
- Depends on: Resolver, cache, formatter, pagination

**Resolution & Cache Layer:**
- Location: `src/mcp_telegram/resolver.py`, `src/mcp_telegram/cache.py`
- Contains: Fuzzy name matching (Cyrillic + transliteration), SQLite-backed entity/reaction/topic caches with TTL
- Used by: Capability layer and tools

**Telegram Integration Layer:**
- Location: `src/mcp_telegram/telegram.py`
- Contains: TelegramClient factory (`@cache` singleton), connection lifecycle, login/logout flows, TelegramSettings
- Depends on: Telethon, Pydantic Settings, XDG base directories
- Used by: Tools layer via `connected_client()` context manager

**Formatting & Pagination Layer:**
- Location: `src/mcp_telegram/formatter.py`, `src/mcp_telegram/pagination.py`
- Contains: Message formatting (date headers, session breaks, reply annotations), cursor encode/decode (base64 JSON navigation tokens)

**Observability Layer:**
- Location: `src/mcp_telegram/analytics.py`
- Contains: TelemetryCollector (SQLite), TelemetryEvent dataclass, format_usage_summary
- Integrated via `_track_tool_telemetry` decorator in `_base.py`

**CLI Layer (Debugging):**
- Location: `cli.py` (root)
- Contains: list-tools and call-tool commands for manual testing
- Depends on: Server layer, Rich for formatting, Typer for CLI

## Data Flow

**Tool Discovery Flow:**
1. MCP client calls `list_tools()` handler
2. Server iterates `TOOL_REGISTRY` dict (populated at import time by `@mcp_tool`)
3. For each `(ToolArgsClass, posture)`, generates Tool schema via Pydantic `model_json_schema()`
4. Returns list of Tool definitions with `[posture] docstring` descriptions

**Tool Call Flow:**
1. MCP client sends tool name + arguments (dict)
2. `call_tool()` handler receives name and arguments
3. Looks up tool class in `TOOL_REGISTRY`, instantiates ToolArgs (Pydantic validates)
4. Dispatches to `tool_runner` singledispatch (which routes to the `@mcp_tool`-registered handler)
5. `_track_tool_telemetry` wrapper logs, times, and records telemetry
6. Handler returns `ToolResult` (content + telemetry metadata); wrapper extracts `.content`

**State Management:**
- Session state: XDG state directory (`~/.local/state/mcp-telegram/`)
- Client caching: `@cache` on `create_client()` — reuses same client within process lifetime
- Entity cache: `entity_cache.db` (SQLite, TTL-based)
- Analytics: `analytics.db` (SQLite, 30-day rolling window)
- Configuration: Environment variables (TELEGRAM_API_ID, TELEGRAM_API_HASH) via Pydantic Settings

## Entry Points

**MCP Server (Primary):**
- `src/mcp_telegram/__init__.py` (run command) → `src/mcp_telegram/server.py` (run_mcp_server)
- Starts stdio-based MCP server

**CLI Sign-In:** `mcp-telegram sign-in --api-id X --api-hash Y --phone-number Z`

**CLI Logout:** `mcp-telegram logout`

**CLI Tool Testing:** `uv run cli.py list-tools` / `uv run cli.py call-tool --name X --arguments Y`
