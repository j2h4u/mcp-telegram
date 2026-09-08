# Architecture cleanup frontier

`tach.toml` is the sole enforced import-graph authority and an enforced green
current-state gate. Every ordinary dependency is an intentional, explicit
collaboration. A Tach
`deprecated = true` edge is the only temporary exception and must remain in
this table until removed.

## Current debt

| ID | Deprecated edge(s) | Owner / target | Removal condition |
|---|---|---|---|
| `TG-001` | `telegram_fragments` → `messages.sqlite_bundle` | Telegram/messages | Inject a message-write port at the fragment ingestion seam. |

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

## Telegram transport boundary

Application work uses the daemon-owned Telegram facade. Supported RPC attempts
follow one transport path:

```text
domain worker or daemon API
    -> operation source and service class
    -> bounded RPC admission arbiter
    -> account rate limit, cooldown, and circuit breaker
    -> Telethon transport
```

The arbiter governs application RPCs and Telethon update/difference work. A
worker may use public facade methods such as `get_messages()` or the callable
request seam, but it must not construct a client, call Telethon's private
`_call` or `_sender.send`, or invoke an unbound superclass implementation.
The boundary check scans `src/mcp_telegram` and rejects those bypasses; tests,
the deploy helper, and archived/vendor material are separate surfaces.

The narrow lifecycle allowlist is:

- `telegram_rpc.py`: the `TelegramRpcGate` Telethon subclass owns the
  superclass dispatch and private sender seam.
- `telegram.py`: `create_client()` is the sole production
  `TelegramRpcGate` constructor. `create_maintenance_client()` may construct
  a plain client for the explicit `logout` command, whose `connect()` and
  `log_out()` calls remain outside application admission.
- `daemon.py`: `_connect_telegram()`,
  `_monitor_flood_wait_kill_switch()`, and
  `_shutdown_sync_main_context()` own daemon connection and disconnection.
- `deploy/telegram_qr_login.py`: the standalone operator login helper owns
  authentication, QR, and connection-control packets; it is not an
  application RPC consumer.

These lifecycle and authentication packets are documented exceptions. They do
not authorize a new application client or a second account limiter.

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
