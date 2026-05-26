# mcp-telegram

A read-only Telegram bridge for AI assistants, built on the [Model Context Protocol](https://modelcontextprotocol.io).

> [!IMPORTANT]
> Review the [Telegram API Terms of Service](https://core.telegram.org/api/terms) before use.
> Misuse may result in account suspension.

## Architecture

Two-process model:

- **Daemon** (`mcp-telegram sync`) — owns the TelegramClient, syncs messages to `sync.db`, runs as PID 1 in the container
- **MCP server** (`mcp-telegram run`) — connects to the daemon via Unix socket on demand, exposes tools over stdio

Deployed as a Docker container. MCP clients connect by running `docker exec -i mcp-telegram mcp-telegram run`.

## Tools

All 14 tools expose `outputSchema`. Successful calls are structured-only: agents
should read `structuredContent` for ids, counts, navigation, coverage, warnings, and
result facts. Successful `content` is empty; concise text content is reserved for
recoverable `isError=true` tool errors.

- `list_dialogs` — list chats, channels, groups with unread counts
- `list_topics` — list forum topics
- `list_messages` — read messages in a dialog (pagination, topic, sender, unread filters)
- `search_messages` — full-text search within a dialog or across all synced dialogs
- `get_inbox` — fetch unread messages across chats, prioritized by tier
- `get_entity_info` — inspect users, bots, channels, supergroups, and legacy chats
- `get_usage_stats` — local telemetry (last 30 days)
- `get_dialog_stats` — local per-dialog activity statistics
- `mark_dialog_for_sync` — enable or disable dialog sync
- `get_sync_status` — inspect sync status for one dialog
- `get_sync_alerts` — inspect recent sync alerts
- `get_my_recent_activity` — summarize the operator's recent sent messages
- `submit_feedback` — write-only agent feedback queue
- `trace_account_messages` — trace observable messages authored by one account

## Deploy

The `deploy/` directory contains everything needed to run the container:

- `Dockerfile` — multi-stage build; takes source from the cloned repo via `additional_contexts`
- `docker-compose.yml` — template; fill in paths to your repo clone and deploy directory
- `scripts/` — healthcheck scripts (copied into the image at build time)
- `telegram_qr_login.py` — one-time auth script (repo root); run it in your deploy directory to create `telegram_session.session`

Create a deploy directory with `.env` (containing `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`), adapt `docker-compose.yml`, then build with `docker compose up -d --build`.

## Setup

1. Get an API ID and hash at [my.telegram.org/auth](https://my.telegram.org/auth) → API Development tools → Create application
2. Authenticate via QR code using `telegram_qr_login.py` (repo root) — the SMS code method (`mcp-telegram sign-in`) is unreliable as Telegram often does not deliver the code
3. To log out: `docker exec -it mcp-telegram mcp-telegram logout`

## Development

See `AGENTS.md` for codebase map, tool patterns, and runtime discipline.

## Project origin

This project originally started as a fork of
[`sparfenyuk/mcp-telegram`](https://github.com/sparfenyuk/mcp-telegram). It has
since diverged substantially in architecture, runtime model, local sync storage,
and MCP tool surface, and is now maintained as an independent project rather
than a downstream variant of the original server.
