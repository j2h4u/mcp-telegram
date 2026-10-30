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

The gateway safety receipt is rollback-incompatible even while the SQLite
schema remains 89. Operational configuration must contain
`[telegram_rpc] transport_contract_version = 1`; keep that marker during a
binary rollback. An older binary may run only after an explicit compatibility
review establishes that it will preserve every unresolved attempt and known
restriction. Prefer a compatible protected binary or forward fix. Do not
restore an old database pair to erase a restriction or discard newer facts.

For this cutover, stop every daemon and standalone authentication owner before
backing up the pair and changing configuration. Hold the stable
`telegram-session.lock` throughout that offline interval, and never replace or
unlink its inode. Old binaries and copied raw authentication helpers do not
honor this lease; replace the deployed QR helper with the current repository
wrapper before allowing maintenance use.

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

Stop the service first. Run the repository QR helper from this directory through the locked project
environment, for example:

```bash
REPO=/absolute/path/to/mcp-telegram
uv run --project "$REPO" --frozen python "$REPO/deploy/telegram_qr_login.py"
```

It writes `/srv/mcp-telegram/database/mcp_telegram_session.session` according
to `config.toml`, which is the same state directory the container uses. The
retired Telegram-message/SMS login-code path is intentionally not used because
repeated setup attempts did not receive codes.

The helper shares the gateway, safety receipt and exclusive Session lease with
the daemon. An already authorized key is retained by default. Explicit
`--reauthenticate` replaces local credentials while preserving mirror facts
and Session peer/update rows; it does not remotely revoke the old Telegram
authorization. Existing uncertain outcomes or manual holds require the
separate `--acknowledge-unknown-outcome` option, and known server waits still
apply. Authentication does not authorize normal acquisition past those holds.

Automatic recovery is enabled by default; `[flood_wait] auto_resume_enabled`
can disable future trials without clearing restrictions or spent attempts.
Confirmed FloodWait has six waits of 1/2/4/8/16/24 hours; transport and
retryable server failures use 30-second to 30-minute backoff, and eligible
unknown read outcomes use five-minute to one-hour backoff. Known server floors
and previously incurred waits still apply. Interrupted trials remain spent.
Unknown mutations, unclassified methods, invalid account ownership or storage,
unresolved authentication and exhausted FloodWait attempts remain hard holds.
Automatic policy disposition retains unknown outcomes and is not manual
acknowledgement. Health remains degraded during protection while retained local
reads stay available.

The explicit `mcp-telegram recover-account --acknowledge-unknown-outcome`
command requests one conditional trial for a verified account after known waits.
Authorization and a scheduled restart do not establish successful recovery.

Automatic episode metadata extends the existing gateway safety receipt;
older readers reject that extension. Disable on the current compatible binary
for rollback; never remove the receipt, restore stale data, or downgrade to a
reader that cannot preserve the episode and its floors. Follow the stopped
matching-backup procedure above before this persisted-format cutover.
