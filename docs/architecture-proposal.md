# Architecture proposal: hybrid modular monolith

`mcp-telegram` is one deployable modular monolith. It uses vertical capability
boundaries where product ownership is already real, while retaining horizontal
runtime layers for the rest of the daemon. This is a deliberate hybrid, not a
claim that every worker should become a package.

```text
MCP stdio / HTTP / Unix socket     delivery transports
              │
              ▼
daemon                          composition root
              │
              ▼
sync, reading, activity, events  application/use-case orchestration
              │
     ┌────────┼─────────┐
     ▼        ▼         ▼
 messages   folders   reactions / topics     established capabilities
     │        │         │
Telegram gateways and SQLite repositories   adapters and persistence
```

The daemon is the sole owner of the `TelegramClient`, process lifecycle, and
writable local state. MCP and the Unix API are thin inbound transports: they
translate requests, but neither owns Telegram access nor writes the sync
database directly.

## Established capability boundaries

- **Messages** owns Telethon message extraction and canonical message/FTS
  persistence. It intentionally has no invented repository port.
- **Folders** owns folder contracts, membership matching, refresh orchestration,
  Telegram projection, and SQLite snapshot access.
- **Reactions** owns neutral contracts and ports, freshening, Telegram
  projection, and reaction persistence.
- **Topics** owns neutral contracts and ports, topic refresh, Telegram
  projection, and SQLite snapshot access.

Ports are introduced only at a genuine variable boundary. Keeping a cohesive
SQLite primitive or Telethon projection as an ordinary module is preferable to
creating a placeholder service/repository layer.

## Horizontal layers that remain

Delivery transports, daemon composition, sync/delta/event workers, activity
sweeps, reading/query facades, Telegram gateway adapters, persistence, and
small neutral foundation modules remain horizontal where no stable product
ownership has yet emerged. Tach records their dependencies explicitly rather
than pretending they are vertical capabilities.

Configuration is foundation data loaded by runtime entrypoints and passed into
workers as immutable policy. Application/capability code does not read the
configuration tree for local fallback policy; `check_config_imports.py`
ratchets that rule.

## Invariants

- Delivery may not directly use Telegram or SQLite adapters.
- Application dependencies are explicit and cannot turn concrete adapters into
  general-purpose imports merely because layer order permits them.
- Capability contracts and ports stay transport/storage neutral; daemon wiring
  selects concrete adapters.
- Dependency cleanup is tracked as named Tach deprecations in
  [architecture-cleanup.md](architecture-cleanup.md), not hidden by ignores.

This architecture is enforced as a green current-state frontier. Future work
removes named deprecated edges one slice at a time; it does not normalize them
into broad module allowlists.
