# Architecture cleanup frontier

`tach.toml` is the sole enforced import-graph authority and an enforced green
current-state gate. Every ordinary dependency is an intentional, explicit
collaboration. A Tach
`deprecated = true` edge is the only temporary exception and must remain in
this table until removed.

## Current debt

| ID | Deprecated edge(s) | Owner / target | Removal condition |
|---|---|---|---|
| `ACT-001` | `activity_cold_backfill`, `activity_hot_sweep`, `activity_peer_{resolve,sweep}`, `scheduled_messages` → `activity_sync` | Sync/activity | Extract the client protocol, timeout helper, and own-only SQL into a neutral activity substrate. |
| `ACT-002` | `event_handlers` → `activity_peer_resolve` | Sync/activity | Publish a small input-entity resolver contract and inject it into the handler. |
| `SYNC-001` | `event_handlers` → `sync_worker` | Sync | Move dialog/entity upsert SQL to `sync_db` or an event-write service. |
| `TG-001` | `telegram_fragments` → `messages.sqlite_repository` | Telegram/messages | Inject a message-write port at the fragment ingestion seam. |

Deprecated diagnostics are warnings so the gate stays green, but they are not
approval to add more coupling. A new deprecated edge requires a table row in
the same change with an owner and a concrete removal condition.

## Next cleanup slices

1. `ACT-001` / `ACT-002`: extract the shared activity client and peer resolver
   substrate without inventing a speculative product capability.
2. `SYNC-001`: replace the direct worker reach with a daemon-wired application
   service.
3. `TG-001`: introduce the message write boundary only once the fragment path
   has a real interchangeable dependency.

The READ-001/READ-002 frontier is closed. Reading orchestration and SQLite
projection now live under `mcp_telegram.reading`; scheduled projection imports
the canonical `reading.query_records.read_message_from_row` decoder (the
intentional consumer of that public submodule interface), and the
deleted top-level `daemon_reading`, `daemon_message_queries`,
`daemon_read_state_queries`, and `daemon_scheduled_queries` modules have no
replacement compatibility shims.

## Rules of the gate

- No `unchecked = true`, inline Tach ignores, or `tach sync` normalization.
- `exact = true` requires every declared ordinary dependency to be used.
- Circular dependencies and type-checking imports remain strict.
- No module is marked as a Tach utility. Config, IPC, state, contracts, and
  primitives are explicit modules, not a foundation allowlist.
- `tach.domain.toml` and CODEOWNERS are later work.

Useful checks:

```bash
just module-boundaries
uv run tach check --dependencies --interfaces --exact
just config-imports
```
