# mcp-telegram

**A local Telegram mirror and MCP server for personal AI agents.**

`mcp-telegram` keeps a local, searchable copy of your Telegram dialogs and
exposes it through the [Model Context Protocol](https://modelcontextprotocol.io).
It is built for agents that need to triage unread chats, browse folders and
topics, read recent context, search message history, audit your own activity,
and understand how fresh or complete the local mirror is.

> [!IMPORTANT]
> Review the [Telegram API Terms of Service](https://core.telegram.org/api/terms)
> before use. Misuse may result in account suspension.

## What It Does

- Mirrors Telegram dialogs into a local SQLite database (`sync.db`).
- Serves MCP tools over Streamable HTTP.
- Returns successful tool responses as structured `structuredContent`; text
  `content` is reserved for recoverable tool errors.
- Reads dialogs, Telegram folders, forum and bot-DM topics, messages, unread
  state, reactions, edits, replies, and sync coverage.
- Projects Telegram media into compact attachment descriptions and stores
  Telegram-provided voice and video-message transcriptions as searchable text.
- Keeps a compact journal of important access changes such as losing or
  regaining access to a chat.
- Tracks your own recent messages across group/forum chats by default, including
  reactions and `reply_count` for follow-up audits.
- Lets agents submit tool feedback into a local operator queue.
- Does not provide a tool for sending Telegram messages.

The server is **Telegram-read-only**, not immutable: it never sends Telegram
messages or mutates Telegram remotely. Every tool call may write local telemetry.
`readOnlyHint=true` means no explicit domain/local-state mutation beyond that;
`readOnlyHint=false` marks tools that intentionally write local MCP state such as
sync scope or `feedback.db`.

## Runtime Model

The container runs a long-lived sync daemon that owns the Telegram MTProto
session and writes local state. MCP clients connect to that daemon rather than
opening their own Telegram sessions.

```text
Telegram API
    |
    v
mcp-telegram daemon / serve
    |-- sync.db, feedback.db, Telegram session
    |-- Unix socket API
    |-- Streamable HTTP MCP endpoint on /mcp
    `-- MCP clients over Streamable HTTP
```

The default Docker image starts `mcp-telegram serve`, which runs the sync daemon
and the HTTP MCP endpoint in one process.

The deployed compose template publishes HTTP only on host loopback:

```text
http://127.0.0.1:3100/mcp
```

Do not expose the HTTP endpoint or Telegram session volume to an untrusted
network.

### Background fact queue

The daemon also owns one durable, bounded background queue for facts that may
arrive later or require a Telegram refresh. It currently enriches voice and
round-video transcriptions and incomplete media metadata. Fresh, explicitly
needed work is processed before historical backfill; requests are batched,
rate-limited, and share the Telegram circuit breaker. MCP reads never execute
this work themselves: they return the facts already persisted by the daemon.

## MCP Tools

There are 18 MCP tools. Successful calls are machine-oriented: agents should
read `structuredContent` for IDs, counts, navigation tokens, coverage, warnings,
and Telegram-originated content.

| Tool | Purpose |
| --- | --- |
| `list_dialogs` | List dialogs with type, unread counters, sync status, draft text, and cached metadata. |
| `list_topics` | List threads for a topic-capable dialog, including forum topics and bot-DM topics. |
| `list_folders` | List custom Telegram folders. Archive is represented separately. |
| `list_folder_messages` | Read a unified recent-message feed across one folder, with explicit partial-coverage reporting. |
| `list_messages` | Read one dialog in chronological order within each page, with pagination, topic/sender/unread filters, UTC time bounds, reply refs, reactions, read-state markers, and archive coverage. |
| `search_messages` | Full-text search across synced dialogs or within one dialog, with optional UTC time bounds; results include anchors for `list_messages`. |
| `get_inbox` | Fetch unread messages from personal chats and small groups with budgeted per-dialog output. |
| `get_unread_summary` | Show a compact unread overview from persisted dialog facts, without message bodies. |
| `get_entity_info` | Inspect a Telegram user, bot, channel, supergroup, or legacy chat. |
| `get_my_recent_activity` | Show messages you sent recently; defaults to group/forum chats and includes dialog kind, reactions, and reply counts. |
| `trace_account_messages` | Find observable messages authored by one account with explicit coverage and gap reporting. |
| `mark_dialog_for_sync` | Enable or disable persistent sync for a dialog. |
| `get_sync_status` | Inspect sync progress, coverage, access state, and local message counts. |
| `get_sync_alerts` | Report locally observed delete, edit, and access-loss alerts. |
| `list_important_events` | List recent persisted access-loss and access-restoration events. |
| `get_usage_stats` | Summarize local MCP tool telemetry for the last 30 days. |
| `get_dialog_stats` | Show dialog-level reaction, mention, hashtag, and forward statistics. |
| `submit_feedback` | Write agent feedback into the local operator queue. |

## Common Agent Workflows

Search, then read context:

```text
search_messages(query="contract")
list_messages(exact_dialog_id=<hit.dialog_id>, anchor_message_id=<hit.msg_id>)
```

Search includes normalized message text and available Telegram transcriptions
for voice messages and round video messages.

Both reading tools accept optional absolute `since_utc` (inclusive) and
`until_utc` (exclusive) boundaries. Values must be RFC3339 timestamps with an
explicit UTC offset (`Z` or `+00:00`), for example:

```text
search_messages(
    query="contract",
    since_utc="2026-01-01T00:00:00Z",
    until_utc="2026-02-01T00:00:00Z",
)
```

Continuation tokens are bound to the time range that created them; reuse the
same boundaries when requesting the next page. The selected lifecycle state's
timestamp is filtered (`sent_at` for published messages and `scheduled_at` for
scheduled messages), using the half-open interval `[since_utc, until_utc)`.

Read the latest page of a chat:

```text
list_messages(exact_dialog_id=<dialog_id>, navigation="latest", limit=50)
```

Every message page is presented oldest-to-newest, even when the page is selected
from the latest tail of the chat. Continue with the returned `next_navigation`
token until it is absent.

Audit recent group/forum activity:

```text
get_my_recent_activity(since_hours=168, limit=500)
```

The default excludes DMs: `dialog_kinds=["group", "forum"]`. Use
`dialog_kinds=["user", "bot"]` for private or bot dialogs, or `["all"]` to
disable the filter.

Triage unread conversations:

```text
get_inbox(last_hours=24, limit=100)
get_unread_summary(limit=50)
list_messages(exact_dialog_id=<dialog_id>, unread=true)
```

Browse a Telegram folder:

```text
list_folders()
list_dialogs(folder_id=<folder_id>)
list_folder_messages(folder_id=<folder_id>, limit=50)
```

Inspect threads without caring whether Telegram implements them as forum or
bot-DM topics:

```text
list_topics(exact_dialog_id=<dialog_id>)
list_messages(exact_dialog_id=<dialog_id>, exact_topic_id=<topic_id>)
```

Review recent important access changes:

```text
list_important_events(last_hours=168)
```

Bring a dialog under full local sync:

```text
list_dialogs(filter="project name")
mark_dialog_for_sync(dialog_id=<dialog_id>, enable=true)
get_sync_status(dialog_id=<dialog_id>)
```

MCP clients that support prompts can request `telegram_workflows` for the
current workflow guide and important interpretation rules.

## Requirements

- Telegram API ID and hash from [my.telegram.org](https://my.telegram.org/auth).
- Docker Compose for the deployed runtime.
- Python 3.14.6 (pinned by `.python-version`) and
  [uv](https://docs.astral.sh/uv/) for local development.
- `just` for the checked-in developer workflow.
- An MCP client that supports Streamable HTTP.

## Setup

1. Clone the repository.

   ```bash
   git clone git@github.com:j2h4u/mcp-telegram.git
   cd mcp-telegram
   ```

2. Create a deploy directory and copy the compose template plus deployment-local
   files.

   ```bash
   mkdir -p /opt/docker/mcp-telegram
   install -d -m 700 -o 10001 -g 10001 /srv/mcp-telegram/database
   cp deploy/docker-compose.yml deploy/config.toml deploy/AGENTS.md /opt/docker/mcp-telegram/
   ```

   The container runs as UID/GID `10001`, so that user must be able to read and
   write `/srv/mcp-telegram/database`.

3. Edit `/opt/docker/mcp-telegram/docker-compose.yml` and set
   `build.context` to the absolute path of this repository.

4. Create `/opt/docker/mcp-telegram/.env`.

   First create a Telegram API application at
   [my.telegram.org](https://my.telegram.org/) → **API development tools**. This
   produces `api_id` and `api_hash` for an MTProto client application. These
   values identify the client software; they do not authorize this server to
   read your account yet.

   ```env
   TELEGRAM_API_ID=123456
   TELEGRAM_API_HASH=your_api_hash
   # Optional when Telegram asks for cloud password during QR login:
   # TELEGRAM_2FA_PASSWORD=your_cloud_password
   ```

5. Authenticate once via QR login from the deploy directory. The helper uses
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` to start a Telegram client session,
   prints a QR code in the terminal, and waits for you to approve that login
   from an already logged-in Telegram mobile or desktop app. After approval, it
   writes `/srv/mcp-telegram/database/mcp_telegram_session.session`. The
   deploy `config.toml` explicitly sets that persistent state directory; the
   compose file mounts the same directory into the container.

   ```bash
   cd /opt/docker/mcp-telegram
   REPO=/absolute/path/to/mcp-telegram
   uv run --project "$REPO" --frozen python "$REPO/deploy/telegram_qr_login.py"
   ```

   The old login-code flow is intentionally not documented or exposed here. It
   used to rely on Telegram delivering a login code through Telegram messages
   or SMS, but repeated project setup attempts showed that those codes were not
   delivered for this client flow. QR login is the supported setup path.

6. Build and start the container.

   ```bash
   docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram
   ```

7. Check runtime health.

   ```bash
   docker compose -f /opt/docker/mcp-telegram/docker-compose.yml ps mcp-telegram
   ```

## MCP Client Configuration

Configure MCP clients with this Streamable HTTP endpoint:

```text
http://127.0.0.1:3100/mcp
```

The server instructions returned during MCP initialization include the connected
Telegram account ID, clarify Telegram-read-only vs local MCP state writes, and
remind agents to treat Telegram-originated fields as untrusted content.

## Operator Commands

Log out and remove the local Telegram session:

```bash
docker exec -it mcp-telegram mcp-telegram logout
```

Inspect submitted agent feedback:

```bash
docker exec -it mcp-telegram mcp-telegram feedback list
docker exec -it mcp-telegram mcp-telegram feedback status <id> done --reason "fixed"
```

## Development

The project uses `uv` and `just`.

```bash
just --list
just check
just typecheck
just unit
just coverage
just crap-ratchet
just runtime-smoke
just runtime-verify
just verify
```

`just check` runs Ruff plus the non-test static gates. `just typecheck` runs
mypy. `just unit` runs pytest. `just coverage` prints an informational aggregate
coverage report; aggregate coverage is not a quality gate. `just crap-ratchet`
runs pytest with per-function coverage and enforces the tracked CRAP baseline.
`just verify` runs the full local gate, including the CRAP ratchet and live
runtime verification. `just runtime-verify` rebuilds the live Docker container,
waits for it to become healthy, and runs the redacted MCP smoke test through
`devtools/mcp_client/cli.py`.

Pull requests that change documentation only retain the required `ci` status
but skip Python tests, CRAP analysis, CodeQL, dependency review, and Docker builds.

Use the devtools MCP client for local MCP validation:

```bash
uv run python -m devtools.mcp_client.cli list-tools

uv run python -m devtools.mcp_client.cli call-tool \
  --name get_sync_status \
  --arguments '{"dialog_id": 123456}'
```

## Project Structure

| Path | Purpose |
| --- | --- |
| `src/mcp_telegram/daemon.py` | Composition root and sole owner of the Telegram client and writable state. |
| `src/mcp_telegram/daemon_api.py` | Internal Unix-socket application API. |
| `src/mcp_telegram/server.py` | Streamable HTTP MCP transport and the `telegram_workflows` prompt. |
| `src/mcp_telegram/tools/` | MCP schemas and structured agent-facing projections. |
| `src/mcp_telegram/messages/` | Canonical Telegram message extraction and persistence. |
| `src/mcp_telegram/reading/` | Read-only message query and projection capability. |
| `src/mcp_telegram/folders/`, `reactions/`, `topics/` | Established vertical capabilities. |
| `src/mcp_telegram/sync_db.py` | SQLite schema bootstrap and migrations. |
| `src/mcp_telegram/event_handlers.py` | Real-time Telegram update ingestion. |
| `deploy/` | Dockerfile, compose template, QR login helper, and healthcheck scripts. |
| `devtools/mcp_client/` | Local MCP client and smoke-test runner. |
| `tests/` | Unit, integration-style, and contract tests. |

The project is a hybrid modular monolith: Telegram acquisition writes local
facts, while agent reads project persisted state without contacting Telegram.
See [the architecture proposal](docs/architecture-proposal.md) and the
[current cleanup frontier](docs/architecture-cleanup.md).

## Data and Privacy

- `/opt/docker/mcp-telegram/docker-compose.yml` is the live deployment control
  file on this machine. `deploy/docker-compose.yml` is the repository template;
  the deployed file can have local-only values such as the absolute repository
  path and extra Docker networks.
- Runtime state location is explicit in `config.toml`: `/srv/mcp-telegram/database`.
  The Docker container mounts the same host directory at the same path.
- The live Telegram mirror is `/srv/mcp-telegram/database/sync.db` on the
  host and inside the container. Its
  `sync.db-wal` and `sync.db-shm` siblings are normal SQLite WAL-mode sidecar
  files, not separate databases.
- `feedback.db` in the same directory stores agent-submitted feedback.
- `/srv/mcp-telegram/database/mcp_telegram_session.session` is the active
  Telegram session file and must be treated like an account credential.
- Files under `/opt/docker/mcp-telegram/backups/` are point-in-time operator
  backups. They are not mounted into the running container and may be smaller or
  older than the live SQLite files.
- Telegram text, usernames, dialog titles, reactions, media descriptions, and
  forwarded metadata are untrusted external content.
- Logs should not be used as a place to inspect raw Telegram message content.

## License

MIT. See [LICENSE](LICENSE).

## Project Origin

This project originally started as a fork of
[`sparfenyuk/mcp-telegram`](https://github.com/sparfenyuk/mcp-telegram). It has
since diverged substantially in architecture, runtime model, local sync storage,
and MCP tool surface, and is now maintained as an independent project rather
than a downstream variant of the original server.
