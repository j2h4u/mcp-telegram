# Architecture proposal: hybrid modular monolith

`mcp-telegram` is one deployable modular monolith. It uses vertical capability
boundaries where product ownership is already real, while retaining horizontal
runtime layers for the rest of the daemon. This is a deliberate hybrid, not a
claim that every worker should become a package.

```text
MCP HTTP / Unix socket             delivery transports
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

## Minimal domain/use-case map

The product model is a local Telegram mirror with two deliberately separate
directions:

- **Acquire Telegram facts** — daemon-owned use cases contact Telegram and
  materialize facts into SQLite.
- **Serve agent reads** — MCP/daemon read use cases project SQLite state only;
  they do not contact Telegram.

This is the minimum DDD vocabulary for the current codebase:

| Domain concept | Use case | Telegram access | Local output |
| --- | --- | --- | --- |
| Dialog | `dialog_sync` reconciliation | Dialog metadata, folders, topics | `dialogs`, folder/topic snapshots |
| Message history | `FullSyncWorker` | Backward history batches | `messages`, FTS, aggregate reactions present on message objects |
| Recent gaps | `DeltaSyncWorker` | Forward messages newer than local max id | New `messages`, FTS, aggregate reactions present on message objects |
| Live events | `EventHandlerManager` | Telethon updates while daemon is online | New/edit/delete/read cursor rows; aggregate reaction updates |
| Optional message facts | `message_fact_refresh` | Bounded per-message fact probes | Detailed reaction events/timestamps and exact outgoing-DM `read_at` facts |
| Agent reading | `DaemonReadingService`, `DaemonAPIServer` read handlers | None | Structured MCP responses from local projections |

The important distinction is not "one worker per entity"; it is whether a use
case is acquiring Telegram facts or serving a deterministic local projection.
For example, `DeltaSyncWorker` stores reactions that arrive on newly fetched
message objects, but it does not chase reaction details for older messages:
that is an optional fact refresh job with a separate RPC budget. A reaction can
change on an old message without any new message id appearing, so it is not a
message-gap concern.

The long-term shape may become one acquisition scheduler with multiple job
types (`fetch_new_messages`, `refresh_reaction_details`, `refresh_read_at`).
Until that abstraction is real, separate small use cases are preferred over a
large "Telegram worker" that mixes freshness, history, read-state, and optional
fact semantics.

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
