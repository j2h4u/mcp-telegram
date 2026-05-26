# mcp-telegram

**A local Telegram mirror and MCP server for personal AI agents.**

`mcp-telegram` keeps a local, structured copy of your Telegram dialogs and
exposes it through the [Model Context Protocol](https://modelcontextprotocol.io).
It is built for agents that need to triage unread chats, read recent context,
search message history, audit your own recent activity, and inspect sync
coverage without taking over the Telegram client session.

> [!IMPORTANT]
> Review the [Telegram API Terms of Service](https://core.telegram.org/api/terms)
> before use. Misuse may result in account suspension.

## What It Does

- Mirrors Telegram dialogs into a local SQLite database (`sync.db`).
- Serves MCP tools over stdio and Streamable HTTP.
- Returns successful tool responses as structured `structuredContent`; text
  `content` is reserved for recoverable tool errors.
- Reads dialogs, forum topics, messages, unread inbox state, search results,
  reactions, edits, reply links, and sync coverage.
- Tracks your own recent messages across group/forum chats by default, including
  reactions and `reply_count` for follow-up audits.
- Lets agents submit tool feedback into a local operator queue.
- Does not provide a tool for sending Telegram messages.

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
    |
    v
MCP stdio server via docker exec
```

The default Docker image starts `mcp-telegram serve`, which runs the sync daemon
and the HTTP MCP endpoint in one process. Stdio MCP clients can still use:

```bash
docker exec -i mcp-telegram mcp-telegram run
```

The deployed compose template publishes HTTP only on host loopback:

```text
http://127.0.0.1:3100/mcp
```

Do not expose the HTTP endpoint or Telegram session volume to an untrusted
network.

## MCP Tools

There are 14 MCP tools. Successful calls are machine-oriented: agents should
read `structuredContent` for IDs, counts, navigation tokens, coverage, warnings,
and Telegram-originated content.

| Tool | Purpose |
| --- | --- |
| `list_dialogs` | List dialogs with type, unread counters, sync status, draft text, and cached metadata. |
| `list_topics` | List forum topics for a dialog. |
| `list_messages` | Read one dialog in chronological order within each page, with pagination, topic/sender/unread filters, reply refs, reactions, read-state markers, and archive coverage. |
| `search_messages` | Full-text search across synced dialogs or within one dialog; results include anchors for `list_messages`. |
| `get_inbox` | Fetch unread messages from personal chats and small groups with budgeted per-dialog output. |
| `get_entity_info` | Inspect a Telegram user, bot, channel, supergroup, or legacy chat. |
| `get_my_recent_activity` | Show messages you sent recently; defaults to group/forum chats and includes dialog kind, reactions, and reply counts. |
| `trace_account_messages` | Find observable messages authored by one account with explicit coverage and gap reporting. |
| `mark_dialog_for_sync` | Enable or disable persistent sync for a dialog. |
| `get_sync_status` | Inspect sync progress, coverage, access state, and local message counts. |
| `get_sync_alerts` | Report locally observed delete, edit, and access-loss alerts. |
| `get_usage_stats` | Summarize local MCP tool telemetry for the last 30 days. |
| `get_dialog_stats` | Show dialog-level reaction, mention, hashtag, and forward statistics. |
| `submit_feedback` | Write agent feedback into the local operator queue. |

## Common Agent Workflows

Search, then read context:

```text
search_messages(query="contract")
list_messages(exact_dialog_id=<hit.dialog_id>, anchor_message_id=<hit.msg_id>)
```

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
get_inbox(scope="personal", limit=100)
list_messages(exact_dialog_id=<dialog_id>, unread=true)
```

Bring a dialog under full local sync:

```text
list_dialogs(filter="project name")
mark_dialog_for_sync(dialog_id=<dialog_id>, enable=true)
get_sync_status(dialog_id=<dialog_id>)
```

## Requirements

- Telegram API ID and hash from [my.telegram.org](https://my.telegram.org/auth).
- Docker Compose for the deployed runtime.
- Python 3.14 and [uv](https://docs.astral.sh/uv/) for local development.
- `just` for the checked-in developer workflow.
- An MCP client that can run a stdio command or connect to Streamable HTTP.

## Setup

1. Clone the repository.

   ```bash
   git clone git@github.com:j2h4u/mcp-telegram.git
   cd mcp-telegram
   ```

2. Create a deploy directory and copy the compose template plus QR login script.

   ```bash
   mkdir -p /opt/docker/mcp-telegram
   cp deploy/docker-compose.yml deploy/telegram_qr_login.py /opt/docker/mcp-telegram/
   ```

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
   writes `telegram_session.session` in the deploy directory, which the compose
   file mounts into the container.

   ```bash
   cd /opt/docker/mcp-telegram
   uv run ./telegram_qr_login.py
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

For stdio MCP clients, use this command:

```bash
docker exec -i mcp-telegram mcp-telegram run
```

For Streamable HTTP MCP clients, use:

```text
http://127.0.0.1:3100/mcp
```

The server instructions returned during MCP initialization include the connected
Telegram account ID and remind agents to treat Telegram-originated fields as
untrusted content.

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
just test tests/test_daemon_api.py -q
just runtime-smoke
just runtime-verify
```

`just check` runs Ruff, mypy, and pytest. `just runtime-verify` rebuilds the live
Docker container, waits for it to become healthy, and runs the redacted MCP smoke
test through `devtools/mcp_client/cli.py`.

Use the devtools MCP client for local MCP validation:

```bash
uv run python -m devtools.mcp_client.cli list-tools \
  -- docker exec -i mcp-telegram mcp-telegram run

uv run python -m devtools.mcp_client.cli call-tool \
  --name get_sync_status \
  --arguments '{"dialog_id": 123456}' \
  -- docker exec -i mcp-telegram mcp-telegram run
```

## Project Structure

| Path | Purpose |
| --- | --- |
| `src/mcp_telegram/daemon.py` | Long-running sync daemon and Telegram client owner. |
| `src/mcp_telegram/daemon_api.py` | Unix socket API used by MCP tools and admin commands. |
| `src/mcp_telegram/server.py` | MCP stdio and Streamable HTTP transports. |
| `src/mcp_telegram/tools/` | MCP tool schemas, routing, and structured outputs. |
| `src/mcp_telegram/sync_db.py` | Local SQLite schema and migrations. |
| `src/mcp_telegram/event_handlers.py` | Real-time Telegram update handling. |
| `src/mcp_telegram/activity_sync.py` | Own-message archive used by recent activity audits. |
| `deploy/` | Dockerfile, compose template, QR login helper, and healthcheck scripts. |
| `devtools/mcp_client/` | Local MCP client and smoke-test runner. |
| `tests/` | Unit, integration-style, and contract tests. |

## Data and Privacy

- `/opt/docker/mcp-telegram/docker-compose.yml` is the live deployment control
  file on this machine. `deploy/docker-compose.yml` is the repository template;
  the deployed file can have local-only values such as the absolute repository
  path and extra Docker networks.
- Runtime state lives under the XDG state directory inside the container:
  `/root/.local/state/mcp-telegram`. In Docker, that directory is backed by the
  named volume `mcp-telegram_state`.
- The live Telegram mirror is `/root/.local/state/mcp-telegram/sync.db` inside
  the container. Its `sync.db-wal` and `sync.db-shm` siblings are normal SQLite
  WAL-mode sidecar files, not separate databases.
- `feedback.db` in the same state directory stores agent-submitted feedback.
- `/opt/docker/mcp-telegram/telegram_session.session` is bind-mounted into the
  container as `/root/.local/state/mcp-telegram/mcp_telegram_session.session`.
  This is the active Telegram session file and must be treated like an account
  credential.
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
