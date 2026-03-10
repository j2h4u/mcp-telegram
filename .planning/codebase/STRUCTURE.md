# Codebase Structure

**Analysis Date:** 2026-03-11

## Directory Layout

```
mcp-telegram/
├── src/                        # Python package source
│   └── mcp_telegram/           # Main package
│       ├── __init__.py         # CLI entry point (Typer app)
│       ├── server.py           # MCP server implementation
│       ├── tools.py            # Tool definitions and runners
│       ├── telegram.py         # Telegram API client and config
│       └── py.typed            # PEP 561 marker for type hints
├── cli.py                      # Development debugging CLI
├── pyproject.toml              # Package metadata and dependencies
├── uv.lock                     # Dependency lock file
├── ruff.toml                   # Ruff linter configuration
├── .pre-commit-config.yaml     # Pre-commit hooks configuration
├── cog.toml                    # Changelog generation configuration
├── .dockerignore                # Docker build exclusions
├── .gitignore                  # Git exclusions
├── .mise.toml                  # Mise tool version management
├── README.md                   # User documentation
├── CHANGELOG.md                # Release notes
└── LICENSE                     # MIT license
```

## Directory Purposes

**src/:**
- Purpose: Python package source code directory
- Contains: `mcp_telegram` subpackage
- Key files: Standard setuptools layout (pyproject.toml references this directory)

**src/mcp_telegram/:**
- Purpose: Core application package
- Contains: MCP server, tool implementations, Telegram integration, CLI
- Key files: `__init__.py` (CLI entry), `server.py` (MCP protocol), `tools.py` (tool logic), `telegram.py` (API client)

## Key File Locations

**Entry Points:**
- `src/mcp_telegram/__init__.py`: CLI entry point via Typer; commands: `run`, `sign_in`, `logout`
- `src/mcp_telegram/server.py`: MCP server entry point `run_mcp_server()` function; handles MCP protocol over stdio
- `cli.py`: Development/debugging entry point; commands: `list-tools`, `call-tool`

**Configuration:**
- `pyproject.toml`: Package name, version (0.1.2), dependencies (mcp, telethon, pydantic, typer, xdg-base-dirs), build system, mypy config
- `ruff.toml`: Linter rules and exclusions
- `.pre-commit-config.yaml`: Automated checks on commit
- `cog.toml`: Changelog generation settings (used for releases)

**Core Logic:**
- `src/mcp_telegram/server.py`: MCP Server initialization, handler registration (@app.list_tools, @app.call_tool), tool dispatcher
- `src/mcp_telegram/tools.py`: Tool definitions (ListDialogs, ListMessages, GetMessage, SearchMessages, GetDialog) as ToolArgs subclasses; tool_runner dispatcher; schema generation functions
- `src/mcp_telegram/telegram.py`: Telegram client factory, login/logout flows, credentials management via TelegramSettings

**Testing:**
- No test files present; testing via `cli.py` manual invocation or MCP Inspector

## Naming Conventions

**Files:**
- Python files: lowercase with underscores (`telegram.py`, `server.py`, `tools.py`, `cli.py`)
- Configuration files: dotfiles or `.toml` extensions (`.pre-commit-config.yaml`, `pyproject.toml`)
- Root entry point for CLI: `cli.py` (not `src/mcp_telegram/cli.py`)

**Directories:**
- Package directories: lowercase (`src/`, `mcp_telegram/`)
- Configuration directories: none present

**Python Classes:**
- Tool argument classes: PascalCase, inherited from ToolArgs (ListDialogs, ListMessages, GetMessage, SearchMessages, GetDialog)
- Base classes: PascalCase with suffix "Args" or "Settings" (ToolArgs, TelegramSettings)

**Python Functions:**
- Async handler functions: snake_case, match tool class name lowercased (`list_dialogs()`, `list_messages()`, `get_message()`, `search_messages()`, `get_dialog()`)
- Utility functions: snake_case (`tool_description()`, `tool_args()`, `tool_runner()`, `create_client()`, `connect_to_telegram()`, `logout_from_telegram()`)

**Python Variables:**
- Constants: UPPERCASE (not present in codebase)
- Module-level: lowercase (`mapping`, `logger`, `app`)
- Local: lowercase with underscores (`response`, `dialog_id`, `dialog`, `iter_messages_args`)

## Where to Add New Code

**New Tool:**
1. Add ToolArgs subclass in `src/mcp_telegram/tools.py`:
   ```python
   class MyNewTool(ToolArgs):
       """Tool description."""
       field1: str
       field2: int = 10
   ```
2. Add @tool_runner.register handler in same file:
   ```python
   @tool_runner.register
   async def my_new_tool(args: MyNewTool) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
       # Implementation
       return [TextContent(type="text", text="result")]
   ```
3. No registration needed; server discovers tools via reflection in `enumerate_available_tools()`

**New Feature (Non-Tool):**
- If Telegram integration: add to `src/mcp_telegram/telegram.py` (e.g., new authentication method)
- If MCP protocol feature: add handler to `src/mcp_telegram/server.py` (e.g., resources, prompts)
- Shared utilities: add function to appropriate module or create new `utils.py` in `src/mcp_telegram/`

**Testing Tool Locally:**
- Use `cli.py`: `uv run cli.py call-tool --name ToolName --arguments '{"field": "value"}'`
- Or use MCP Inspector: `npx @modelcontextprotocol/inspector uv run mcp-telegram`

**Configuration/Secrets:**
- Environment variables: Define in `.env` file or set in shell
- Required: TELEGRAM_API_ID, TELEGRAM_API_HASH
- Optional: Any TelegramSettings env vars prefixed with TELEGRAM_

## Special Directories

**`.planning/`:**
- Purpose: Planning and analysis documents (generated by GSD tools)
- Generated: Yes
- Committed: Yes (contains architecture analysis)

**`__pycache__/`:**
- Purpose: Python bytecode cache
- Generated: Yes (automatic by Python interpreter)
- Committed: No (in .gitignore)

## Package Entry Points

**CLI Command (via pyproject.toml [project.scripts]):**
- `mcp-telegram` → `src/mcp_telegram:app` (Typer app from __init__.py)
- `mcp-telegram-server` → `src/mcp_telegram:app` (backward compatibility)

**MCP Server Command:**
- Invoked from Claude Desktop or Inspector as: `mcp-telegram run`
- Or configured in claude_desktop_config.json as: `"command": "mcp-telegram"`

---

*Structure analysis: 2026-03-11*
