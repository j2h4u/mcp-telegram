# mcp-telegram Deployment Notes

This directory is the live deployment workspace, not the source checkout.

## Runtime Files

- `docker-compose.yml` is the operative compose file for the running service.
- `config.toml` is required. It sets `state.dir`; missing config is a startup/config error.
- `.env` contains Telegram API credentials and must stay private.
- `/srv/mcp-telegram/database/` is bind-mounted at the same path inside the container.
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

Run the repository QR helper from this directory through the locked project
environment, for example:

```bash
REPO=/absolute/path/to/mcp-telegram
uv run --project "$REPO" --frozen python "$REPO/deploy/telegram_qr_login.py"
```

It writes `/srv/mcp-telegram/database/mcp_telegram_session.session` according
to `config.toml`, which is the same state directory the container uses. The
retired Telegram-message/SMS login-code path is intentionally not used because
repeated setup attempts did not receive codes.
