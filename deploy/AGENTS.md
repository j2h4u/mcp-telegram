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

- dotMD integration is archived and stopped; this deployment has no active
  dotMD downstream consumer. The source-export snapshot is reference-only in
  the repository archive and is not part of the runtime contract. Generic
  Unix-socket and HTTP MCP serving remain active.

## Operations

### SQLite backup, restore and retention

Before changing the schema or an incompatible persisted format, stop the
writer daemon and create a consistent matching backup pair of `sync.db` and
`mcp_telegram_session.session` in the operator-owned `backups/` directory.
Use SQLite-aware backups of both databases while the writer daemon is stopped, or
copy their complete stopped database families, including required WAL/SHM
sidecars. Copying only live database files is insufficient. Protect Session
backups as credentials and retain the matching release/configuration reference.

Restore only a matching pair with the writer daemon stopped; first preserve the
current pair. Restoring an older backup loses facts ingested after its capture
and is not a lossless rollback. The Session checkpoint may lag the authoritative
`sync.db` checkpoint, while Session also holds required authentication and peer
facts; neither database replaces the other.

Binary rollback requires compatibility with the current schema, pending-delivery
format and authoritative/safe checkpoint semantics. Healthy startup or a schema
number alone does not establish that compatibility. Do not run an older binary
that ignores pending deliveries or the safe checkpoint, delete pending rows, or
manually rewind offsets.

After the service is healthy, the expected migration version is confirmed, and
the official MCP smoke succeeds, remove only the matching backup pair and
verification sidecars created for that rollout. Keep copies needed for a failed
deployment or rollback until the issue is resolved. Never use a blanket glob
that could remove historical backups or exports. Code-only rebuilds without
schema or incompatible-format changes do not need a new backup.

### Entity Profile rollout and rollback

Roll back the profile pair switch by disabling it and restarting the same
current compatible binary; re-enable it only after that binary restarts
successfully. This configuration rollback does not authorize a binary downgrade:
the schema, pending-delivery and checkpoint compatibility rules above still
apply. Each generation keeps its captured mode, so a disabled generation stays
disabled across restart and a configuration flip. Existing receipts and
progressive sections remain readable.

### Runtime checks

Run the standard runtime checks from the repository root
(`/home/j2h4u/repos/j2h4u/mcp-telegram`):

- Rebuild, restart, wait for Healthy, and run the MCP smoke:
  ```bash
  just runtime-verify
  ```
- Check the already running HTTP service:
  ```bash
  just runtime-smoke
  ```
- Check container status:
  ```bash
  just runtime-status
  ```

Use the official `devtools.mcp_client.cli` only for point diagnostics absent
from the existing recipes; it does not replace the standard MCP smoke.

- Query logs across container recreation:
  ```bash
  journalctl CONTAINER_NAME=mcp-telegram --since '3 days ago' -o short-iso
  ```
  Docker Compose logs cover only the current container. Journald retention is
  controlled by the host; this service does not configure a separate log
  retention policy.

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
