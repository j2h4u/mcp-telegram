# Telegram Fact Acquisition: Expert Panel Decision

## Decision

Develop the existing modular monolith around typed, domain-owned fact
acquisition operations. Keep the current demand coordinator, RPC admission,
budgets, and FloodWait handling. Permit reuse only when a domain-owned receipt
proves that locally materialized data meets a consumer's freshness and
completeness requirement.

The first vertical slice will combine the duplicated full-user acquisition used
by the Entity Profile full-profile and personal-channel sections. It will not
introduce a global fact cache, a separate acquisition store, durable leases, a
new scheduler, or a repository-wide DDD restructuring.

## Product outcome

For an eligible profile refresh, one Telegram `GetFullUser` response will update
both the full-profile and personal-channel projections. This releases one RPC
attempt for other work and may reduce profile completion latency and FloodWait
pressure. The overall traffic benefit remains to be measured.

## Architecture

The service remains one local Telegram-mirror bounded context with an explicit
MCP boundary. Internally, the following ownership areas guide module contracts:

- Dialog Directory;
- Conversation Organization;
- Conversation Mirror and Repair;
- Entity Profile;
- Activity Evidence.

Reactions, read state, topics, and hydration retain typed module contracts but
do not become independent bounded contexts. Telegram transport, RPC admission,
demand scheduling, and optional singleflight remain infrastructure.

A consumer owns the reason for work, its acceptable age, and its required
completeness. A domain acquisition operation owns the exact Telegram request,
normalization, materialization, and evidence that the materialized facts are
usable. The coordinator continues to own when durable work runs and how many
actual attempts it may make.

## Domain language

- **Demand:** why work is required.
- **Acquisition request:** account/session generation, fact family, typed
  subject, endpoint semantics, selector version, and exact selection.
- **Observation:** a successful Telegram response bounded by acquisition start
  and completion times.
- **Materialization:** typed local facts derived from an observation.
- **Coverage receipt:** a transactional claim about exactly what the
  materialization proves.
- **Freshness requirement:** a consumer-owned maximum observation age.
- **Complete:** the declared selection was authoritatively exhausted.
- **Partial:** useful facts were committed but absence cannot be inferred.
- **Absent:** a successful authoritative response proved nonexistence.
- **Unavailable:** no trustworthy new observation was obtained.
- **Unknown:** no applicable receipt exists.

Fresh and stale are evaluations of a receipt against a consumer requirement;
they are not permanent stored states. Reusing a fact never advances its original
observation time.

## First vertical slice

Create one Entity Profile operation that performs a single `GetFullUser`
request, validates the returned user, and immediately derives bounded normalized
data for the full-profile and personal-channel projections. Do not persist a
raw Telethon object.

Commit both projection outcomes, their independent status and provenance, the
original observation boundary, normalization version, and durable refresh
progress in one transaction. A valid full profile must remain usable when the
personal-channel result is missing or partial. Observed absence of a personal
channel is a TTL-bound result because the user may later attach one.

The `FullUser` observation confirms only fields it actually contains. It does
not renew the freshness of local channel metadata or a locally selected message
preview. Those fields retain their own provenance and observation age.

Protect every projection from an older response that completes after a newer
event or acquisition. Use an observation-time comparison or generation and
revision fence. Matching a section cursor alone is insufficient.

Use the existing profile waiter coalescing for the first slice. Add a new
singleflight mechanism only when independent paths can concurrently issue the
same exact acquisition. A follower timeout must not cancel shared work.

## Correctness invariants

1. A receipt that can suppress a future RPC becomes visible atomically with the
   facts it proves.
2. Timeout, FloodWait, transport failure, malformed or omitted data, persistence
   failure, and crash before commit create no positive receipt.
3. Older observations cannot overwrite newer payloads, tombstones, reactions,
   or receipts.
4. Reuse preserves the original observation boundaries and never renews TTL.
5. A complete empty result may prove absence; a partial or capped result may not.
6. Account/session generation, endpoint semantics, selector version, and exact
   selection are part of acquisition identity.
7. Existing RPC admission and actual-attempt budgets govern every remote call.
8. Telemetry contains aggregate outcomes only, without Telegram identifiers,
   selectors, content, or stable behavioral fingerprints.

## Overlap that remains intentional

Realtime processing, Telegram difference recovery, reconnect recovery, delta
repair, full-history backfill, and activity searches retain their own intents,
cursors, retry rules, and completion criteria. Common message rows do not prove
that these acquisitions are interchangeable.

Bootstrap and full dialog reconciliation remain separate workflows. They may
share protected positive materialization, but only a complete reconciliation
generation may authorize hiding dialogs. Folder publication retains its own
complete staging and publication rule.

An empty hot activity window cannot complete cold history. A reaction aggregate
cannot prove a complete reactor roster. A response for exact message IDs cannot
prove a surrounding interval complete. Cached message data must be read from
the current canonical projection rather than replayed through a writer that
could revive deleted or superseded state.

## Measurement

Extend bounded operational telemetry with:

- actual remote attempts;
- usable, partial, absent, and unavailable observations;
- projections satisfied by each observation;
- reuse and rejection reasons;
- age of reused observations;
- stale-writer rejection;
- profile-card readiness latency;
- queue age and freshness debt;
- prevented requests only when a real acquisition need was satisfied locally.

Returned rows and newly materialized rows measure projection overlap, not by
themselves unnecessary RPCs.

Use an observe-only baseline before enabling reuse. The suggested evaluation
window is seven days before and seven days after enablement, with at least 100
eligible successful profile pairs when production volume permits. This is a
proposed threshold, not a result already measured.

## Acceptance criteria

- One successful `GetFullUser` request satisfies both eligible sections.
- The two sections retain independent applicability, completeness, freshness,
  and failure outcomes.
- Missing channel information cannot invalidate a usable full profile.
- Reuse after restart preserves original observation time.
- TTL expiry causes a new acquisition when the consumer requires it.
- Timeout, FloodWait, invalid response, failed persistence, and crash cannot
  produce a positive receipt or advance durable progress.
- A concurrent newer observation wins over a slower older response.
- Cancelling one waiting caller cannot cancel the durable refresh.
- Existing actual-attempt budgets remain enforced.
- Historical database schemas migrate safely and the normal MCP client works
  after deployment.

## Expansion rule

Do not extract a shared acquisition framework until two independent ownership
areas implement the same identity, observation, receipt, and concurrency rules.
`FullChannel` may be a useful next profile optimization, but it does not count as
an independent domain proving the abstraction.

Later candidates enter observe-only mode first. Broader sharing requires a
measured rate of semantically identical, safely reusable acquisitions and zero
confirmed correctness violations. Catalog traversals, activity pages, reaction
details, fragments, and history ranges each require their own proof of negative
results, continuation, completion, and crash recovery.

## No-go conditions

Stop or roll back if reuse causes false freshness or completeness, hides data,
revives deleted facts, permits stale overwrites, skips required recovery,
violates RPC budgets, or cannot link a positive receipt to the corresponding
materialized facts.

Rollback disables combined reuse while leaving compatible additive data in
place. Existing acquisition paths remain available.

## Explicit non-goals

- a universal fact cache;
- event sourcing;
- raw Telethon-object persistence;
- durable acquisition leases;
- new processes or databases;
- a universal TTL;
- merging recovery workflows;
- deriving range completeness from stored-row presence;
- rearranging repository files for architectural appearance;
- changing the external MCP contract for this internal optimization.
