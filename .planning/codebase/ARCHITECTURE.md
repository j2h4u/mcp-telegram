# Architecture

**Analysis Date:** 2026-03-11

## Pattern Overview

**Overall:** MCP Server Bridge Pattern - Request/Response multiplexer

**Key Characteristics:**
- Tool discovery via reflection (introspection of ToolArgs classes)
- Single-dispatch pattern for tool execution routing
- Async/await concurrency throughout
- Pydantic-based input validation and schema generation
- Two entry points: MCP server (stdio) and CLI debugging

## Layers

**Presentation Layer (MCP Protocol):**
- Purpose: Implements Model Context Protocol server interface
- Location: `src/mcp_telegram/server.py`
- Contains: Server initialization, tool listing, tool execution handlers
- Depends on: `tools` module for tool definitions and runners
- Used by: MCP clients (Claude Desktop, Inspector tools)

**Tool Definition & Execution Layer:**
- Purpose: Encapsulates individual tool logic and argument validation
- Location: `src/mcp_telegram/tools.py`
- Contains: ToolArgs base class, tool-specific argument classes (ListDialogs, ListMessages, GetMessage, SearchMessages, GetDialog), tool_runner dispatcher, tool metadata functions
- Depends on: Telegram client, Pydantic, MCP types
- Used by: Server layer for dispatching requests

**Telegram Integration Layer:**
- Purpose: Manages Telegram API connectivity and session handling
- Location: `src/mcp_telegram/telegram.py`
- Contains: TelegramClient factory, connection lifecycle, login/logout flows, configuration management via TelegramSettings
- Depends on: Telethon (Telegram API client), Pydantic Settings, XDG base directories
- Used by: Tools layer for creating authenticated client instances

**CLI Layer (Debugging):**
- Purpose: Provides developer-friendly debugging and testing interface
- Location: `cli.py` (root)
- Contains: list-tools and call-tool commands for manual testing
- Depends on: Server layer, Rich for formatting, Typer for CLI
- Used by: Developers during development/debugging

## Data Flow

**MCP Tool Execution Flow:**

1. MCP client calls `list_tools()` handler
2. Server reflects over `tools` module, finds all ToolArgs subclasses
3. For each subclass, generates Tool schema using Pydantic's `model_json_schema()`
4. Returns list of Tool definitions with names, descriptions, and input schemas

**Tool Call Flow:**

1. MCP client sends tool name + arguments (dict)
2. `call_tool()` handler receives name and arguments
3. Looks up tool in `mapping` dict (name → Tool descriptor)
4. Instantiates ToolArgs subclass with arguments (Pydantic validates)
5. Dispatches to `tool_runner.register` handler for that ToolArgs type
6. Handler creates Telegram client, executes API calls, formats response
7. Returns sequence of TextContent/ImageContent/EmbeddedResource

**Telegram Message Listing Flow:**

1. Tool receives dialog_id, unread flag, limit, optional before_id
2. Creates Telegram client via `create_client()` factory
3. Validates dialog exists via `GetPeerDialogsRequest`
4. Calls `iter_messages()` with optional filters (unread only, pagination via max_id)
5. For each message, extracts text and wraps in TextContent
6. Returns list of formatted message strings

**State Management:**
- Session state: Stored in XDG state directory (`~/.local/share/mcp-telegram/`)
- Client caching: `@cache` decorator on `create_client()` - reuses same client instance within process lifetime
- Configuration: Environment variables (TELEGRAM_API_ID, TELEGRAM_API_HASH) via Pydantic Settings

## Key Abstractions

**ToolArgs (Base Class):**
- Purpose: Represents tool input contract
- Examples: `ListDialogs`, `ListMessages`, `GetMessage`, `SearchMessages`, `GetDialog` in `src/mcp_telegram/tools.py`
- Pattern: Pydantic BaseModel with type-hinted fields; docstring becomes tool description

**tool_runner (Single-Dispatch Function):**
- Purpose: Routes tool execution to type-specific async handler
- Examples: `list_dialogs(args: ListDialogs)`, `list_messages(args: ListMessages)` in `src/mcp_telegram/tools.py`
- Pattern: `@singledispatch` decorator enables type-based dispatch; `@tool_runner.register` registers handlers per ToolArgs type

**TelegramClient (Factory Pattern):**
- Purpose: Centralized Telegram API client creation with cached singleton behavior
- Examples: `create_client()` in `src/mcp_telegram/telegram.py`
- Pattern: Cached factory function; reads credentials from environment or arguments; manages session file storage

**Tool Schema Generation:**
- Purpose: Dynamically generates MCP Tool schema from Pydantic model
- Examples: `tool_description()` in `src/mcp_telegram/tools.py`
- Pattern: Extracts class name as tool name, docstring as description, calls `model_json_schema()` for input validation schema

## Entry Points

**MCP Server (Primary):**
- Location: `src/mcp_telegram/__init__.py` (run command) → `src/mcp_telegram/server.py` (run_mcp_server)
- Triggers: CLI invocation via `mcp-telegram` command (configured in Claude Desktop)
- Responsibilities: Starts stdio-based MCP server, listens for tool calls, routes to handlers

**CLI Sign-In (Setup):**
- Location: `src/mcp_telegram/__init__.py` (sign_in command)
- Triggers: Manual user invocation: `mcp-telegram sign-in --api-id X --api-hash Y --phone-number Z`
- Responsibilities: Interactive Telegram authentication, saves session to XDG state directory, handles 2FA

**CLI Logout:**
- Location: `src/mcp_telegram/__init__.py` (logout command)
- Triggers: Manual user invocation: `mcp-telegram logout`
- Responsibilities: Destroys session file, logs out from Telegram API

**CLI Tool Testing (Development):**
- Location: `cli.py` (root)
- Triggers: Manual invocation: `uv run cli.py list-tools` or `uv run cli.py call-tool --name X --arguments Y`
- Responsibilities: Lists available tools with schemas, executes individual tools with manual arguments

## Error Handling

**Strategy:** Try-catch at call_tool boundary; log and re-raise as RuntimeError

**Patterns:**
- `TypeErrors` for invalid argument types (dictionary check in `call_tool`)
- `ValueErrors` for missing entities (dialog not found, message not found)
- `SessionPasswordNeededError` caught during login if 2FA enabled
- General exception catch in `call_tool` handler with exception logging then RuntimeError wrapping

## Cross-Cutting Concerns

**Logging:** `logging` module with logger per file; configured via `logging.getLogger(__name__)`; INFO level for user operations, DEBUG for tool calls

**Validation:** Pydantic models validate tool arguments; Pydantic Settings validates Telegram credentials from environment

**Authentication:** Environment variables (TELEGRAM_API_ID, TELEGRAM_API_HASH) via TelegramSettings; session persistence via Telethon's built-in session file in XDG state directory

---

*Architecture analysis: 2026-03-11*
