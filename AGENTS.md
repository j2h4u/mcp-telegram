# mcp-telegram Agent Notes

## Canon

- Trust source, tests, and live runtime over `.planning/*`. Some planning notes are stale.

## Brownfield Map

- `src/mcp_telegram/tools.py`: main surface; most behavior lives here.
- `src/mcp_telegram/server.py`: MCP stdio server; reflects `ToolArgs` subclasses.
- `src/mcp_telegram/telegram.py`: Telethon client factory and auth flows.
- `src/mcp_telegram/cache.py`: SQLite caches for entities, reactions, topics.
- `src/mcp_telegram/analytics.py`: local telemetry into `analytics.db`.
- `src/mcp_telegram/resolver.py`: fuzzy resolution.
- `src/mcp_telegram/formatter.py`: message formatting.
- `src/mcp_telegram/pagination.py`: cursor encode/decode.
- `cli.py`: local debug entrypoint.

## Current Tools

- `ListDialogs`
- `ListMessages`
- `SearchMessages`
- `GetMyAccount`
- `GetUserInfo`
- `GetUsageStats`

## Tool Pattern

- Add tools in `src/mcp_telegram/tools.py`.
- Normal pattern:
  - `class NewTool(ToolArgs): ...`
  - `@tool_runner.register async def new_tool(args: NewTool): ...`
- Do not wire new tools in `server.py`; discovery is reflection-based.

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
