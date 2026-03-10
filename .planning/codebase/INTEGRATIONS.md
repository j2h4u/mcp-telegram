# External Integrations

**Analysis Date:** 2026-03-11

## APIs & External Services

**Telegram:**
- Telegram MTProto API - Core integration for accessing Telegram messages, dialogs, and chat metadata
  - SDK/Client: `telethon` >=1.23.0
  - Auth: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` environment variables
  - Transport: Direct TCP connection to Telegram servers via MTProto protocol
  - Session: File-based session stored at `~/.local/state/mcp-telegram/mcp_telegram_session.session`

## Data Storage

**Databases:**
- No persistent database - read-only access to Telegram
- All data accessed on-demand from Telegram servers

**File Storage:**
- Session file: `telegram_session.session`
  - Location: `~/.local/state/mcp-telegram/` (XDG state directory)
  - Purpose: Stores authenticated session token (long-lived bearer credential)
  - In Docker: Mounted as volume at `/root/.local/state/mcp-telegram/mcp_telegram_session.session`
  - Sensitivity: **Private** - Contains session auth token; stolen file enables full Telegram account access without 2FA

**Caching:**
- None - all queries hit Telegram API directly

## Authentication & Identity

**Auth Provider:**
- Telegram account credentials (phone number + code + optional 2FA)
- Two login modes:
  1. Interactive via `mcp-telegram sign-in --api-id ... --api-hash ... --phone-number ...`
  2. QR login via `telegram_login.py` script (supports 2FA)

**Implementation:**
- Telethon handles OTP code verification and password (2FA) prompts
- Session persisted to file after successful login
- No refresh tokens - session file is the bearer credential
- Environment: `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` (app credentials, NOT user credentials)

## Monitoring & Observability

**Error Tracking:**
- None - no external error tracker
- Logging: Python `logging` module to stderr (telethon base logger configured)

**Logs:**
- Console/stderr via Python logging
- In Docker: captured by docker daemon logs

## CI/CD & Deployment

**Hosting:**
- Docker container deployed via `docker compose`
- Port: 3100 (forwarded to 8080 inside container)
- Exposed endpoints:
  - `/mcp` - MCP protocol endpoint (streamable HTTP)
  - `/sse` - SSE transport endpoint
- Container: `mcp-telegram` (built from `Dockerfile`)

**CI Pipeline:**
- None configured in this repo
- Pre-commit hooks: `cog verify` for changelog consistency
- Version management: `cog` for semantic versioning

**Build Process:**
- Multi-stage Docker build:
  1. Builder stage: installs mcp-telegram via `uv tool install` from local repo
  2. Runtime stage: copies venv + node:22-slim base + mcp-proxy
- Source mounted via `additional_contexts: src:` from `/home/j2h4u/repos/j2h4u/mcp-telegram`

## Environment Configuration

**Required env vars:**
- `TELEGRAM_API_ID` - Telegram app ID (get from https://my.telegram.org/auth)
- `TELEGRAM_API_HASH` - Telegram app hash (get from https://my.telegram.org/auth)

**Optional env vars:**
- None currently

**Secrets location:**
- Docker: `.env` file (symlinked from `~/.secrets/` in production)
- Development: `.env` or environment variables
- **Session file**: mounted separately, NOT in .env
- Note: Session file is the actual credential - `.env` only has app credentials

## Webhooks & Callbacks

**Incoming:**
- None - pure read-only client, no webhooks received

**Outgoing:**
- None - no external API calls except to Telegram

## HTTP Transport

**Adapter:**
- `mcp-proxy` (npm package, v6.4.2) wraps stdio MCP server in HTTP/SSE
- Command: `mcp-proxy --port 8080 -- mcp-telegram run`
- Transports:
  - HTTP MCP (streamable) on `/mcp` path
  - SSE on `/sse` path

**Security Notes:**
- Currently **no application-layer authentication** on HTTP endpoint
- Any client with network access to port 3100 can read Telegram messages
- Session file is long-lived bearer token - compromise is equivalent to account compromise
- Recommended hardening: add `--apiKey` to mcp-proxy or reverse proxy with auth

---

*Integration audit: 2026-03-11*
