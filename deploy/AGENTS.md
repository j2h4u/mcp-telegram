# mcp-telegram Deployment Notes

This directory is the live deployment workspace, not the source checkout.

## Runtime Files

- `docker-compose.yml` is the operative compose file for the running service.
- `.env` contains Telegram API credentials and must stay private.
- `/srv/mcp-telegram/database/` is bind-mounted to `/root/.local/state/mcp-telegram` inside the container.
- `/srv/mcp-telegram/database/sync.db` is the live Telegram mirror.
- `/srv/mcp-telegram/database/feedback.db` stores agent-submitted feedback.
- `/srv/mcp-telegram/database/mcp_telegram_session.session` is the active Telegram account session.
- `/srv/mcp-telegram/database/*-wal` and `/srv/mcp-telegram/database/*-shm` are normal SQLite WAL-mode sidecar files.
- `backups/` contains point-in-time operator backups only; it is not mounted into the container.

## Downstream Consumers

- dotMD is the external Telegram indexer/search engine. It reaches this
  deployment through dotMD's Telegram adapter/source integration.
- On this machine dotMD mounts `/srv/mcp-telegram/database/` read-only. If this deployment path
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
`/srv/mcp-telegram/database/mcp_telegram_session.session` by default, which is the
same state directory the container uses. Set `MCP_TELEGRAM_STATE_DIR` only if this
host uses a different durable data root. The retired Telegram-message/SMS login-code
path is intentionally not used because repeated setup attempts did not receive codes.
