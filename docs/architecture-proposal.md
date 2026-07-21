# Architecture proposal: modular monolith → Ports & Adapters

## Current state (PR #33)

`mcp-telegram` is a **modular monolith moving toward Ports & Adapters**. It is
not yet a set of fully verticalized capabilities, and it should not be
described as one. The daemon remains the composition root and lifecycle owner:
it creates the Telegram client, opens the writable state, wires adapters to
application services, and runs background work. MCP and the Unix API are
inbound transports, not owners of the runtime.

The first slices are real, but deliberately narrow:

- `messages/telegram_adapter.py` extracts Telethon-shaped messages into the
  existing `message_contracts` records; `messages/sqlite_repository.py` owns
  canonical message/FTS and child-projection writes via
  `insert_messages_with_fts()`.
- `reactions/contracts.py` contains transport- and storage-neutral reaction
  values. `reactions/ports.py` defines `TelegramReactionGateway` and
  `ReactionSnapshotRepository`; `reactions/refresh.py` contains the
  `ReactionFreshener` application service.
- `reactions/telegram_adapter.py` implements the Telegram port with
  `TelethonTelegramReactionGateway`; `reactions/sqlite_repository.py`
  implements the snapshot repository. `reactions/persistence.py` owns the
  transaction-neutral `replace_reaction_aggregates()` primitive.
- `TelegramHistoryGateway` already exists for history reads. There is
  intentionally **no `MessageRepository` port**: message persistence is still
  the concrete `messages/sqlite_repository.py` shape, so the architecture must
  not invent a port merely to complete a diagram.

Import-linter protects boundaries for these first message/reaction slices and
some existing layers. It is a ratchet, not a global proof that every capability
already follows the target structure. `daemon.py`, `daemon_reading.py`, and
`daemon_account_trace.py` are representative residual horizontal modules.

## Target model

```text
MCP / Unix API / daemon schedules
                │
                ▼
       capability application service
                │
        ┌───────┴────────┐
        ▼                ▼
  Telegram gateway     repository port
        │                │
        ▼                ▼
 Telethon adapter    SQLite adapter
```

The daemon remains the composition root in this model. It is allowed to know
the concrete adapters and configuration; capability code is not. A port is
introduced only at a genuine variable boundary, such as Telegram I/O or a
repository required by an application service. Pure extraction, projection,
and SQL ownership can remain ordinary modules.

An illustrative target tree must start from the modules that actually exist,
not from generic `service`/`repository` placeholders:

```text
messages/
  telegram_adapter.py
  sqlite_repository.py

reactions/
  contracts.py
  ports.py
  projection.py
  refresh.py
  persistence.py
  telegram_adapter.py
  sqlite_repository.py

# Later, selectively: reading/, account_trace/, activity/, dialogs/, ...
```

The last line is a destination, not a claim that those capabilities are already
vertical modules.

## Configuration is a hard architectural boundary

All operator-configurable runtime policy and its defaults belong in
`config.py`, in a hierarchical immutable
`@dataclass(frozen=True, slots=True)` configuration tree. Composition roots
alone load that tree. They pass an explicit, immutable, default-free policy
object to a capability or application service; capability code does not read
configuration directly and does not install a local fallback value.

This rule concerns **operator policy**: retention, freshness, timeouts,
limits, scheduling, concurrency, and similar runtime choices. It does not
reclassify protocol constants, domain invariants, or request-specific values as
operator configuration.

The enforcement is intentionally incremental:

- `scripts/check_config_imports.py` ratchets direct `config` imports to
  composition roots;
- import-linter protects declared package boundaries;
- `scripts/check_policy_placement.py` checks reviewed policy placement and
  injected-policy shape.

The allowlist and placement checker are not evidence of whole-runtime policy
provenance. Each new policy path still needs a design decision and a boundary
test.

## Migration checklist

- [x] 1. Establish the first message/reaction module boundaries and make their
  responsibilities explicit.
- [x] 2. Introduce the initial Telegram and reaction-repository ports and their
  concrete adapters.
- [x] 3. Complete reaction persistence ownership in substance:
  `replace_reaction_aggregates()` is owned by reaction persistence and both
  message ingestion and the reaction snapshot repository use it.
- [x] 4. Keep just-in-time reaction refresh as the port-driven
  `ReactionFreshener` application service.
- [x] 5. Enforce import directions for the first message/reaction slices only;
  this is not yet a project-wide capability boundary.
- [ ] 6. Move the remaining horizontal reading, account-trace, sync, activity,
  and related flows one capability at a time, with their tests and composition
  wiring.

## Final organization invariant

The end state is still one deployable daemon-backed service. Capabilities are
organized vertically where they have earned a boundary; each may contain
contracts, application orchestration, and adapters. The daemon is the sole
composition root and owner of the Telegram session and writable runtime state;
MCP stays a thin inbound adapter. No migration step should duplicate an
existing contract, create a speculative repository port, or hide
operator-controlled policy outside `config.py`.
