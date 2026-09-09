# Telegram Demand Control

Status: proposal for expert review  
Basis: Astra architecture proposal and expert-panel convergence, 2026-09-09

## Decision

`mcp-telegram` currently regulates Telegram traffic only after producers have
created RPC work. The shared gate enforces the account limit and distributes
dispatches among `INTERACTIVE`, `LIVE_SYNC` and `BACKGROUND`, but producers
independently decide how much work to create, repeat and recover. Unnecessary
demand can therefore reach a shared queue before the system understands it.

The target is one model for all Telegram demand:

```text
Domain need
    ↓
Demand registry and coordinator
    ↓ select, combine, defer, slice
Service-class and source queues
    ↓
RPC scheduler and account gate
    ↓
Telegram
```

Every application-owned reason to contact Telegram receives a precise,
code-owned identity and contract. The coordinator sees all demand while
preserving its real execution ownership:

- `INLINE` work remains tied to a caller or event;
- `PROTOCOL` work remains controlled by Telethon's recovery protocol;
- `DURABLE` work is awakened by the coordinator through a small domain adapter.

The existing transport gate remains the final authority for the account limit,
FloodWait, admission deadlines and actual RPC attempts. Domain modules retain
Telegram semantics, eligibility, cursors, coalescing keys and atomic result
application. Their existing tables remain the durable source of truth; there is
no universal job database.

This is a complete migration of all Telegram demand, delivered in two pull
requests. The first introduces the final model, complete attribution, adapters
and shadow execution. The second performs one global durable cutover and removes
the scheduling machinery it replaces.

## Business outcome

The change must achieve three outcomes together:

1. The service remains responsive and recovers correctly from restart,
   cancellation, partial work and Telegram throttling.
2. It stops producing Telegram work more often or in larger batches than the
   product's freshness needs require.
3. Operators and maintainers can see where demand comes from, whether it is
   being satisfied and where policy belongs without tracing many worker loops.

The goal is not to minimize RPC traffic at any cost. Useful background work may
consume spare capacity. The system suppresses redundant work, preserves
priority under contention and exposes freshness debt when required work exceeds
available capacity.

## Current evidence

The existing scheduler is already a strong transport boundary: it implements
work-conserving 6/3/1 admission, limits outstanding work and rechecks account
readiness before every send. Every scalar sender attempt is rate-limited,
including pagination, resolution and retries.

The current registry has 25 source identities. It makes ownership and class
visible, but several sources mix operations with different cost, cadence and
bounds. Dialog resolution, for example, can mean one entity lookup or a complete
dialog traversal.

Recent runtime evidence shows the account operating close to configured
capacity. Scheduled reconciliation and Telethon difference recovery are large
contributors. Interactive MCP latency remained low in the inspected window and
no user-visible starvation was established. This supports demand reduction and
better attribution, but not hard reservations that deliberately waste capacity.

The repository already has partial demand-control patterns: hydration has
durable coalescing and batching; entity refresh has bounded single-flight work;
activity and sync workers have cursors and checkpoints; the scheduled diff adds
per-dialog due state, active versus quiet work, generation safety and bounded
slices. The target standardizes shared control without replacing these domain
rules with a workflow engine.

## Target responsibilities

### Demand registry

The existing registry evolves into the one exhaustive policy source.
`DemandKind` identifies a stable operation such as `entity_lookup`,
`dialog_traversal`, `scheduled_repair`, `scheduled_discovery` or
`cold_backfill`. `TelegramRpcSource` remains the producer grouping used for
ownership, fairness and reporting. A page or batch is a slice, not another kind.

Each immutable contract contains only policy that runtime enforces:

| Field | Meaning |
| --- | --- |
| `kind` | Stable operation identity |
| `source` | Owning producer and source-fairness key |
| `service_class` | `INTERACTIVE`, `LIVE_SYNC` or `BACKGROUND` |
| `execution_mode` | `INLINE`, `PROTOCOL` or `DURABLE` |
| `freshness_target` | Product deadline for durable data, when applicable |
| `max_rpc_attempts_per_slice` | Hard bound for durable execution |

Descriptive label, purpose and fact-domain metadata may remain for reports and
audits. They do not create a second policy system.

Startup checks enforce:

- every demand kind has exactly one contract;
- every Telegram source is covered;
- every durable kind has exactly one adapter;
- inline and protocol kinds cannot register durable adapters;
- callers cannot choose their class or slice limit; a caller deadline may only
  tighten the code-owned admission deadline;
- the gate rejects application RPC without demand context.

The initial exhaustive mapping is:

| Current source | Demand kind or kinds | Mode | Durable adapter |
| --- | --- | --- | --- |
| `MCP_INTERACTIVE` | MCP remote acquisition | `INLINE` | — |
| `MESSAGE_READ_FALLBACK` | Message read fallback | `INLINE` | — |
| `DIALOG_RESOLUTION` | Entity lookup; dialog traversal | `INLINE` | — |
| `TOPIC_RESOLUTION` | Topic lookup | `INLINE` | — |
| `ENTITY_INFO_FOREGROUND` | Foreground entity facts | `INLINE` | — |
| `ENTITY_INFO_REFRESH` | Entity profile refresh | `DURABLE` | Entity refresh state |
| `ACCOUNT_TRACE` | Account trace page | `INLINE` | — |
| `TELETHON_UPDATE_DIFFERENCE` | Telethon update difference | `PROTOCOL` | — |
| `RECONNECT_DIFFERENCE` | Reconnect difference request | `DURABLE` | Reconnect state |
| `REALTIME_EVENT` | Realtime event acquisition | `INLINE` | — |
| `DELTA_SYNC` | Delta gap fill; access probe | `DURABLE` | Delta state |
| `ACTIVITY_HOT_SWEEP` | Hot activity page | `DURABLE` | Activity dialog state |
| `FACT_HYDRATION_LIVE` | Live hydration batch | `DURABLE` | Hydration jobs |
| `FULL_SYNC` | Full-sync page | `DURABLE` | Synced-dialog state |
| `DIALOG_SYNC` | Dialog bootstrap; light/full reconciliation | `DURABLE` | Dialog state |
| `ACTIVITY_ARCHIVE` | Archive backfill; incremental archive | `DURABLE` | Activity archive state |
| `ACTIVITY_COLD_BACKFILL` | Cold peer page | `DURABLE` | Activity dialog state |
| `FACT_HYDRATION_BACKFILL` | Backfill hydration batch | `DURABLE` | Hydration jobs |
| `FOLDER_RECONCILIATION` | Folder snapshot | `DURABLE` | Folder state |
| `TOPIC_RECONCILIATION` | Topic snapshot | `DURABLE` | Topic state |
| `MESSAGE_FACT_REFRESH` | Message-fact refresh | `DURABLE` | Fact-refresh state |
| `REACTION_REFRESH` | Reaction refresh batch | `DURABLE` | Reaction state |
| `READ_RECEIPT_PROBE` | Read-receipt batch | `DURABLE` | Read-receipt state |
| `SCHEDULED_MESSAGES` | Scheduled repair; scheduled discovery | `DURABLE` | Scheduled state |
| `MAINTENANCE` | Self-profile maintenance | `DURABLE` | Maintenance state |

This table is the required starting inventory, not permission to preserve broad
kinds. Code review must split any row whose operations have different mode,
freshness or slice bounds. `RECONNECT_DIFFERENCE` remains distinct from
Telethon-owned update difference.

### Demand coordinator

One process-local coordinator accepts all demand through three small entry
points:

```python
run(kind, operation, *, deadline=None)  # INLINE
protocol_scope(kind)                    # PROTOCOL
offer(kind)                             # DURABLE
```

`run()` installs context around caller- or event-owned work and preserves its
existing result and cancellation behavior. It does not persist arguments or
invent a generic future lifecycle.

`protocol_scope()` attributes Telethon-owned recovery while leaving protocol
and retry lifecycle inside Telethon.

`offer()` wakes a durable kind after its domain state has already been written
atomically. The coordinator keeps one ready entry and at most one active slice
per kind. Repeated offers coalesce as wakeups; the domain table determines
actual work and whether more remains.

`offer()` is only a low-latency hint, never the durable record. To close the
commit-to-wakeup race, the coordinator scans `status()` for every durable
adapter at startup, after each slice and when the nearest reported release time
arrives. A crash between a domain commit and `offer()` therefore delays work at
most until the next authoritative scan; it cannot lose it.

The coordinator owns only shared durable mechanics: readiness waiting, wakeups,
ready-queue deduplication, single-flight, bounded slice invocation and lifecycle
observations. It rotates ready durable kinds using deterministic FIFO. Source
fairness belongs only to the RPC scheduler, where actual attempts are known.
The coordinator does not store work keys, payloads, cursors, generations, retry
counts, results or generic lifecycle rows.

### Durable domain adapter

Every durable kind exposes a narrow interface over existing domain state:

```python
class DurableDemandAdapter(Protocol):
    def status(self, now: float) -> DemandStatus | None: ...
    async def run_slice(self, budget: RpcAttemptBudget) -> None: ...
```

`status()` reports whether work exists, its release boundary and any freshness
debt. `run_slice()` performs one safe unit or bounded group. The gate debits
`RpcAttemptBudget` on every real send and yields before a send that would exceed
the slice. After return, the coordinator always rereads `status()`; there is no
second generic result lifecycle. The adapter owns selection, eligibility,
coalescing identity, domain retry, cursors, generation guards and result
application.

The transaction that stores a domain event or advances a cursor must leave
`status()` able to report remaining work. A crash may repeat a Telegram read,
so application is idempotent and completion is never recorded before domain
state is committed.

### RPC scheduler and gate

The transport layer remains the only owner of the account limiter, 6/3/1 class
arbitration, admission deadlines, readiness before send, transport retries,
finite FloodWait cooldown, latched kill switch and actual attempt accounting.

Scheduling becomes `class → source → FIFO actual attempt`. Source round-robin
prevents a prolific producer from occupying its whole class. Charging occurs at
the sender because only it observes resolution, pagination and retries.

Each source also has a code-owned outstanding bound enforced before it enters a
class queue. This prevents one producer from filling shared class capacity
before round-robin selection. It is a memory/concurrency guard, not a separate
rate or operator tuning knob.

The class scheduler stays work-conserving. There are no hard source percentages
or idle reservations.

### Demand context

Each root operation creates an opaque runtime token containing its demand kind,
source and service class. Nested helpers inherit it. Detached work must create
or explicitly transfer registered context. Helpers cannot silently promote
their class.

The token provides causal attribution only. It is not persisted and contains no
Telegram or user data.

## Freshness, bounds and backpressure

Freshness is the product tolerance for stale local state. A transport admission
deadline is how long one RPC may wait after execution begins. They remain
separate.

Durable work is not runnable before its domain release time, so spare capacity
cannot make a quiet poll repeat early. Once runnable, an adapter performs no
more than its code-owned actual-attempt bound and yields at the next safe cursor
or snapshot boundary.

The first implementation uses measured attempts, not predictive cost models.
If a safe unit can exceed the bound, the adapter splits it or reports a contract
violation. When demand exceeds account capacity, overdue age and progress expose
the shortfall; stale data is never reported as fresh.

## FloodWait and restart

One account-wide cooldown controls every class and producer. Finite cooldown
survives daemon restart so repeated restarts cannot reset protection and create
fresh bursts. The existing latched kill switch remains a process stop and is
cleared only by an explicit operator restart; automatic restart policy must not
turn it into a retry loop.

The coordinator starts no durable slice while the gate is unavailable. Offers
continue to coalesce. Wakeup considers both adapter release time and account
readiness, preventing zero-time polling during cooldown.

Shutdown stops new slices and leaves domain state recoverable. The daemon is the
only writer and executor, so a distributed lease system is unnecessary. Domain
claims must still be atomic and restart-safe.

## Domain ownership that remains

The common layer never infers Telegram correctness. Domain modules retain
eligibility, access-loss interpretation, history completion, publication and
cancellation semantics, batching compatibility, cursors, generation safety,
idempotent storage and exact coalescing rules.

This keeps one reason to change per component: registry changes policy,
coordinator changes shared demand mechanics, adapters change domain
acquisition, and the gate changes transport protection.

## Shadow mode

PR 1 runs the final decision code in shadow while legacy launchers execute.
Shadow receives real triggers and outcomes but does not call Telegram, claim
domain rows or apply results. It uses only process-local coordinator queues,
reconstructed from every adapter's `status()`, and the existing observation
stream. It creates no shadow table or copied lifecycle state.

It compares offered demand, ready work, coalesced wakeups, predicted selection,
actual attempts, queue age and overdue state. It also proves that every send has
registered root identity.

Shadow cannot prove counterfactual freshness because it cannot see a result it
would have requested at another time. Such cases remain unknown. Freshness,
latency and FloodWait effects require live enforcement. Shadow runtime is
removed after cutover.

## Pull request plan

Two pull requests provide a deployed observation boundary without leaving a
permanent mixed architecture. One PR would combine identity, Telethon
attribution, every adapter, global cutover and legacy removal without a soak
boundary. Three are unnecessary unless PR 1 becomes too large to review safely.

### PR 1: contracts, attribution, adapters and shadow

PR 1 introduces the complete target model without changing the production owner
of durable execution.

It must:

- define all operation-specific demand kinds and execution modes;
- replace source-only scopes at every root entry point;
- cover all interactive, live-sync, background and Telethon-owned paths;
- propagate context through resolution, pagination, retries and detached tasks;
- make the gate fail closed for missing or inconsistent context;
- add source subqueues inside the existing class scheduler;
- implement the coordinator and every durable adapter over domain tables;
- run durable selection in shadow without sends or claims;
- record bounded offered-to-attempt evidence and truthful coverage;
- persist finite cooldown at the transport boundary and preserve the latched
  stop across automatic restarts;
- keep legacy launchers as the only production executors.

PR 1 adds no universal demand table and moves no domain cursor. It deploys in
shadow for at least one complete cycle of the longest freshness policy.

Acceptance requires every RPC to be attributed, zero shadow RPC, exact adapter
coverage, bounded resource use, restart-safe account protection and no demand
shape that the registry cannot represent.

### PR 2: global durable cutover and legacy deletion

PR 2 makes the coordinator the sole launcher for every durable kind. The cutover
is global in daemon composition: coordinator pumps start and legacy loops do not.

It must migrate scheduled repair/discovery, full and delta sync, dialog sync,
hot/cold and archive activity, hydration, entity refresh, folders, topics,
facts, reactions, read receipts and maintenance work. Protocol-owned Telethon
recovery stays behind its registered scope; inline caller and event behavior
stays behind `run()`.

The PR removes producer polling loops, sleeps, wakeup calculations, generic
single-flight and account retry state replaced by the coordinator. It retains
domain selection, batching, cursor, generation and result application. It also
removes shadow, compatibility paths and superseded config.

Acceptance requires restart, cancellation, FloodWait, partial-page, snapshot
race and mixed-load tests plus one live longest-freshness cycle without
unexplained debt or user-path regression.

Rollback returns to the PR 1 image and legacy launchers. PR 2 must therefore
avoid destructive domain-table changes and cursor deletion.

If PR 1 becomes too large, the only allowed three-PR split is:

1. contracts, context, full attribution and all-demand shadow;
2. all adapters and one global cutover;
3. deletion of legacy and shadow paths.

The work must not settle into a permanent mixture of old and new producers.

## Current dirty worktree

The current scheduled work is input, not protected sunk cost.

Preserve after verification: active repair versus quiet discovery, narrow
working set, per-dialog due state, event coalescing, oldest dirty age,
generation protection, bounded selection, restart-aware dialog/folder cadence
and reaction persistence retry without another Telegram fetch.

Correct or replace before cutover:

- future account retry can produce a zero-time busy loop;
- excluded discovery removes the whole `own_only_dialogs` row instead of only
  its scheduled basis;
- generation must be checked transactionally before snapshot writes;
- candidate seeding must not rescan everything before every immediate slice;
- cadence must come from the contract rather than a hard-coded duplicate;
- unused lifecycle columns should not enter an unreleased migration.

Other migration-sensitive invariants found by the panel:

- HotSweep must not advance its committed high-water cursor after only one page
  when a later page is throttled;
- ColdBackfill needs an atomic claim that excludes a second executor;
- long FullSync and dialog traversals must yield and resume from committed state.

These belong in domain adapters and do not justify a generic workflow lifecycle.

## Operator evidence

The existing observation stream and CLI summary remain the operator surface.
They report offered and locally satisfied demand, ready/completed/deferred
slices, oldest overdue age, backlog progress, actual RPC attempts, queue wait,
account utilization, FloodWait state and interactive latency.

The report answers: what demand arrived, how Telegram capacity was used, and
what remains unsatisfied and why. It omits empty matrices and synthetic scores.

A window is partial or unreliable after queue overflow, writer failure, shutdown
loss, retention-cap truncation or a telemetry gap. Missing failures in an
incomplete window are not reported as health.

Telemetry contains no peer ID, username, Telegram text, search argument, durable
work key, raw exception or traceback.

## Acceptance invariants

- Every Telegram send has exactly one registered kind, source and class.
- Producers cannot override transport policy.
- Nested sends inherit one root; detached work establishes another.
- Every durable kind has exactly one adapter and execution owner.
- Every source has a bounded outstanding count before class admission.
- Equivalent wakeup storms create one ready entry while domain state remains
  authoritative.
- One source cannot capture its whole service class.
- Long work yields after a bounded number of actual attempts and resumes.
- A concurrent event cannot be erased by an older snapshot.
- Crash before send, after send or before apply cannot mark work complete.
- FloodWait stops sends across all sources, survives restart and creates no spin.
- Inline cancellation and Telethon recovery retain their semantics.
- Insufficient capacity appears as overdue demand.
- Shadow sends nothing and claims no counterfactual freshness.
- Telemetry failure cannot block work or leak content.
- Final composition has no legacy polling launcher, dual executor,
  `PRODUCER_UNBOUNDED` contract or runtime priority knob.

## Explicitly excluded enterprise features

The implementation does not add a separate service, broker, distributed
scheduler, universal job table, serialized workflow payloads, cross-domain
result application, predictive cost model, per-source operator weights, dynamic
policy controller, durable correlation database, dashboard, permanent shadow
runtime, plugin system or generic executor framework.

New common state is admitted only when the full migration requires it and no
domain state can own it. After cutover, another generic mechanism requires the
same proven need in at least three demand kinds and must delete the duplicated
implementation it replaces.

## Accepted scheduled-message targets

The product targets are confirmed:

- changes to a known scheduled-message queue are repaired within 15 minutes;
- a previously unknown scheduled-message queue is discovered within 24 hours.

These targets define overdue work and acceptance for scheduled reconciliation.
They may change later only from product need or production evidence, not as an
operator tuning response to incidental load.
