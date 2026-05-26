# mcp-telegram Deployment Notes

This directory is the live deployment workspace, not the source checkout.

## Runtime Files

- `docker-compose.yml` is the operative compose file for the running service.
- `.env` contains Telegram API credentials and must stay private.
- `database/` is bind-mounted to `/root/.local/state/mcp-telegram` inside the container.
- `database/sync.db` is the live Telegram mirror.
- `database/feedback.db` stores agent-submitted feedback.
- `database/mcp_telegram_session.session` is the active Telegram account session.
- `database/*-wal` and `database/*-shm` are normal SQLite WAL-mode sidecar files.
- `backups/` contains point-in-time operator backups only; it is not mounted into the container.

## Downstream Consumers

- dotMD is the external Telegram indexer/search engine. It reaches this
  deployment through dotMD's Telegram adapter/source integration.
- On this machine dotMD mounts `database/` read-only. If this deployment path
  changes, update `/opt/docker/dotmd/docker-compose.override.yml` as well.

## Operations

- Rebuild and restart after source changes:
  ```bash
  docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram
  ```
- Check health:
  ```bash
  docker compose -f /opt/docker/mcp-telegram/docker-compose.yml ps mcp-telegram
  ```
- Validate MCP over stdio from the source checkout:
  ```bash
  uv run python -m devtools.mcp_client.cli list-tools \
    -- docker exec -i mcp-telegram mcp-telegram run
  ```

## Auth

Run `telegram_qr_login.py` from this directory. It writes
`database/mcp_telegram_session.session`, which is the same path the container
uses. The retired Telegram-message/SMS login-code path is intentionally not
used because repeated setup attempts did not receive codes.
