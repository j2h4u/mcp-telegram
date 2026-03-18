# Technology Stack

**Updated:** 2026-03-18

## Languages

- Python 3.11+ — all source code

## Runtime

- Python 3.11.10 (via `mise`)
- Package manager: `uv` (>=0.4), lockfile: `uv.lock`

## Frameworks

**Core:**
- MCP (Model Context Protocol) >=1.1.0 — server framework for AI assistant integration
- Telethon >=1.23.0 — Telegram API client (MTProto)

**CLI:**
- Typer >=0.15.0 — CLI for sign-in, logout, and server run

**Data/Config:**
- Pydantic >=2.0.0 — data validation and tool argument models
- Pydantic-settings >=2.6.0 — environment variable management
- xdg-base-dirs >=6.0.0 — XDG directory specification

**Dev:**
- mypy >=1.13.0 — type checking (with Pydantic plugin)
- pytest — test framework (280+ tests)
- ruff — linting and formatting

## Key Dependencies

**Critical:**
- `telethon` — Telegram MTProto protocol; no substitutes
- `mcp` — server foundation; all tools depend on MCP types
- `pydantic` — input validation for all tools via ToolArgs

## Configuration

**Environment:**
- Read via `pydantic-settings` with `TELEGRAM_` prefix
- Settings class: `TelegramSettings` in `telegram.py`
- Required: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`
- Session file: `~/.local/state/mcp-telegram/` (XDG state dir)

**Build:**
- `pyproject.toml` — package metadata, dependencies, build system
- `ruff.toml` — linter rules (line-length: 120, all rules except D, TRY003, EM101, EM102, TCH)
- `.pre-commit-config.yaml` — pre-commit hooks

## Deployment

**Production (this machine):**
- Docker container via `docker compose up -d --build`
- Runs `mcp-telegram run` (stdio MCP server)
- Clients connect via `docker exec -i mcp-telegram mcp-telegram run`
- No HTTP transport, no mcp-proxy — pure stdio

**Development:**
- `uv run mcp-telegram run` — direct stdio
- `uv run cli.py list-tools` / `call-tool` — debug CLI
- `npx @modelcontextprotocol/inspector uv run mcp-telegram` — MCP Inspector
