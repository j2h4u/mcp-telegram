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
directions. This is the required boundary; existing departures are listed below:

- **Acquire Telegram facts** — daemon-owned use cases contact Telegram and
  materialize facts into SQLite.
- **Serve agent reads** — MCP/daemon read use cases project SQLite state only;
  they do not contact Telegram.

This is the minimum DDD vocabulary for the current codebase:

| Domain concept | Use case | Telegram access | Local output |
| --- | --- | --- | --- |
| Dialog | Canonical dialog directory and `dialog_sync` light reconciliation | Dialog metadata and folders | `dialogs`, folder snapshots |
| Topic catalog | Enabled enrollment and durable `TOPIC_SNAPSHOT` demand | Bounded topic pages | Topic metadata, catalog membership and enumeration receipts |
| Message history | `FullSyncWorker` | Backward history batches | `messages`, FTS, aggregate reactions present on message objects |
| Recent gaps | `DeltaSyncWorker` | Forward messages newer than local max id | New `messages`, FTS, aggregate reactions present on message objects |
| Live events | `EventHandlerManager` | Telethon updates while daemon is online | New/edit/delete/read cursor rows; aggregate reaction updates |
| Optional message facts | `message_fact_refresh` | Bounded per-message fact probes | Detailed reaction events/timestamps and exact outgoing-DM `read_at` facts |
| Agent reading | `DaemonReadingService`, `DaemonAPIServer` read handlers | None | Structured MCP responses from local projections |

## Durable facts and acquisition evidence

SQLite is the durable working source of truth for local search, analytics and
MCP. Telegram is the upstream source of observations, not a required query
backend. Ordinary reads project canonical persisted facts and their limitations;
they do not initiate or await remote acquisition. Ingestion, enabled sync
subscriptions, initial/backfill work and repair of identified gaps own that work.
All actual Telegram calls still pass through the existing classified RPC gate,
priority admission, budgets and FloodWait handling. A call can use the gate
correctly and still violate the product boundary by originating inside a read.

Keep these meanings independent:

| Concern | Meaning | Effect of elapsed time |
| --- | --- | --- |
| Durable fact | Latest accepted value or authoritative clearing, within its account namespace | Remains available until superseded or subject to an actual domain validity, retention or privacy rule |
| Coverage | Which fields or selection were observed, including known absence | Does not become never-observed because an observation aged |
| Observation age | When the evidence was acquired | Describes currency, not whether the fact exists |
| Synchronization state | Known gaps, pending changes, retries and unavailable fields | Reports what maintenance remains without erasing last-known data |
| Acquisition receipt | Exact observation, scope, field coverage and ordering evidence | May stop satisfying an explicit acquisition requirement without revoking materialized facts |
| Transport cache | Disposable access hashes, request coalescing and acceleration state | May expire independently of durable domain data |

A never-observed biography, an observed empty biography and a failed refresh
are different results. A useful local profile can be old or limited without
being absent. Time-bound facts such as an online-until timestamp can change
their current interpretation locally; that is not permission to poll Telegram
or invent a new observation. Same-account reconnection and expired receipt
freshness are not account ownership changes. A real account mismatch remains a
closed privacy boundary.

Free facts delivered by Telegram updates must materialize in the canonical
repository transaction used by both SQL analytics and MCP. Conflict metadata
may protect a merge, but a service-only overlay must not create a second truth.
Only fields actually observed may supersede older values; omitted, partial and
explicitly cleared fields have different semantics. Preserve original evidence
times and protect newer events from older in-flight acquisitions.

Profile reads and entity-name resolution project retained local facts without
network acquisition, refresh admission or foreground waiting. Bounded, durable
counterpart-profile collection covers all local personal user/bot dialogs,
including archived, hidden and history-disabled dialogs, plus peers known from
retained private-message history; the authenticated account itself is excluded.
Profile admission never changes message-history enrollment. Supported fields
supplied by updates materialize canonically, with per-field ordering against
older in-flight RPCs.
Acquisition receipts still retain their separate authorization/reuse rules.

History and bounded context use local projections regardless of sync lifecycle
or enabled enrollment. A retained context anchor is required; missing, deleted
and time-excluded local anchors are distinct outcomes. Selection availability,
historical coverage, observation horizon and current access are independent.
Legacy fragment markers are not successful acquisition receipts. Neither local
pagination exhaustion nor incomplete sender/topic attribution establishes
remote absence. Explicit history acquisition remains sync enrollment, not a
read-side fallback or a new cloud-context command.

Topic reads never fetch catalogs or optional icons, offer demand, or write
observations. Existing enabled enrollment authorizes durable topic collection,
including automatic DM enrollment, without creating new subscriptions. Startup
bootstraps missing work and resumes saved pages, rather than invalidating
completed catalogs merely because the process restarted. Relevant delivered
changes and identified recovery gaps invalidate existing authorized work.
Page facts and cursor progress commit atomically in `sync.db`; completion is an
enumeration observation interval, not an atomic cloud snapshot. Generation,
request revision and observation fences preserve newer events. A completed
omission changes catalog membership, never invents deletion or removes
historical topic metadata. Partial attempts retain last-good facts and receipts
while reporting current incompleteness. Missing icon decoration is independent
of catalog completeness.

Migration 85 completes historical FTS filling once for every supported earlier
database. It checks keyed live-row coverage and removes invalid index rows,
without comparing every stored text or asserting Telegram-history completeness.
Bounded batches retain committed progress; only successful completion of both
passes records version 85 and removes the former startup receipt in one final
transaction. A failure stops bootstrap until a successful retry. Later starts
use the migration ledger and do not scan FTS or check a special receipt, even
after unrelated schema changes. Normal message writes maintain the message/key/FTS
projection atomically. Actual search SQL failures report index unavailability
with `list_messages` as the retained-data fallback; scheduled-only search remains
independent. Reads never start repair or Telegram acquisition.

Accepted scope limits and delivery evidence:

| Scope | Current boundary | Evidence |
| --- | --- | --- |
| Non-DM profiles | Reads expose retained facts or explicit partial coverage; blanket automatic acquisition of never-collected group/channel profiles is outside the accepted DM slice | Accepted scope review `TG-97` |
| Startup and offline availability | Telegram/startup readiness limits local serving; no whole-process offline guarantee is made | Accepted audit `TG-90`; historical FTS migration `TG-96` replaces `TG-95` startup receipts, not the startup lifecycle |

WorkGraph `TG-91` records source, test and live-runtime evidence for update replay
after durable local admission (`TG-101`, `TG-16`), observed-mode account tracing
(`TG-102`), retained statistics (`TG-103`), operational documentation (`TG-106`),
and persistence consolidation (`TG-94`, including `TG-107` and `TG-104`).
Storage/WAL flush latency remains outside this delivery’s guarantees.
Tests for ordinary reads must forbid network calls and acquisition waits,
retain aged useful records and expose honest coverage.

### Acquisition ownership

See the [fact acquisition telemetry reference](telegram-fact-acquisition-telemetry.md)
for the active paired Entity Profile observation contract.

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

Every application-owned Telegram RPC reason is declared in the immutable
consumer registry. Its admission view classifies transport work, while its
acquisition and demand views describe fact ownership, overlap with recovery
paths, fan-out, and where offered work is bounded. The transport gate rejects
unclassified calls before Telegram sees them. Producer-specific cadence and
work selection remain with the producer rather than turning the registry into
a second scheduler.

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

### Telegram write admission and recovery

Every application write and replay passes through the existing gateway
priority queue with its bounded attempt accounting and deadline. Immediately
before a write, a synchronous final claim rechecks cancellation, known account
and method cooldown floors, authentication scope, protection generation and
pacing. Production transport uses a repository-owned, version-guarded adapter
for Telethon 1.45 private APIs; it does not patch the installed virtualenv, fork
Telethon or introduce a competing queue.

Runtime configuration has one fixed compatibility marker:

```toml
[telegram_rpc]
transport_contract_version = 1
```

The marker must be the integral value `1`; it is not a policy knob. Missing or
unknown values fail configuration loading before Telegram connection. Persisted
format migration requires the writer stopped and a matching backup of the
current `sync.db`, Telegram session and configuration; the deployment-local
[backup and rollback procedure](../deploy/AGENTS.md#sqlite-backup-restore-and-retention)
contains the supported SQLite-aware handling. Keep the marker when rolling
back to a compatible binary. Never rewind the database after new facts have
been committed or erase protection floors/intents. For a failed rollout, use a
forward fix or protected current compatible binary first.

Automatic full-lifecycle resumption is enabled by default
(`[flood_wait] auto_resume_enabled = true`). The central receipt retains
cause-specific recovery state: confirmed FloodWait has six attempts with
1/2/4/8/16/24-hour waits; transport and retryable server failures use a capped
30-second to 30-minute backoff; eligible unexplained unknown read outcomes use
a capped five-minute to one-hour backoff. Applicable server floors and already
incurred policy waits cannot be shortened. Interrupted trials remain spent.
Automatic recovery records a scoped policy disposition for eligible unknown
reads; it does not turn the outcome into a known result or an operator
acknowledgement. Unknown mutations, unclassified methods, invalid storage or
account ownership, unresolved authentication and exhausted FloodWait attempts
remain hard holds. Disable the mode on a compatible binary to disarm future
trials while preserving receipt state. Stability requires a continuous verified
hour plus successful application acquisition and accepted canonical progress.

Manual recovery is `mcp-telegram recover-account
--acknowledge-unknown-outcome`. It reads the active protection generation, then
requests one conditional, one-use authorization scoped to the known cooldown
floor, authentication scope and valid persisted receipt. Success means only
that recovery is authorized and a restart is scheduled; it does not mean the
account has recovered. The command retains known floors and prior uncertain
outcomes, with operator acknowledgement. A bounded bootstrap and identity trial
may follow restart only under that authorization. Trial success opens that
operator authorization but cannot prove an unseen server floor has elapsed.
Failure, FloodWait or uncertainty returns to hold; retry requires a new explicit
operator authorization. There is no MCP recovery tool, raw state reset or
SQL-edit recovery path.

Protocol ACK, Ping and StateInfo controls use exact, bounded central admission
during an application-write hold. They remain protocol-only: no application
payload smuggling and no per-ACK filesystem sync. A local protocol-control write
is not evidence that Telegram applied an application request. A never-sent
failed Ping releases only its matching keepalive marker. The bounded ACK replay
cache starts at the first physical write, preserving queued obligations. During an
application acquisition hold, the daemon keeps the connected receiver, passive
update persistence, local API, writer and heartbeat alive. Difference requests
are deferred before a write while the existing vendor loop retains its genuine
gaps and safe sequence/date checkpoints. Retained data remains readable;
receive-only operation does not claim completeness across those gaps or restore
a disconnected session. A local operator with root or administrator access can
still bypass repository controls; the gateway guard prevents accidental
application regressions.

### Protected acquisition health and method recovery

The daemon health probe checks both local socket serving and the existing account
protection status. Active account protection makes container health fail while
retained local MCP reads remain available. Docker health alone does not restart
the service; the account latch still requires controlled operator recovery.

The gateway owns network retry eligibility for all durable acquisition owners.
Server/account floors are never capped. A canonical method's additional delay
starts at 300 seconds, doubles on repeated actual method FloodWaits up to 3600 seconds,
and resets only after the current generation's actual recovery request succeeds.
Only one real request owns that method/generation recovery trial at a time.
Durable method state retains its live or recovered generation across restart.

The demand coordinator selects business work and keeps siblings runnable while
a selected revision waits. Each owner identifies its account, target and exact
persisted business revision, validates that selection before acquisition, and
returns progress only from its own accepted transaction. A successful empty
periodic cycle advances an accepted-cycle identity; retry timestamps and failed
attempts do not create new business work. Supplied events remain local writes.

The gateway can grant one additional original-contract allowance per canonical
method/generation/exact revision within the running process. All bindings share
cumulative actual-write accounting. Preparation alone grants no write, and a
restart cannot recreate spent extra allowances for a restored generation; the
ordinary finite base allowance remains available. A revision with no remaining
allowance or eligible recovery stays parked until its business state changes or
a permitted recovery/restart boundary applies. Producers retain cadence and
local fact state, but do not add independent network retry delays. Failed
profile acquisition preserves last-known facts and section failure evidence.
Account protection and manual recovery remain separate from method recovery.
