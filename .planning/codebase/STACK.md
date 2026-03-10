# Technology Stack

**Analysis Date:** 2026-03-11

## Languages

**Primary:**
- Python 3.11+ - All source code
- JavaScript/TypeScript - `mcp-proxy` HTTP adapter (npm dependency)

## Runtime

**Environment:**
- Python 3.11.10 (via `mise`)
- Node.js 22 (slim image in Docker)

**Package Manager:**
- `uv` (>=0.4) - Python package manager and tool installer
- `npm` - JavaScript/Node packages (for mcp-proxy)
- Lockfile: `uv.lock` (present)

## Frameworks

**Core:**
- MCP (Model Context Protocol) >=1.1.0 - Server framework for AI assistant integration
- Telethon >=1.23.0 - Telegram API client (MTProto implementation)

**CLI:**
- Typer >=0.15.0 - CLI framework for sign-in, logout, and server run commands

**Data/Config:**
- Pydantic >=2.0.0 - Data validation and models
- Pydantic-settings >=2.6.0 - Environment variable management
- xdg-base-dirs >=6.0.0 - XDG base directory specification support for session storage

**Type Checking:**
- mypy >=1.13.0 (dev only)

## Key Dependencies

**Critical:**
- `telethon` - Directly implements Telegram MTProto protocol; no substitutes without major rewrite
- `mcp` - Server foundation; all tools and responses depend on MCP types
- `pydantic` - Input validation for all tools; ToolArgs definitions require it
- `typer` - CLI entry point; handles sign-in flow and server initialization

**Infrastructure:**
- `mcp-proxy` 6.4.2 (npm) - HTTP/SSE adapter layer wrapping stdio MCP in Docker
- `setuptools` >=70 - Build system for package installation

## Configuration

**Environment:**
- Read via `pydantic-settings` from environment variables with `TELEGRAM_` prefix
- Settings class: `TelegramSettings` in `src/mcp_telegram/telegram.py`
- Required vars: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` (set in docker-compose or Claude Desktop config)
- `.env` file supported but not used in Docker deployment
- Session file location: XDG state directory (`~/.local/state/mcp-telegram/`) with fallback to `xdg_state_home()`

**Build:**
- `pyproject.toml` - Package metadata, dependencies, build backend
- `ruff.toml` - Linting and formatting config (line-length: 120, all rules except D, TRY003, EM101, EM102, TCH)
- `.pre-commit-config.yaml` - Pre-commit hooks (cog for changelog verification)
- `cog.toml` - Version management via Cog, changelog generation

## Platform Requirements

**Development:**
- `uv` tool installed
- Python 3.11+
- For interactive debugging: Node.js + MCP Inspector (`npm install -g @modelcontextprotocol/inspector`)

**Production:**
- Docker (Dockerfile uses multi-stage build)
- Python 3.11+ in runtime image
- Node.js for `mcp-proxy` HTTP adapter
- Port 3100 exposed (docker-compose)
- Telegram session file must be mounted as volume

**Deployment Target:**
- Docker container via `docker-compose up`
- Runs `mcp-proxy --port 8080 -- mcp-telegram run`
- Exposes HTTP MCP transport on port 3100

---

*Stack analysis: 2026-03-11*
