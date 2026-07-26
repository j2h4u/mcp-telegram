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
| `API-001` | `daemon_api` → `folders.sqlite_repository`, `feedback_db` | Daemon API | Route folder reads and feedback validation through application services/contracts. |
| `READ-001` | `daemon_reading` → `daemon_{account_trace,activity_stats,dialog_queries,message_queries,read_state_queries}` | Reading/query | Stop importing sibling private SQL/constants; make the query facade own its SQL or use explicit public query contracts. |
| `READ-002` | `daemon_scheduled_queries` → `daemon_message_queries` | Reading/query | Move the private row decoder to a shared public query-record module. |
| `SYNC-001` | `event_handlers` → `sync_worker` | Sync | Move dialog/entity upsert SQL to `sync_db` or an event-write service. |
| `TG-001` | `telegram_fragments` → `messages.sqlite_repository` | Telegram/messages | Inject a message-write port at the fragment ingestion seam. |

Deprecated diagnostics are warnings so the gate stays green, but they are not
approval to add more coupling. A new deprecated edge requires a table row in
the same change with an owner and a concrete removal condition.

## Next cleanup slices

1. `READ-001` / `READ-002`: collapse private cross-query SQL into explicit
   query contracts or one cohesive query implementation.
2. `ACT-001` / `ACT-002`: extract the shared activity client and peer resolver
   substrate without inventing a speculative product capability.
3. `API-001` and `SYNC-001`: replace direct persistence/worker reaches with
   daemon-wired application services.
4. `TG-001`: introduce the message write boundary only once the fragment path
   has a real interchangeable dependency.

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
uv run tach check --interfaces
just config-imports
```
