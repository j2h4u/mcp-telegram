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

## Implementation adjudication addendum

Source-level discovery exposed a gap between the architectural decision and the
current progressive profile transaction. A section cursor alone cannot prove
that two section outcomes belong to the same logical refresh, cannot survive an
ABA return to the same cursor, and cannot prevent an older response from
overwriting a newer canonical observation. The implementation therefore uses an
additive, Entity Profile-specific generation and revision fence.

Extend the existing profile state rather than creating an acquisition store:

- each logical refresh has a non-reused generation, start time, captured pair
  eligibility, and a persisted follow-up-demand marker;
- each section may carry bounded Entity Profile acquisition evidence containing
  its generation, outcome, provenance, normalization version, and original
  observation interval;
- canonical entity/profile writes advance a revision used for compare-and-swap
  protection of response-bearing commits;
- migration preserves old facts, cursors, retry state, and pending work but
  invents no receipt, provenance, completeness, or observation time.

The combined operation validates one `GetFullUser` envelope and normalizes two
independent outcomes. It commits both projections, their evidence, and durable
progress in one short transaction after checking generation, cursor, and
revision. It advances from `full_profile` to `common_chats`; the intervening
sections retain their established order. When the cursor later reaches
`personal_channel`, a terminal outcome for the same generation advances locally
without changing its observation time or making an unavailable result reusable
in a later generation.

Same-generation completion and cross-generation freshness reuse are different
claims. An unavailable or partial personal-channel result may complete that
operation in the current generation, while only an applicable positive or
authoritative-absence receipt satisfying identity, coverage, and TTL may avoid
work in a later generation. If a new freshness demand arrives after a completed
section expires while the current generation is still running, the persisted
follow-up marker starts a new generation when the current one terminates.

Reuse identity includes the authenticated account and session generation, typed
entity, `users.GetFullUser` semantics, normalization version, and declared
fields. Missing or invalid identity disables reuse. No auth key, Telegram ID,
generation value, selector, or stable fingerprint is exposed through MCP or
operational telemetry.

Observation starts immediately before the admitted operation and completes
after response validation. Freshness uses the conservative original start and
expires at equality with the TTL. Commit, restart, recomposition, and later
section advancement never renew it. Invalid time boundaries cannot authorize
reuse.

Fields acquired from `FullUser` have an explicit ownership list. Local channel
metadata, folder information, and message previews retain their own provenance
and are recomposed from current canonical projections. A cached preview is never
replayed through a message writer, so edits and tombstones remain authoritative.

A valid envelope with optional channel deficiencies may commit a usable profile
and an honest partial or unavailable channel outcome. Invalid envelope identity,
transport failure, timeout, or FloodWait creates no positive evidence or
successful cursor advancement. Retry bookkeeping may still be persisted.

The feature has a migration-aware configuration switch. Disabled mode preserves
the two existing acquisitions while retaining new fencing and observe-only
eligibility metrics. Enabled mode combines the pair. The supported rollback is
disabling the switch on a release that understands the additive schema; an
arbitrary older binary is not promised to support that schema.

This addendum remains domain-local. It introduces no generic fact cache, raw
Telegram response persistence, second scheduler, new database, durable lease,
or generalized range-coverage logic.

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

### Product acceptance

An **eligible pair** is a User or Bot refresh where both `full_profile` and
`personal_channel` require acquisition at refresh start. A **clean success** is
an eligible pair whose first valid matching `GetFullUser` response commits. A
pair is **ready** when both sections have durable, honest outcomes; the channel
outcome may be present, authoritatively absent, partial, or unavailable.

The slice passes product acceptance when all of the following are proven:

| ID | Criterion | Required evidence |
|---|---|---|
| PA1 | Every clean success uses exactly one successful `GetFullUser` dispatch and produces independent outcomes for both sections. | Controlled gateway trace and committed database state. |
| PA2 | Existing User and Bot fields and applicability rules remain semantically unchanged for identical source facts. | Golden MCP comparisons for every supported entity class and mismatch case. |
| PA3 | A valid profile remains usable when channel information is absent, partial, malformed, or unavailable. | Scenario results and persisted state before and after restart. |
| PA4 | A `FullUser` observation does not renew the age of local channel metadata or message preview. | Before-and-after provenance and observation timestamps. |
| PA5 | Reuse before TTL causes no Telegram attempt; the documented TTL boundary and expiry cause acquisition when required. Restart does not renew TTL. | Boundary scenarios at immediately before, at, and after expiry, including reopen. |
| PA6 | Successful `get_entity_info` p95 latency does not regress by more than 10% or 100 ms, whichever allowance is larger. Foreground timeout/error rate does not rise by more than one absolute percentage point after a sufficient sample. | Controlled latency sample and comparable production aggregates. |
| PA7 | The public MCP schema and field surface do not expose acquisition keys, receipt state, normalization versions, or raw Telegram objects. | Schema diff and representative real responses. |
| PA8 | Profile, queue-freshness, and recovery behavior outside the paired acquisition remains within the accepted pre-change behavior. | Existing scenario corpus and RPC sequence comparison outside the combined call. |

Any removed or retyped public field, false complete result, renewed provenance,
hidden usable profile, or unexplained new RPC fails product acceptance.

### Failure and recovery acceptance

The following scenarios are mandatory and must use a file-backed database where
restart or transaction behavior matters:

| ID | Scenario | Required result |
|---|---|---|
| FR1 | Timeout, FloodWait, transport failure, wrong entity, missing envelope, or malformed response. | No positive receipt or successful progress; previous valid data survives; retry and actual-attempt accounting remain honest. |
| FR2 | Persistence fails after either projection or receipt write but before commit. | Reopen shows none of the new data, receipt, or progress. |
| FR3 | Process dies after Telegram responds but before commit. | Work remains required after restart; a repeated RPC is allowed. |
| FR4 | Process dies after commit but before waiter notification. | Committed sections are reused within their original freshness window and remaining work continues. |
| FR5 | An older response finishes after a newer observation commits, including reuse of the same section cursor. | The newer payload, status, absence result, and timestamps remain authoritative; stale-writer rejection is observable. |
| FR6 | One waiter cancels or times out while another remains. | Durable work continues and the remaining waiter receives a result or honest intermediate state. |
| FR7 | Concurrent identical requests arrive during queue pressure and shutdown. | No extra equivalent acquisition, lost waiters, false terminal success, or leaked task. |
| FR8 | Telemetry fails or drops events. | Domain correctness and reuse decisions remain unaffected; measurement loss is reported. |
| FR9 | Combined behavior is disabled on the migrated database and later re-enabled. | Previous acquisition path, durable progress, MCP reads, and new receipts remain mutually safe. |

Tests cannot promise one external RPC across FR3: at-least-once reacquisition is
the correct outcome after a response whose commit cannot be proven.

## Definition of Done

Completion has two separate verdicts: **`SHIPPED_VERIFIED`** for engineering
and live correctness, and **`PRODUCT_VALIDATED`** after sufficient production
evidence. Green tests alone grant neither verdict.

For every item the final verifier records `PASS`, `FAIL`, or `UNPROVEN`, the
source state, command or scenario, time, expected and actual outcome, and an
artifact reference. A skipped check, empty sample, or absence of log errors is
not `PASS`. Evidence becomes stale when relevant implementation, schema,
configuration, or dependencies change afterward.

### Mandatory ship gate

The slice is `SHIPPED_VERIFIED` only when an independent final verifier confirms
all of the following:

| Area | Definition of done |
|---|---|
| Code ownership | All active `FullUser` paths are mapped. Exactly the intended pair is combined; every remaining call has a documented distinct purpose. No global cache, new scheduler, separate observation store, durable lease, or raw Telethon persistence was introduced. |
| Projection contract | Every written field has a named owner, source, observation boundary, applicability rule, and completeness meaning. Shared compatibility data cannot accidentally acquire section-wide freshness. |
| Atomicity and races | Both section outcomes, receipts, and durable progress commit atomically. Every writer is covered by an observation or revision fence. FR1-FR9 pass. |
| Identity | Account/session generation, typed entity, endpoint semantics, selector or normalization version, and required fields participate in or are unambiguously inherited by reuse identity. Each mismatch is tested. |
| Migration | Fresh, immediately preceding, and every supported historical schema converge without data loss. Opening twice is idempotent. Interrupted migration cannot advertise success and recovers on reopen. Old rows receive no invented provenance. |
| Compatibility | MCP input/output schemas and accepted response fixtures remain compatible. Other profile sections, startup identity, RPC admission, budgets, queue semantics, and FloodWait behavior retain their contracts. |
| Tests | New acceptance scenarios and affected regression tests pass, followed on the final candidate by `just check`, full `just unit`, `just crap-check`, and `just docker-build-check`, without weaker gates or unjustified exclusions. Required GitHub checks are green for the final PR head. |
| Independent verification | After the last relevant change, an independent verifier reproduces the successful pair, transaction failure, both crash boundaries, stale-writer race, and a real historical migration, or validates immutable artifacts and reruns the affected risk. |
| Deployment provenance | The deployed container is traced to the verified source and image; schema and configuration match. Pre-deploy work, health, queues, image, rollback target, and a WAL-consistent recovery copy are recorded. |
| Live behavior | Daemon and HTTP health remain stable through an observation window; no unexplained restart, OOM, or task failure appears; queued work progresses. Canonical `list-tools` and integration smoke pass through `devtools/mcp_client/cli.py`. |
| Live product scenario | A real `get_entity_info` call for an eligible profile produces both section outcomes with one attributable actual attempt. Reuse within TTL and behavior after restart are demonstrated without editing production state or shortening production TTL. |
| Telemetry and privacy | Runtime observations prove the new branch and distinguish actual attempts, satisfied projections, reuse rejection, stale-writer rejection, and measurement loss. No Telegram IDs, selectors, content, raw exceptions, or stable behavioral fingerprints are emitted. |
| Rollback | A compatible rollback image or supported switch is documented and tested on the migrated database through enabled, disabled, and re-enabled states. Additive data is retained. |
| Documentation and cleanup | Architecture, metric definitions, evidence matrix, rollback runbook, and honest user-facing claims are current. Temporary resources are owner-checked and removed; retained rollback artifacts have a purpose and retention period. |

The final verifier emits one of these states:

| Verdict | Meaning |
|---|---|
| `BLOCKED` | At least one mandatory criterion is `FAIL` or `UNPROVEN`; the exact evidence gap and next action are named. |
| `READY_TO_DEPLOY` | Code, migration, checks, and rollback are proven; live deployment criteria remain. |
| `SHIPPED_VERIFIED` | Every mandatory code, failure, test, deployment, live, rollback, and documentation criterion is proven. Product-effect status is reported separately. |
| `PRODUCT_VALIDATED` | `SHIPPED_VERIFIED` plus all post-deploy product criteria below are proven. |

### Post-deploy product validation

Before enablement, capture an observe-only baseline with fixed metric definitions,
code and configuration versions, window boundaries, eligible pairs, clean
successes, actual attempts, section outcomes, readiness latency, foreground
timeouts, queue freshness debt, FloodWaits, and telemetry loss.

The target evaluation uses comparable seven-day baseline and post-change
windows and at least 100 naturally occurring eligible pairs. Do not generate
artificial Telegram traffic to reach the sample. When volume is lower, the
slice may remain `SHIPPED_VERIFIED`, while the product effect remains
`UNPROVEN`.

`PRODUCT_VALIDATED` requires:

| ID | Criterion |
|---|---|
| PV1 | At least 95% of clean successes use exactly one attempt; every extra attempt has an attributable failure or retry reason. |
| PV2 | Actual attempts per completed eligible pair fall by at least 40% from a demonstrated duplicate baseline. If the baseline median is not two or its mean is below 1.8, no production RPC-reduction claim is made. |
| PV3 | PA1-PA8 remain satisfied in production, with zero confirmed false freshness, false completeness, stale overwrite, hidden data, or receipt/materialization mismatch. |
| PV4 | Latency, timeout/error rate, queue age, freshness debt, usable/partial/unavailable outcomes, recovery, and FloodWait are compared with denominators, exclusions, telemetry loss, and known workload changes. |
| PV5 | Prevented requests count only real acquisition needs satisfied by valid evidence; projections built and rows inserted are reported separately. |
| PV6 | The final report distinguishes the proven local saving from any claim about total account traffic, latency, or FloodWait. |

Latency and error thresholds must be fixed before inspecting post-change
results. If natural volume remains insufficient after fourteen days, the
official state is `SHIPPED_VERIFIED; PRODUCT EFFECT UNPROVEN`.

### Independent final-verification protocol

1. Pin the final source, image, configuration, schema, and evidence set; stop if
   deployed provenance differs.
2. Inspect the final diff and all writers, then grade the code, identity,
   projection, atomicity, migration, and compatibility gates.
3. Grade PA1-PA8 and reproduce every insufficiently proven FR scenario. Do not
   substitute a full suite for a missing fault or race scenario.
4. Validate final-candidate local and CI gates. Confirm heavyweight jobs ran for
   the final PR head rather than passing through skipped aggregation.
5. Inspect pre-deploy recovery evidence, deployment provenance, runtime health,
   canonical MCP smokes, the live profile scenario, telemetry, and rollback.
   Destructive crash and race injection stays outside production.
6. Emit exactly one pipeline verdict and list every `FAIL` or `UNPROVEN` item.
   Do not upgrade `SHIPPED_VERIFIED` to `PRODUCT_VALIDATED` while the observation
   window, sample, or telemetry evidence is incomplete.

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
