# mcp-telegram Agent Notes

## Canon

- Trust source, tests, and live runtime over `.planning/*`.

## Brownfield Map

- `src/mcp_telegram/tools/`: tool package — `_base.py` (ToolArgs, ToolResult, mcp_tool, TOOL_REGISTRY, singledispatch, telemetry), `discovery.py`, `reading.py`, `unread.py`, `user_info.py`, `stats.py`.
- `src/mcp_telegram/server.py`: MCP stdio server; iterates `TOOL_REGISTRY` for tool listing.
- `src/mcp_telegram/telegram.py`: Telethon client factory and auth flows.
- `src/mcp_telegram/cache.py`: SQLite caches for entities, reactions, topics.
- `src/mcp_telegram/capabilities.py`: re-export shim for split capability modules.
- `src/mcp_telegram/capability_history.py`: history read orchestration.
- `src/mcp_telegram/capability_search.py`: search orchestration.
- `src/mcp_telegram/capability_topics.py`: topic listing orchestration.
- `src/mcp_telegram/models.py`: shared dataclasses, TypedDicts, type aliases.
- `src/mcp_telegram/budget.py`: unread tier classification, budget allocation.
- `src/mcp_telegram/dialog_target.py`: dialog resolution orchestration.
- `src/mcp_telegram/forum_topics.py`: topic catalog loading, topic resolution.
- `src/mcp_telegram/message_ops.py`: message fetch helpers, reply/reaction maps.
- `src/mcp_telegram/errors.py`: action-oriented error text functions.
- `src/mcp_telegram/resolver.py`: fuzzy resolution (Cyrillic + transliteration).
- `src/mcp_telegram/formatter.py`: message formatting.
- `src/mcp_telegram/pagination.py`: cursor encode/decode.
- `src/mcp_telegram/analytics.py`: local telemetry into `analytics.db`.
- `cli.py`: local debug entrypoint.

## Current Tools

- `ListDialogs` — list dialogs (chats, channels, groups)
- `ListMessages` — read messages in one dialog (with pagination, topic, sender, unread filters)
- `SearchMessages` — search messages in a dialog by text query
- `ListTopics` — list forum topics in a dialog
- `ListUnreadMessages` — fetch unread messages across chats, prioritized by tier
- `GetMyAccount` — get current authenticated user info
- `GetUserInfo` — look up a user by name (fuzzy match + common chats)
- `GetUsageStats` — get usage statistics from telemetry (last 30 days)

## Tool Pattern

- Add tools in `src/mcp_telegram/tools/` (pick the appropriate domain module, or create a new one).
- Normal pattern:
  - `class NewTool(ToolArgs): ...`
  - `@mcp_tool("primary") async def new_tool(args: NewTool) -> ToolResult: ...`
- Import the module in `src/mcp_telegram/tools/__init__.py` so it registers at import time.
- Do not wire new tools in `server.py`; discovery iterates `TOOL_REGISTRY`.

## State

- This is read-only against Telegram, not stateless.
- XDG state dir stores Telegram session, `entity_cache.db`, and `analytics.db`.
- `create_client()` is process-cached. Cache and telemetry objects are singleton-like within process lifetime.

## Testing

- Full suite: `uv run pytest`
- Focused topic slice: `uv run pytest tests/test_tools.py -k topic -v`
- Manual debug:
  - `uv run cli.py list-tools`
  - `uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"..."}'`

## Runtime On This Machine

- Source repo: `/home/j2h4u/repos/j2h4u/mcp-telegram`
- Deploy project: `/opt/docker/mcp-telegram`
- Compose build uses this repo via `additional_contexts.src=/home/j2h4u/repos/j2h4u/mcp-telegram`
- Runtime container: `mcp-telegram`
- Clients commonly hit a long-lived container and start the MCP server through `docker exec ... mcp-telegram run`

## Runtime Discipline

- After any runtime-affecting change: update runtime, not just code.
- Rebuild when needed.
- Always restart the runtime.
- Do not mark work done until the restarted runtime is verified to expose the new behavior/schema.
- Safe update path here:
  - `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram`
  - then verify inside container

## Lessons Learned

- Green tests do not prove the live runtime is current; stale containers can serve old tool schemas.
- Forum-topic support is test-covered, but live Telegram semantics can still require manual validation.
- Avoid logging Telegram message content or other sensitive data.
- Treat Telegram session files and `.env` credentials as secrets.
