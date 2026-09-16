# Telegram Behind Domain Ports

Status: working architectural compass  
Last reviewed: 2026-09-13

This document keeps the target architecture and the next acceptance slices in
one place. Use it when accepting each slice to determine whether the service is
moving toward the intended boundary or merely relocating direct Telegram
coupling.

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

## Current state

The MCP delivery boundary already satisfies the target: production modules
under `tools/` have no direct Telethon imports and communicate with the daemon
API.

The boundary below delivery is transitional. Folders, topics, reactions, and
messages have recognizable Telegram adapter modules. The canonical dialog
directory has a dedicated raw TL adapter and owns account-wide dialog
acquisition. Other application and worker modules still import Telethon or
invoke client methods directly, especially entity profile acquisition,
daemon-level orchestration, message synchronization, activity sweeps, account
trace, read receipts, scheduled messages, transcription, and realtime event
enrichment.

The current external-import check is a ratchet over the brownfield state. It
prevents an unreviewed new Telethon importer, but its allowlist still recognizes
30 production import owners. Passing that check does not yet prove that
Telegram is hidden behind domain ports.

## Action plan

The checkboxes below are decision and acceptance points, not a one-checkbox-per-PR
plan. Implement the roadmap as a small sequence of coherent vertical slices.
One slice should normally close several related action points: introduce the
port and adapter, move one proven acquisition path, simplify the affected
legacy functions, remove the superseded path, and verify the resulting product
behavior together. Complexity cleanup is not a separate workstream or PR
series. The expected order of magnitude is four to seven substantial PRs, but
evidence and cohesion determine the actual boundaries.

Before the first substantial acquisition refactor, land one small enabling PR
for causal request observability. It does not increase the expected number of
substantial domain slices and does not require a baseline observation period.
Deploy it, verify that the evidence is emitted, and proceed directly to the
next architectural slice.

### Lock the intended boundary

- [ ] Add an explicit architecture test that permits Telethon imports only in
  Telegram adapters, inbound Telegram event adapters, transport/runtime
  composition, authentication, and narrowly documented compatibility leaves.
- [ ] Classify every currently allowed Telethon-importing module as a target
  adapter, composition owner, inbound event adapter, or migration candidate.
- [ ] Give every migration candidate a destination domain capability and a
  named owner of the facts it acquires.
- [ ] Make the allowed-import set shrink with each accepted slice; do not
  replace exact exceptions with a broader package-level allowance.

### Enable causal request observability

- [ ] Generate one opaque operation ID at the MCP boundary and propagate it
  explicitly through daemon requests without exposing it in product schemas.
- [ ] Persist a bounded, versioned daemon timing observation in the existing
  `runtime_observations` store and correlate it with the terminal `mcp.call`
  observation by operation ID.
- [ ] Instrument `list_messages` first with a closed route vocabulary covering
  local history, local context, local non-sent state, ordinary Telegram
  fallback, and Telegram topic fallback.
- [ ] Measure fixed causal boundaries: resolution, local projection, Telegram
  fallback, RPC admission wait, RPC execution, and response shaping. Preserve
  missing measurements as unavailable and nested measurements as nested.
- [ ] Record actual RPC attempt count and attempted fallback even when the
  fallback fails, returns no rows, or the final response remains local.
- [ ] Extend the existing operator summary so a slow call shows its largest
  measured contributor, attribution completeness, and any unattributed time.
- [ ] Keep the payload privacy-safe: no arguments, selectors, message text,
  names, peer or message IDs, cursor values, raw exceptions, SQL, or response
  bodies.
- [ ] Reuse the existing TTL, row cap, asynchronous loss reporting, and
  operator command. Do not add a telemetry database, tracing backend,
  dashboard, exporter, sampler, or per-statement/per-message events.
- [ ] Prove local delay, admission delay, RPC execution delay, fallback,
  shaping, cancellation, concurrency, telemetry loss, and legacy-row behavior
  with deterministic tests and one live devtools-client smoke.
- [ ] Reduce any Radon or CRAP legacy debt touched by this enabling slice; do
  not add a generic tracing abstraction or increase either baseline.
- [ ] Deploy the slice and proceed directly to `GetFullChat`; do not wait for a
  weekly baseline or make later domain work contingent on traffic volume.

### Complete the next proven overlap

- [ ] Introduce a group-profile port and Telegram adapter for the existing
  `GetFullChat` acquisition.
- [ ] Let one successful group observation materialize both the full-profile
  facts and the contact-overlap facts atomically.
- [ ] Make the second consumer reuse the same observation locally without an
  additional RPC.
- [ ] Remove the superseded direct `GetFullChat` path from the application
  service after parity is proven.
- [ ] Prove with deterministic tests that one group refresh performs one
  `GetFullChat` attempt and produces both domain sections.

### Extract entity-profile acquisition

- [ ] Define domain ports for user, channel, and legacy-group profile facts
  currently acquired inside daemon application services.
- [ ] Move `GetFullUser`, `GetFullChannel`, `GetFullChat`, participant, common
  chat, avatar, and related request construction into capability-specific
  Telegram adapters.
- [ ] Keep profile orchestration, section ownership, generation fences, and
  local publication in application/domain code using transport-neutral
  contracts.
- [ ] Identify additional response pairs that obtain the same facts and merge
  only overlaps supported by source and test evidence.
- [ ] Remove direct Telethon imports from the entity-profile application path.

### Extract remaining acquisition paths

- [ ] Put history and gap synchronization behind message-history ports while
  preserving their distinct progress and completeness semantics.
- [ ] Put activity search and peer resolution behind activity-domain ports.
- [ ] Put read-receipt, scheduled-message, transcription, media hydration, and
  account-trace RPCs behind their owning capability ports.
- [ ] Separate outbound enrichment triggered by realtime events from the
  inbound Telethon event adapter.
- [ ] Keep authentication, connection lifecycle, update transport, and adapter
  construction in the composition/runtime boundary.

### Consolidate shared facts

- [ ] For each extracted capability, list which product facts each consumer
  needs and which Telegram observations can supply them.
- [ ] Detect consumers that acquire the same facts through different RPCs or
  pagination strategies.
- [ ] Assign one owner and one local projection where shared acquisition has a
  clear product benefit.
- [ ] Record observation time and completeness at the smallest coherent domain
  bundle rather than mechanically timestamping every column.
- [ ] Remove remote fallbacks from consumers once the local projection has an
  honest stale, incomplete, and unavailable contract.
- [ ] Add focused telemetry only for overlaps that cannot be resolved from
  source, tests, and existing runtime observations.

### Pay down legacy complexity

- [ ] Assign every existing Radon and CRAP legacy baseline entry to the
  architectural slice that already needs to change or remove that code.
- [ ] Identify the Radon and CRAP baseline entries owned or materially touched
  by each vertical slice before implementation.
- [ ] Delete superseded branches, compatibility wrappers, duplicate
  orchestration, and obsolete recovery code as part of the same slice.
- [ ] Split mixed application/RPC functions along the accepted domain boundary
  so that the resulting units have coherent responsibilities and lower
  cyclomatic complexity.
- [ ] Tighten or remove affected Radon and CRAP baseline entries whenever the
  measured debt falls; do not preserve an obsolete ceiling for convenience.
- [ ] Require an explicit architectural reason when a touched legacy baseline
  cannot be reduced, and prevent the slice from increasing either ceiling.
- [ ] Prefer deletion and direct domain contracts over forwarding layers that
  merely move complexity or inflate the call graph.
- [ ] Remove the final legacy baseline entries within the last architectural
  slice; do not schedule a separate complexity-cleanup phase afterward.

### Reach the final boundary

- [ ] Reduce the Telethon importer allowlist to the accepted adapter,
  composition, authentication, transport, and inbound-event modules.
- [ ] Make the structural gate fail on any Telethon import from delivery,
  application, domain contract, or persistence modules.
- [ ] Verify that every registered RPC demand maps to a named domain capability
  and acquisition owner.
- [ ] Verify that no MCP scenario can bypass a published local projection to
  repeat account-wide or shared-fact acquisition.
- [ ] Update the architecture map and remove this roadmap when all remaining
  items have become enforced invariants.

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

## Definition of done for the roadmap

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
