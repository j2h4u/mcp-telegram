# mcp-telegram Agent Notes

## Canon

- Trust source, tests, and live runtime over `.planning/*`.

## Architecture

Two-process model running inside a Docker container:

- **Daemon** (`mcp-telegram sync`) — PID 1; owns the TelegramClient exclusively, runs FullSyncWorker
  and DeltaSyncWorker, handles real-time events, exposes a Unix socket API.
- **MCP server** (`mcp-telegram run`) — started on demand via `docker exec`; connects to the daemon
  over the Unix socket, translates tool calls into daemon API requests.

State lives in `sync.db` (XDG state home) plus the Telegram session file. The daemon is the only
writer; the MCP server opens `sync.db` read-only for lightweight queries.

## Brownfield Map

### Core
- `daemon.py` — `sync_main()` entry point; owns TelegramClient, heartbeat loop, gap scan scheduling
- `daemon_api.py` — Unix socket server; 14+ API methods; `_build_list_messages_query()` dynamic SQL builder
- `daemon_client.py` — Unix socket client used by MCP tool runners
- `sync_db.py` — `sync.db` schema + migrations; `open_sync_db()` / `open_sync_db_readonly()`
- `sync_worker.py` — `FullSyncWorker`: batch history fetch, FloodWait handling, checkpoint progress
- `delta_sync.py` — `DeltaSyncWorker`: gap-fill, catch-up, access-loss detection
- `event_handlers.py` — `EventHandlerManager`: real-time NewMessage / Edited / Deleted via Telethon events
- `read_state.py` — `apply_read_cursor()`: monotonic inbox/outbox read cursor writes to `synced_dialogs`
- `fts.py` — FTS5 full-text search with Russian snowball stemming
- `telegram.py` — TelegramClient factory and auth flows
- `__init__.py` — CLI entrypoint: `sign-in`, `run`, `logout`, `sync`

### Shared Utilities
- `models.py` — TypedDict schemas, dataclasses (`StoredMessage`, `ReadMessage`)
- `budget.py` — message budget allocation for tool responses
- `resolver.py` — fuzzy name resolution (anyascii + Cyrillic transliteration); single match auto-resolves
- `formatter.py` — `format_messages()` with `[edited HH:mm]`, media, reactions
- `pagination.py` — `NavigationToken`, `HistoryDirection` StrEnum, encode/decode
- `errors.py` — structured error types
- `server.py` — MCP stdio server; iterates `TOOL_REGISTRY` for tool listing

### Deploy (`deploy/`)
- `Dockerfile` — multi-stage build; copies source via `--from=src additional_contexts`
- `docker-compose.yml` — template with path placeholders
- `scripts/healthcheck_daemon.py` — Unix socket healthcheck (copied into image)
- `scripts/healthcheck_all.sh` — healthcheck entrypoint (copied into image)
- `telegram_qr_login.py` — QR-based auth (repo root, SMS method unreliable); run from deploy dir to produce `telegram_session.session`

### Tools Package (`tools/`)
- `_base.py` — `ToolArgs`, `ToolResult`, `@mcp_tool`, `TOOL_REGISTRY`, `daemon_connection`, telemetry
- `activity.py` — `get_my_recent_activity`
- `discovery.py` — `list_dialogs`, `list_topics`
- `entity_info.py` — `get_entity_info` (universal entity inspector: User/Bot/Channel/Supergroup/LegacyChat)
- `feedback.py` — `submit_feedback` (write tool — agents report bugs/suggestions; daemon writes to feedback.db)
- `reading.py` — `list_messages`, `search_messages`
- `stats.py` — `get_usage_stats`, `get_dialog_stats`
- `sync.py` — `mark_dialog_for_sync`, `get_sync_status`, `get_sync_alerts`
- `unread.py` — `get_inbox`

Canonical tool registry: `tools/__init__.py`. Total: 13 MCP tools.

## Tool Pattern

```python
from ._base import ToolArgs, ToolResult, mcp_tool, text_result
from mcp.types import ToolAnnotations

class NewTool(ToolArgs):
    """Description shown to the LLM."""
    field: str

@mcp_tool(
    name="new_tool",
    title="New Tool",
    posture="primary",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def new_tool(args: NewTool) -> ToolResult:
    async with daemon_connection() as conn:
        ...
    return text_result("...")
```

- Add to the appropriate domain module (or create a new one).
- Import in `tools/__init__.py` — registration happens at import time; `server.py` discovers via `TOOL_REGISTRY`.
- Use `"primary"` for user-facing tools, `"secondary/helper"` for supporting tools.

## Feedback queue

Agents submit feedback via the `submit_feedback` MCP tool; the daemon
persists rows in `feedback.db` (XDG state dir, alongside `sync.db`).
Operator manages the queue with:

- `mcp-telegram feedback list [--limit N]` — print recent entries (most-recent-first)
- `mcp-telegram feedback status <id> <status> [--reason TEXT]` — move a row through `open`,
  `in_progress`, `done`, or `dismissed`

No MCP read tool exists for feedback by design — agents submit, operator
reviews. Source: `src/mcp_telegram/feedback_db.py`,
`src/mcp_telegram/tools/feedback.py`, `src/mcp_telegram/__init__.py`.

## Testing

```bash
uv run pytest                              # full suite
uv run pytest tests/test_daemon_api.py -v  # focused
```

## Вызов MCP tools (devtools-клиент)

**Всегда используй `devtools/mcp_client/cli.py` для ad-hoc проверок и E2E-валидации — не `docker exec python3` и не самодельные скрипты.**

Запускается из корня репозитория (`~/repos/j2h4u/mcp-telegram/`):

```bash
# Список доступных tools
uv run python -m devtools.mcp_client.cli list-tools \
  -- docker exec -i mcp-telegram mcp-telegram run

# Разовый вызов tool'а
uv run python -m devtools.mcp_client.cli call-tool \
  --name get_sync_status \
  --arguments '{"dialog_id": 228055330}' \
  -- docker exec -i mcp-telegram mcp-telegram run

# Запуск smoke-теста из JSON-файла
uv run python -m devtools.mcp_client.cli script \
  --file devtools/mcp_client/smoke-integration.json \
  -- docker exec -i mcp-telegram mcp-telegram run
```

Требует живого daemon (контейнер должен быть Healthy).

## Runtime On This Machine

- Source repo: `/home/j2h4u/repos/j2h4u/mcp-telegram`
- Deploy project: `/opt/docker/mcp-telegram`
- Compose build pulls source via `additional_contexts.src`
- Rebuild after code changes:
  ```bash
  docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram
  ```

## Ownership

The agent owns the full development cycle end to end: writing code, running tests, rebuilding the container, verifying live behavior, and reporting results. Nothing is handed off to the operator for execution. If something requires a command to be run, the agent runs it.

## Runtime Discipline

- After any runtime-affecting change: rebuild the container and verify live behavior — do not hand off to the operator.
- Do not mark work done until the restarted runtime exposes the expected behavior.
- Green tests do not prove the live container is current — stale containers serve stale schemas.

## Lessons Learned

- Forum-topic support is test-covered, but live Telegram semantics require manual validation.
- Avoid logging Telegram message content or other sensitive data.
- Treat session files and `.env` credentials as secrets.
