# Telegram Behind Domain Ports

Status: panel accepted; Slice 1 deployed; Slice 2 is a code-complete PR candidate pending release
Last reviewed: 2026-09-18

## Incident guardrail: reaction-detail demand

The reaction-detail lifecycle introduced by PR #275 still allowed repeated
durable refresh slices to reopen the same raw candidates. In the stopped
production runtime this produced 321 `message_fact_refresh` dispatches in
about 20 seconds from 8,279 observed dispatches. The lifecycle contract now
requires a database-backed singleton pacing window: at most five logical
reaction-detail pages may be claimed in each 600-second window by default;
claims and starts are committed before Telegram work, and cancellation,
restart, failure, or a FloodWait cannot return a claimed slot to the window.
The raw reaction due boundary is combined with this release boundary while
exact outgoing-DM read-date work remains independently due and runs first when
both lanes are ready. The operator may change the positive cycle duration and
page cap through scheduling configuration, with the page cap retaining its
default of five.

Latest panel recommendation:
[Expert Panel Recommendation, 2026-09-18](telegram-domain-ports-panel-2026-09-18.md).
It proposes two evidence-backed slices. Slice 1 is deployed; Slice 2 is
implemented locally and awaits release and deployment.

Before deploying Slice 2, the orchestrator must remove the obsolete
`[freshness.reactions]` section from the host-owned config. The source parser
intentionally rejects that retired section; this checkout does not rewrite the
live `/opt/docker/mcp-telegram/config.toml` automatically.

This document records the target architecture, the program completed through
PR #270, and the residual state considered by the 2026-09-18 expert panel. The
panel produced a separate recommendation that is now accepted for both slices;
the second slice is not yet a deployment approval.

The accepted catalog-specific contract remains
[Canonical Dialog Snapshot](canonical-dialog-snapshot.md). The broader current
module map remains [Architecture proposal](../architecture-proposal.md). This
document defines the direction for reducing the remaining Telegram coupling
across the whole service.

## Problem

The service grew from product-facing MCP tools. Each new scenario selected the
Telegram or Telethon operations it needed, interpreted their pagination and
completeness, persisted useful fields, and returned an MCP result. This was a
simple way to add capabilities, but it distributed acquisition decisions among
multiple application paths.

The shared RPC coordinator controls admission, budgets, fairness, and FloodWait
pressure. It cannot by itself recognize that different admitted consumers are
obtaining the same facts through different RPC paths. Consequently, regulated
calls can still duplicate acquisition, publish inconsistent local views, or
apply different freshness and completeness semantics to the same fact.

The canonical dialog directory fixed one concrete instance. Account-wide
dialog acquisition now has one owner, while folder projection, DM enrollment,
and natural-name resolution consume its published local facts. The same
ownership principle has not yet been applied consistently to every Telegram
capability.

## Target architecture

Telegram is an external system hidden behind domain-oriented ports. MCP tools
are inbound delivery adapters. They call application services and never select
Telegram RPCs, import Telethon, or interpret Telegram response objects.

```text
MCP tools
    |
    v
Application services
    |
    v
Domain contracts and ports
    |
    v
Telegram adapters -----> Telethon / Telegram RPC
    |
    v
Acquisition owners ----> SQLite projections
                            ^
                            |
                     local read consumers
```

Every outbound Telegram operation belongs to a named domain capability and is
invoked through its port. Request construction, Telethon types, Telegram error
translation, raw pagination, and response normalization stop at the Telegram
adapter boundary. Application code works with domain requests, observations,
and outcomes.

An RPC does not automatically require a table, aggregate, cache, or durable
entity. A point operation may pass through a domain port and adapter without
local persistence. When several consumers need the same facts, or when those
facts require shared freshness, completeness, restart, or failure semantics,
one acquisition owner publishes a local projection and the consumers read it
locally.

The unit of ownership is therefore a coherent set of product facts, not a
Telethon method and not an MCP tool.

## Architectural invariants

- MCP tools and delivery modules do not import Telethon or construct Telegram
  requests.
- Application services do not import Telethon or call `TelegramClient`
  methods directly.
- Every outbound Telegram operation crosses a domain port and a concrete
  Telegram adapter.
- Every repeatedly acquired fact set has one named acquisition owner.
- Consumers of a published fact set do not add their own remote fallback.
- Completeness and freshness belong to domain observations and receipts. A
  local read does not renew Telegram observation time.
- `unknown`, `absent`, and `present` remain distinct whenever the source cannot
  prove a boolean or an empty result.
- SQLite projections use domain shapes. They do not persist raw Telegram
  response objects merely to reproduce the RPC schema.
- The RPC coordinator remains the common transport admission boundary. Domain
  ownership complements it; it does not replace its budgets and fairness.
- New observability is added only when existing evidence cannot adjudicate a
  suspected overlap or prove an acceptance criterion.
- A refactor removes the superseded path. It does not retain rollback paths,
  compatibility healing, or shadow execution without a current product need.
- Each slice reduces the legacy complexity it touches. The Radon and CRAP
  baselines are temporary debt ceilings, not complexity budgets that new code
  may consume.

## Current state after the completed program

The MCP delivery boundary already satisfies the target: production modules
under `tools/` have no direct Telethon imports and communicate with the daemon
API.

The causal request observability slice is deployed: MCP operation IDs reach the
daemon, bounded timing observations correlate with terminal `mcp.call` rows,
and `list_messages` records privacy-safe route, phase, fallback, and RPC
attempt evidence. A live devtools-client call and deterministic tests cover
the instrumented behavior.

The group, user, channel, and remaining entity-profile fact acquisitions now
cross capability-specific ports and Telegram adapters. Group profile and
contact overlap share one `GetFullChat` observation, and the profile adapters
own `GetFullUser`, `GetFullChannel`, participant, common-chat, and avatar
request construction. Generic entity resolution remains migration debt:
`daemon_entity_info.py` still calls `client.get_entity` and translates
Telethon error types for not-found classification. That is separate from the
completed entity-profile fact ports. The external-import ratchet still allows
legacy owners elsewhere in the service.

Full message-history backfill and bounded forward gap synchronization now use
separate message-history ports and one Telegram normalization boundary. Their
different completeness, restart, access-loss, cancellation, and checkpoint
semantics remain explicit. The obsolete unbounded delta path and its inert
operator configuration were removed.

The boundary below delivery remains transitional. Folders, topics, reactions,
entity profiles, and persistent message history have recognizable Telegram
adapters. The canonical dialog directory owns account-wide dialog acquisition.
Other application and worker modules still import Telethon or invoke client
methods directly, especially activity sweeps, account trace, read receipts,
scheduled messages, transcription, generic entity resolution, and realtime
event enrichment.

The current external-import check is a ratchet over the brownfield state. It
prevents an unreviewed new Telethon importer, but its allowlist still recognizes
27 production import owners. Passing that check does not yet prove that
Telegram is hidden behind domain ports.

## Completed program through PR #270

The completed program was a coherent sequence, not 49 independent pull
requests. It delivered these product and architecture outcomes:

- [x] Added privacy-safe causal request observability for slow MCP calls,
  including local work, admission delay, Telegram execution, fallback, and
  response shaping.
- [x] Made the canonical dialog snapshot the single account-wide dialog owner;
  folder projection, DM enrollment, and local resolution consume it locally.
- [x] Consolidated the proven `GetFullChat` overlap so one observation supplies
  both group profile and contact-overlap facts.
- [x] Moved user, channel, group, participant, common-chat, and avatar profile
  acquisition behind capability-specific ports and Telegram adapters.
- [x] Moved persistent full-history and bounded forward-gap acquisition behind
  separate message-history ports with one normalization boundary.
- [x] Preserved truthful completeness, freshness, retry, cancellation,
  access-loss, and restart behavior for the migrated capabilities.
- [x] Removed superseded direct paths, including the unused unbounded delta
  implementation and its inert configuration.
- [x] Tightened structural and complexity ratchets when migrated ownership made
  old exceptions and debt entries obsolete.
- [x] At acceptance time, each slice passed its release gate, was merged and
  deployed, and received a live MCP runtime smoke. This is the recorded
  completion status through 2026-09-18, not fresh runtime evidence for a
  future panel; refresh operational state when the next program is designed.

This closes the previously approved implementation sequence. No further slice
is approved merely because it appeared later in the old roadmap.

## Known residual state

These are current facts for the panel to evaluate, not a predetermined backlog:

- Generic entity resolution in `daemon_entity_info.py` still calls
  `client.get_entity` and imports Telethon error types even though the profile
  fact acquisitions themselves are ported.
- The external-import ratchet still names 27 Telethon-owning production
  modules. Some are legitimate final-boundary owners; others are migration
  candidates that have not been freshly classified.
- Activity sweeps and peer resolution, read receipts, scheduled messages,
  transcription, media hydration, account trace, and some realtime enrichment
  still own direct Telegram or Telethon behavior.
- Authentication, connection lifecycle, RPC admission, update transport, and
  inbound Telegram events are expected to remain in infrastructure/runtime
  modules; moving them behind application-domain ports may add ceremony
  without product value.
- The remaining Radon and CRAP baselines include code both inside and outside
  likely migration candidates. Complexity cleanup remains attached to code
  that a chosen slice materially changes; it is not a separate cleanup phase.
- A service-wide proof that no consumers reacquire the same facts through
  different paths has not been completed. Existing observability should be
  used only for intersections that source and tests cannot resolve.

## Candidate work for expert reassessment

The following candidates are deliberately unordered and unapproved. The panel
may merge, split, defer, replace, or reject them after inspecting current code,
runtime evidence, product demand, and remaining complexity.

- [ ] Reclassify all 27 Telethon import owners into accepted final-boundary
  owners and migration candidates before choosing another implementation
  slice.
- [ ] Decide whether generic entity resolution belongs in the existing entity
  profile capability, a shared entity-reference capability, or infrastructure.
- [ ] Evaluate activity search, peer resolution, and account trace together for
  shared authored-message, identity, and peer-reference facts.
- [ ] Evaluate read receipts and scheduled messages as separate lifecycle
  capabilities rather than assuming they belong in one mechanical RPC slice.
- [ ] Evaluate transcription and media hydration around durable media facts,
  download ownership, and retry semantics.
- [ ] Separate outbound enrichment initiated by realtime events from the
  inbound event transport where that separation removes duplicate acquisition
  or mixed policy.
- [ ] Inventory remaining repeated fact acquisition and remote fallbacks, then
  consolidate only overlaps with a clear product benefit.
- [ ] Decide which current local projections need explicit bundle-level
  observation time and completeness before consumers can safely drop remote
  fallbacks.
- [ ] Attach each selected slice to the Radon and CRAP entries it will remove
  or reduce, and reject designs that merely move complexity into wrappers.
- [ ] Tighten the Telethon-import gate toward the final boundary as a result of
  accepted migrations, rather than as an isolated mass-rewrite objective.

## Expert panel mandate

The next panel should start from the deployed system and may overturn the old
ordering. Its output should:

1. Revalidate the product problem: where duplicate acquisition, inconsistent
   local facts, weak freshness/completeness, or mixed ownership still causes a
   real cost.
2. Classify the remaining direct Telegram owners and identify which ones are
   already legitimate adapters, runtime composition, authentication, or
   inbound-event infrastructure.
3. Recommend the smallest coherent next program, normally two to four vertical
   slices, without inventing a PR per checkbox or per RPC.
4. State which candidates should be deferred or rejected and why.
5. For every recommended slice, name the product facts, acquisition owner,
   consumers, local projection, superseded paths, complexity debt, dependencies,
   acceptance criteria, and definition of done.
6. Order slices by product value, duplication removed, architectural leverage,
   and implementation risk. Observability should be added only for unresolved
   intersections.
7. Provide an approximate PR count only after the boundaries are chosen; the
   count is an estimate, not a commitment.

The panel should not assume that every Telegram RPC deserves a domain entity,
that every local field needs its own timestamp, or that completing the final
boundary is more valuable than simplifying a high-cost consumer path.

## Acceptance checklist for every slice

Use this checklist when accepting a PR or deployment that claims progress
toward this roadmap. Leave the boxes empty in the standing document; evaluate
them against the concrete slice under review. These are recurring review
questions, not additional implementation PRs and not cumulative progress boxes.

- [ ] The slice names the product facts, their owner, and all consumers.
- [ ] The application contract contains no Telethon types or Telegram request
  constructors.
- [ ] Telegram request construction and response normalization end inside the
  concrete adapter.
- [ ] Shared facts are acquired once per required observation and reused
  locally by other consumers.
- [ ] Partial, failed, stale, and empty observations cannot become false
  completeness or absence.
- [ ] Restart, retry, cancellation, and stale-writer behavior preserve the
  domain contract.
- [ ] The old direct path and obsolete recovery machinery are removed.
- [ ] The Telethon-import ratchet is unchanged or smaller, never broader.
- [ ] Touched Radon and CRAP debt is lower, or the unchanged debt has a concrete
  documented reason; neither ratchet ceiling increases.
- [ ] Complexity was removed rather than displaced into adapters, wrappers, or
  additional orchestration layers.
- [ ] Targeted contract tests and a real scenario smoke prove the intended
  product behavior.
- [ ] Instrumented paths produce causally correlated timing evidence, while
  uninstrumented or lossy paths are reported as unavailable rather than zero.
- [ ] The release-candidate full gate passes; coverage is collected only as
  input to the CRAP score.
- [ ] The deployed runtime exposes the new behavior and remains healthy.
- [ ] Temporary work and review artifacts created for the slice are removed.

## Long-term definition of done for the architecture

The unchecked boxes below describe the eventual target boundary. They are not
unfinished obligations from the program completed through PR #270, an approved
backlog, or required scope for the next expert panel. The panel should use them
as constraints when choosing what is valuable now and may leave them open.

- [ ] MCP delivery and application services contain no direct Telethon imports
  or client calls.
- [ ] All outbound Telegram operations are reachable only through named domain
  ports and Telegram adapters.
- [ ] All repeated shared-fact acquisition has one owner or a documented,
  evidence-backed reason to remain separate.
- [ ] Local consumers expose truthful freshness and completeness without remote
  fallback traversal.
- [ ] Structural checks enforce the final boundary without a brownfield
  application-layer allowlist.
- [ ] The legacy Radon and CRAP baselines contain no remaining debt entries;
  the architectural slices removed them without a separate cleanup project or
  replacement compatibility complexity.
- [ ] Production telemetry shows no unexplained duplicate acquisition for the
  same fact bundle and observation window.
- [ ] Slow instrumented MCP calls can be attributed to a measured local,
  admission, RPC-execution, or shaping boundary, or explicitly report the
  remaining evidence gap.
- [ ] The operator-facing architecture documentation matches the deployed
  system.
