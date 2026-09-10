# Telegram Fact Acquisition: Sol and Astra Expert Inputs

## Panel composition

This round used Sol for product-domain and data-correctness judgment, and Astra
for long-horizon architecture and adversarial review. Luna and Terra findings
were treated only as the evidence dossier in
`telegram-fact-acquisition-panel-round-1.md`.

## Strong consensus

- Do not build a global generic fact cache.
- Do not begin with a repository-wide DDD restructuring.
- Keep the current demand coordinator and RPC admission path as the owners of
  scheduling, budgets, and FloodWait behavior.
- Introduce typed, domain-owned acquisition observations and coverage receipts
  inside the current monolith.
- Preserve distinct realtime, difference, reconnect, delta, and full-history
  intentions. Shared rows do not make their recovery guarantees equivalent.
- Start with the duplicated `GetFullUser` acquisition used by the full-profile
  and personal-channel sections. One normalized observation may feed multiple
  projections, while each projection retains independent status, freshness,
  applicability, and provenance.
- Use process-local singleflight only for concurrent, exactly equivalent point
  acquisitions. Sequential durable duplication requires persisted evidence,
  not singleflight.
- Do not introduce durable leases in the one-process runtime without a failure
  that existing durable domain cursors cannot handle.
- Freshness is evaluated from the original observation time against a
  consumer-owned requirement. Reuse must never renew the observation time.
- Projection data and any receipt that authorizes skipping a later RPC must be
  committed atomically and protected from stale writers.

## Proposed language

- **Demand:** why a consumer requires work.
- **Acquisition request:** account/session, fact family, typed subject, endpoint
  semantics, selector schema version, and exact normalized selection.
- **Observation:** a successful Telegram response with acquisition start and
  completion boundaries.
- **Materialization:** typed local facts derived from an observation.
- **Coverage receipt:** a transactional claim about precisely what the
  materialization proves.
- **Freshness requirement:** the consumer's maximum acceptable observation age.
- **Complete:** the declared selection was authoritatively covered, including
  EOF or an exhausted continuation where relevant.
- **Partial:** useful facts were committed but absence cannot be inferred.
- **Absent:** an authoritative successful response proved nonexistence.
- **Unavailable:** no trustworthy new observation was obtained.
- **Unknown:** no applicable receipt exists.

`Fresh` and `stale` are evaluations of a receipt against a requirement rather
than permanent stored states.

## Credible domain boundaries

The experts identified Dialog Directory, Conversation Organization, Conversation
Mirror, Entity Profile, and Activity Evidence as useful ownership areas.
Reactions, read receipts, topics, and hydration require typed module contracts
but do not currently justify independent bounded contexts. RPC admission,
Telethon translation, scheduling, and singleflight are infrastructure.

The strongest strategic view is that the service-local Telegram mirror and its
external consumers form the only clear bounded-context boundary today. Internal
domain services should be modular capabilities, not separately persisted or
deployed contexts, until they gain different models, permissions, retention
rules, or independent life cycles.

## Adversarial corrections

- A cache hit should expose the current canonical projection. Replaying an old
  acquired message through the writer can overwrite newer edits, reactions, or
  tombstones.
- Hot and cold activity requests are equivalent only when every bound, filter,
  limit, and endpoint semantic matches. An empty hot window cannot prove cold
  history completion.
- Reaction aggregate freshness cannot satisfy completeness of the individual
  reactor roster.
- A fragment fetch proves outcomes for its exact requested ID set. It does not
  prove an interval complete when omitted IDs are discarded.
- Positive dialog facts may be reusable without transferring authority to hide
  dialogs. Negative membership evidence belongs to a completed reconciliation
  generation.
- Atomic commit is insufficient without observation-time comparison or a
  generation fence: a slow older response can overwrite a newer event.
- Shared rows demonstrate materialization overlap, not acquisition equivalence.

## First slice proposed by all experts

Create an Entity Profile domain acquisition operation that performs one
`GetFullUser` request and immediately normalizes the bounded fields needed by
the full-profile and personal-channel projections. Commit their independent
section outcomes with the same original observation boundary. Do not persist a
raw Telethon object.

The slice must prove:

- one remote full-user request satisfies both eligible sections;
- missing personal-channel data does not invalidate a valid full profile;
- reuse does not extend observation freshness;
- timeout, FloodWait, malformed response, or persistence failure creates no
  positive receipt;
- an older response cannot overwrite a newer observation;
- restart between sections retains correct progress;
- concurrent exact requests may join without coupling cancellation;
- remote attempts, projections satisfied, reuse decisions, latency, and saved
  attempts are observable without Telegram identifiers or content.

## Later candidates

After a second domain demonstrates the same need, extract the small common
observation and receipt vocabulary. Likely candidates are full-channel profile
acquisition and point message retrieval. Catalog traversal, activity pages,
reaction detail, fragments, and history ranges stay observe-only until exact
selection identity and authoritative completeness can be demonstrated.

## Strategic decision still required

The final panel must decide:

1. Whether the first slice should persist a reusable normalized observation or
   merely commit both projections in one operation.
2. Whether a shared acquisition kernel should exist immediately as a minimal
   interface or be extracted only after the second implementation.
3. Which metrics and production observation window are sufficient to authorize
   the second domain.
4. Whether bootstrap and full dialog reconciliation should ever share one
   workflow or only share positive materialization.
5. What explicit non-goals keep the first phase from becoming a generic cache or
   a DDD file-moving exercise.
