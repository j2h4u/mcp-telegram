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
network acquisition, refresh admission or foreground waiting. Enabled personal
dialog synchronization owns bounded, durable counterpart-profile collection,
including the existing archived and hidden cohort. Delivered profile fields
materialize canonically, with per-field ordering against older in-flight RPCs.
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

Current implementation debt is tracked explicitly:

| Work | Known departure | Delivery |
| --- | --- | --- |
| Non-DM profiles | Reads expose retained facts or explicit partial coverage; automatic acquisition ownership for never-collected group/channel profiles is not supplied by the DM slice | WorkGraph `TG-91`, including completed DM-profile delivery evidence |
| Startup and offline availability | Telegram/startup readiness currently limits local serving; no whole-process offline guarantee is made | Planned startup investigation `TG-90`; lifecycle design tracked in `TG-91` |

The WorkGraph delivery tracker `TG-91` separates
these changes from COMMIT performance and broader replay reliability research.
Do not report this contract as fully implemented after a documentation change
or only the profile slice. Tests for ordinary reads must forbid network calls
and acquisition waits, retain aged useful records and expose honest coverage.

### Acquisition ownership

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
