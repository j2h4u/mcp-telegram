# Codebase Structure

**Updated:** 2026-03-18

## Directory Layout

```
mcp-telegram/
├── src/
│   └── mcp_telegram/
│       ├── __init__.py              # CLI entry point (Typer app)
│       ├── server.py                # MCP server implementation
│       ├── tools/                   # Tool package (domain-split)
│       │   ├── __init__.py          # Re-exports all tools + base helpers
│       │   ├── _base.py             # ToolArgs, ToolResult, mcp_tool, TOOL_REGISTRY, singledispatch, telemetry
│       │   ├── discovery.py         # ListDialogs, GetMyAccount
│       │   ├── reading.py           # ListMessages, SearchMessages
│       │   ├── unread.py            # ListUnreadMessages
│       │   ├── user_info.py         # GetUserInfo
│       │   └── stats.py             # GetUsageStats
│       ├── capabilities.py          # Re-export shim for split capability modules
│       ├── capability_history.py    # execute_history_read_capability
│       ├── capability_search.py     # execute_search_messages_capability
│       ├── capability_topics.py     # execute_list_topics_capability
│       ├── models.py                # Shared dataclasses, TypedDicts, type aliases
│       ├── budget.py                # Unread tier classification, budget allocation
│       ├── dialog_target.py         # Dialog resolution orchestration
│       ├── forum_topics.py          # Topic catalog loading, topic resolution
│       ├── message_ops.py           # Message fetch helpers, reply/reaction maps
│       ├── errors.py                # Action-oriented error text functions
│       ├── resolver.py              # Fuzzy name matching (Cyrillic + transliteration)
│       ├── cache.py                 # SQLite caches (entities, reactions, topics)
│       ├── formatter.py             # Message formatting (date headers, session breaks)
│       ├── pagination.py            # Cursor encode/decode (base64 JSON tokens)
│       ├── analytics.py             # Local telemetry (analytics.db)
│       ├── telegram.py              # Telethon client factory, auth flows
│       └── py.typed                 # PEP 561 marker
├── tests/
│   ├── conftest.py                  # Shared fixtures (mock_cache, mock_client, etc.)
│   ├── test_tools.py                # Tool integration tests
│   ├── test_capabilities.py         # Capability orchestration tests
│   ├── test_cache.py                # SQLite cache tests
│   ├── test_resolver.py             # Fuzzy resolution tests
│   ├── test_formatter.py            # Message formatting tests
│   ├── test_pagination.py           # Navigation token tests
│   ├── test_analytics.py            # Telemetry tests
│   ├── test_server.py               # MCP server tests
│   ├── test_cli.py                  # CLI entry point tests
│   ├── test_load.py                 # Import smoke test
│   ├── test_mcp_test_client.py      # MCP client integration tests
│   └── fixtures/                    # Test fixtures
├── devtools/                        # Development tools
│   └── mcp_client/                  # MCP client scripts and smoke tests
├── cli.py                           # Development debugging CLI
├── pyproject.toml                   # Package metadata and dependencies
├── uv.lock                          # Dependency lock file
├── ruff.toml                        # Ruff linter configuration
├── .pre-commit-config.yaml          # Pre-commit hooks
├── AGENTS.md                        # Agent notes for AI assistants
├── README.md                        # User documentation
├── CHANGELOG.md                     # Release notes
└── LICENSE                          # MIT license
```

## Key File Locations

**Entry Points:**
- `src/mcp_telegram/__init__.py`: CLI entry point via Typer; commands: `run`, `sign_in`, `logout`
- `src/mcp_telegram/server.py`: MCP server entry point `run_mcp_server()`; handles MCP protocol over stdio
- `cli.py`: Development/debugging entry point; commands: `list-tools`, `call-tool`

**Tool Registration:**
- `src/mcp_telegram/tools/_base.py`: `@mcp_tool(posture)` decorator handles singledispatch registration, telemetry wrapping, and `TOOL_REGISTRY` population in one step
- `src/mcp_telegram/tools/__init__.py`: Re-exports all tool classes and runner functions

**Testing:**
- `uv run pytest` — full suite (280+ tests)
- `uv run pytest tests/test_tools.py -k topic -v` — focused topic slice
- `uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"..."}'` — manual debug

## Where to Add New Code

**New Tool:**
1. Create ToolArgs subclass + runner in the appropriate domain module under `src/mcp_telegram/tools/`:
   ```python
   from ._base import ToolArgs, ToolResult, _text_response, mcp_tool

   class MyNewTool(ToolArgs):
       """Tool description shown to LLM."""
       field1: str

   @mcp_tool("primary")
   async def my_new_tool(args: MyNewTool) -> ToolResult:
       return ToolResult(content=_text_response("result"))
   ```
2. Import the module in `src/mcp_telegram/tools/__init__.py` so it registers at import time.
3. No wiring in `server.py` needed — server iterates `TOOL_REGISTRY`.

**New Feature (Non-Tool):**
- Telegram integration: `src/mcp_telegram/telegram.py`
- MCP protocol feature: `src/mcp_telegram/server.py`
- Shared types/dataclasses: `src/mcp_telegram/models.py`
- Error text: `src/mcp_telegram/errors.py`

**Package Entry Points (pyproject.toml):**
- `mcp-telegram` → `src/mcp_telegram:app` (Typer CLI app)
