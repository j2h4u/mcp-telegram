# Telegram Domain Ports: Expert Panel Recommendation

Status: accepted; both recommended slices deployed

Release completed with the retired `[freshness.reactions]` section removed and
the durable reaction-detail pacing window enabled in the host-owned config.

Date: 2026-09-18

## Decision

The panel recommends a short program of two vertical slices. Current evidence
does not justify continuing the previous roadmap as a broad architecture
program or implementing a migration for every remaining Telethon owner.

The strongest current signal is `message_fact_refresh`: 14,276 of 43,216
Telegram RPC attempts in the reviewed 24-hour window. The source and runtime
evidence split that pressure into 5,675 exact-read-date attempts and 8,601
reaction attempts. Both paths also have correctness problems: completed facts
can be queried again, later failures can erase last-good facts, and reaction
aggregate freshness is conflated with reactor-detail completeness.

Recommended order:

1. Make exact outbox read dates terminal and monotonic.
2. Separate reaction aggregate ownership from reactor-detail lifecycle.

Expected size: three to four deployable PRs. Conditional activity and topic
work is outside that estimate.

## Slice 1: terminal exact outbox read dates

### Product problem

An exact `read_at` is immutable once Telegram confirms it, but the current TTL
selection can request it again after a successful result. A later `missing` or
`unavailable` observation can replace the stored date with `NULL`. This spends
Telegram capacity and can make a known fact disappear from the local product.

### Outcome and scope

- `complete` with a non-null `read_at` becomes terminal.
- `missing` and `unavailable` remain distinct retryable states with bounded
  retry policy.
- Candidate selection excludes terminal rows.
- Persistence is monotonic: later failure, empty response, stale writer, or
  cancellation cannot erase a last-good `read_at`.
- Existing MCP and SQLite read shapes remain stable.
- The status-independent TTL reselect and unconditional replacement path are
  removed.

### Ownership and dependencies

This slice has no architectural dependency. The exact-read-date acquisition
owner remains the message-fact refresh capability through the existing
read-receipt gateway. Its consumers are local message/read-marker projections
and MCP message reads. The existing `message_read_facts` projection remains the
durable source of `read_at`, attempt status, and observation time; no new table
or public schema is required.

### Acceptance criteria

- A terminal row is never selected after any number of TTL intervals.
- `complete -> missing` and `complete -> unavailable` races retain the date and
  terminal status.
- `missing -> complete` and `unavailable -> complete` publish the date.
- A failure with no last-good fact remains nullable and retryable.
- Cancellation or restart leaves uncompleted candidates due without changing
  terminal facts.
- Privacy-safe telemetry distinguishes first attempts, retryable attempts, and
  suppressed terminal candidates.

### Definition of done

- Targeted state-transition and stale-writer tests pass.
- The release-candidate full gate and CRAP ratchet pass.
- The deployed runtime remains healthy.
- A live local-only smoke confirms a stored date survives restart and a later
  coordinator cycle.
- Runtime evidence shows no Telegram RPC for already terminal rows. Acceptance
  does not depend on an immediate percentage reduction while old backlog is
  still being consumed.

## Slice 2: reaction aggregate and reactor-detail lifecycle

### Product problem

`UpdateMessageReactions` already carries the aggregate reaction payload, but
the realtime handler performs another message lookup and reads the aggregate
from that response. Background refresh also treats aggregate counters and the
completeness of the reactor list as one freshness problem. A push can therefore
cause a redundant RPC, while a failed detail refresh can erase last-good detail
or make its completeness ambiguous.

### Outcome and scope

- Realtime applies aggregate counts directly from the update, including an
  empty aggregate; the extra message lookup is deleted.
- Aggregate observations receive their own observation time, source, and
  stale-writer fence.
- Reactor details use a separate `complete`, `partial`, `stale`, or
  `unavailable` lifecycle.
- A newer aggregate observation invalidates detail completeness without
  deleting last-good detail rows.
- Detail acquisition calls `GetMessageReactionsList` directly; it does not
  first refetch the message.
- Complete detail is terminal until a newer aggregate/update invalidates it.
- History, delta, edit, and realtime aggregate writers share one persistence
  contract.
- Materially changed aggregates re-page detail; identical observations advance
  their ordering boundary without invalidating an existing detail result.
- Detail work is bounded by a durable global pacing window: five claimed pages
  per 600 seconds by default. A FloodWait stops remaining probes and can only
  extend the persisted release boundary.

### Ownership and dependencies

This slice follows Slice 1 because both change `message_fact_refresh` candidate
semantics and telemetry. The reaction capability owns aggregate and detail
acquisition. Realtime/history writers publish aggregate observations, while
the existing reaction detail gateway owns paginated reactor acquisition. MCP
message reads, formatting, and statistics consume the existing reaction
projections; aggregate receipts and detail lifecycle state remain local facts.

### Acceptance criteria

- A raw reaction update performs zero message-lookup RPCs.
- Persisted aggregate rows exactly match the update, including removal of the
  last reaction.
- A push never marks reactor details complete.
- Complete details are not reacquired without a newer aggregate observation.
- One newer update creates one resumable detail demand.
- Partial or unavailable detail does not delete last-good rows.
- An older history observation cannot overwrite a newer realtime aggregate.
- Telemetry distinguishes aggregate observations, detail attempts,
  invalidations, and terminal detail outcomes without Telegram identifiers.

### Definition of done

- Contract, ordering, race, retry, and restart tests pass.
- Superseded realtime lookup and combined-freshness paths are removed.
- The release-candidate full gate and CRAP ratchet pass.
- A live reaction-update smoke shows zero reaction message lookups and truthful
  detail status while the service remains healthy.
- Repeat attempts for complete, non-invalidated detail disappear after the
  initial backlog is consumed.

## Conditional candidates

### One initial owner for per-peer activity bootstrap

Source shows that HotSweep and ColdBackfill can both start from the newest page
for the same peer. Current runtime does not justify immediate work: the reviewed
window contained 29 hot RPCs, no cold RPCs, and all 159 known peers were already
cold-complete.

Measure at least five genuinely new peers within a 14-day window. If fewer than
five appear, the candidate remains deferred regardless of elapsed time. Record
pages and RPCs by tier, genuinely new message keys, overlap, time to first
visibility, and time to history floor. Activate the slice only if at least two
of the five peers repeat the same initial page across tiers, or the sample
projects at least 100 redundant page RPCs per 14 days. The minimal design
should assign one owner to the initial page while preserving the different
hot-freshness and cold-completeness contracts.

### Durable topic catalog receipt

The source has a real semantic gap: an empty successful topic result leaves no
durable receipt, and the one-page limit of 100 does not publish partial versus
complete status. Current product pressure is low: the largest observed catalog
had 16 topics, and `list_topics` was used 89 times in the reviewed 30-day
window.

Measure refresh triggers, repeated empty results, Telegram-reported totals,
returned counts, pages, and RPCs. Any observed catalog that reaches or exceeds
the page boundary is an immediate correctness trigger. Repeated empty refreshes
alone should be prioritized by measured cost.

## Deferred or rejected now

- Defer generic Entity Addressing and linked-chat ownership until repeated
  acquisition of the same fact and observation window is measured.
- Defer a shared Authored Message Evidence port and Account Trace migration;
  current use and RPC volume are low, and arbitrary-account evidence has
  different completeness semantics from own activity.
- Reject a Scheduled Queue architecture slice for now. It already has a single
  reconciler, generation fencing, durable discovery/repair, and truthful
  lifecycle states; moving the call behind another interface does not itself
  remove the observed RPC volume. Reconsider only after measuring discovery
  eligibility, non-empty yield, entity-resolution misses, and detection
  latency. If yield is zero or negligible, tune cadence/admission before
  considering a new port.
- Defer media and transcription restructuring until a product or operational
  failure appears. Do not invent a media-download subsystem; production has no
  such path.
- Do not migrate authentication, connection lifecycle, RPC admission,
  FloodWait policy, update transport, or inbound event registration into
  domain ports.
- Do not run a mass rewrite of all 27 Telethon import owners. Tighten the
  allowlist only as a consequence of a valuable vertical slice.

## Complexity policy

The Radon baseline contains stale entries: 53 recorded entries versus 31
currently above the threshold. Tighten the baseline in the first implementation
PR as release hygiene, not as a standalone cleanup slice. Neither selected
slice owns a current CRAP-baseline entry, so unrelated CRAP cleanup is outside
scope. Any complexity touched by the selected paths must decrease or remain
below the ratchet; wrappers that merely relocate branching are not acceptable.

## Panel conflicts and resolution

- The DDD position proposed Entity Addressing, Authored Message Evidence,
  Scheduled Queue, and realtime separation. These remain plausible long-term
  boundaries but lost priority because current evidence does not show enough
  product cost to justify seven to ten PRs.
- The Kaizen position correctly identified direct reaction-payload application
  and duplicate hot/cold bootstrap. Its reaction fix was expanded only enough
  to cover the larger confirmed correctness risk in reactor-detail lifecycle;
  activity remains conditional because current RPC pressure is negligible.
- Product and SRE positions agreed on exact read dates first and on preserving
  last-good facts. That recommendation won because it combines large confirmed
  pressure with a concrete user-visible correctness failure; the entire
  `message_fact_refresh` capability is the largest confirmed pressure in this
  program.
- Product's durable topic-receipt proposal remains semantically correct but
  lost priority because current use is low and no partial catalog has been
  observed.
- SRE's unified activity owner and Account Trace port remain valid options
  after overlap and yield measurements, but current traffic does not justify
  implementing them now.

The program should stop after the two selected slices unless the conditional
measurements cross their stated triggers or a new production incident changes
the evidence.
