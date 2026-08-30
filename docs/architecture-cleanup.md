# Architecture cleanup frontier

`tach.toml` is the sole enforced import-graph authority and an enforced green
current-state gate. Every ordinary dependency is an intentional, explicit
collaboration. A Tach
`deprecated = true` edge is the only temporary exception and must remain in
this table until removed.

## Current debt

| ID | Deprecated edge(s) | Owner / target | Removal condition |
|---|---|---|---|
| `TG-001` | `telegram_fragments` → `messages.sqlite_repository` | Telegram/messages | Inject a message-write port at the fragment ingestion seam. |

Deprecated diagnostics are warnings so the gate stays green, but they are not
approval to add more coupling. A new deprecated edge requires a table row in
the same change with an owner and a concrete removal condition.

## Next cleanup slices

1. `TG-001`: introduce the message write boundary only once the fragment path
   has a real interchangeable dependency.

The SYNC-001 frontier is closed. Runtime entity writers use the canonical,
transaction-neutral `entity_store` persistence boundary, and `event_handlers`
no longer imports `sync_worker`.

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
