# Telegram fact acquisition telemetry

The paired Entity Profile operation emits bounded `entity_profile.pair`
observations through the existing runtime observation sink. Events are
aggregated over the configured runtime observation window and contain no
account, entity, session, generation, selector, content, exception text, or
stable fingerprint.

Each completed generation produces at most one summary. The refresh state keeps
the mode, eligibility, section outcomes, actual dispatch and retry counts,
readiness timestamp, completeness bit, and a summary watermark. A generation
without both durable section outcomes, with a mode mismatch, or with lost
measurement is excluded from the completed-pair denominator. Each emitted
summary has an explicit `event_count` denominator and these dimensions:

- `mode`: `enabled` or `disabled` (the feature switch state);
- `eligible_pair`: both `full_profile` and `personal_channel` required work at
  refresh start;
- `actual_attempts`: GetFullUser dispatches attributed to profile events;
- `retries`: additional attempts explicitly attributed by the producer;
- `full_profile_outcome` and `personal_channel_outcome`: independent
  `usable`, `partial`, `absent`, or `unavailable` outcomes;
- `pair_ready_count`: durable pair outcomes committed successfully;
- `local_satisfaction` and `prevented_request`: valid local evidence served a
  real acquisition need;
- `reuse_rejection_reason`: bounded receipt rejection category, including an
  authorization-scope change during acquisition;
- `reused_age_ms`: maximum age of reused observations in the summary;
- `pair_readiness_latency_ms`: average original observation-to-commit latency;
- `stale_writer_rejected`: a response lost the repository fence.
- `measurement_complete`: the generation had complete pair attribution when
  the summary was taken.

The pair-mode value is captured per generation. A generation started with the
switch disabled remains a disabled generation after restart or a configuration
flip; a later generation can capture the newly enabled value. The additive
ownership and measurement columns are introduced by schema migration v62. The
default is enabled on v62-aware releases. Optional observe-only measurement
supports aggregate impact claims and is not a prerequisite for deterministic
one-RPC correctness or enablement. The supported rollback is enabled to
disabled to enabled on a v62-aware binary; arbitrary older binaries are
unsupported.

`telegram.rpc_admission` remains the authoritative transport admission stream.
Profile `actual_attempts` is intentionally independent and is not added to
admission counters. Rows inserted or projections built are not prevented
requests. A prevented request requires a valid, applicable receipt that
satisfied a real acquisition need.

When the asynchronous sink drops or cannot write events, the daemon preserves
the existing loss markers in `daemon_state` and also appends one
`runtime.telemetry_loss` observation with only aggregate drop and writer
failure counts. Loss never changes profile persistence, reuse decisions, or
RPC admission.

See the [fact acquisition decision](plans/telegram-fact-acquisition-expert-decision.md)
for deterministic acceptance criteria and the optional observe-only evaluation
windows for aggregate impact claims.

## Bounded RPC method and causal observations

`telegram.rpc_request` adds `method_summary`, `causal_sample` and `coverage`
outcomes to the existing sink. It does not admit work, change retries, or add
Telegram acquisition to reads. `telegram.rpc_admission` remains the admission
total; method, demand and profile counts must not be added to that total.

A method summary groups the installed canonical TL constructor by the existing
source, service, demand and acquisition classifications. Its independent counters
are `write_claim`, `write_return`, `known_response`, `unknown_outcome`,
`vendor_cached`, `rejected` and `retry`. `actual_attempts` aliases `write_claim`
with `dispatch_semantics=write_claim_v1`. A claim consumes physical admission
before encryption and the writer callback. A write return establishes only local
writer acceptance. A known response establishes an actual provider result or RPC
error, not durable canonical progress; synthetic ACK/StateInfo success is excluded.
A claim followed by encryption/write failure therefore has no write return, and
an accepted local write followed by response loss has no known response. Cached
Flood errors, rejection and retry notices are not additional physical attempts.
The phases of one owner can occur in different windows; compare claims with
admission claims, not with a same-window response or write-return total.

Legacy `summary` rows for GetFullChannel retain their original semantics. Older
sender-enqueue-era counts cannot be upgraded to physical-write evidence. Current
method summaries retain `request_class=get_full_channel` for this method; the
operator summary accepts both generations and does not double count them.
Overflow can omit attributed GetFullChannel detail. GetDifference and
GetChannelDifference remain distinct methods even under the same source.

The fixed limit per configured aggregation window is 16 method-dimension buckets
plus one overflow bucket, two sampled roots with at most twelve event rows each,
and one coverage row: at most 42 new rows. Phase counters share each method bucket;
they do not multiply its cardinality. Overflow keeps phase totals but loses method
attribution. Sampling chooses the first roots and is biased; sampled retries are
not population rates. `unsampled_events`, `truncated_events`, `lost_rows` and
`unaccepted_summary_rows` report limitations. All causal samples are explicitly
partial, including roots that span windows. Absence of a sample is not absence of
activity. Missing retained windows and historical journal gaps remain unknown.

Sample `operation_id` and `target_ref` are keyed opaque hashes of the existing
account-scoped WorkRevision and owner-local WorkKey. A random process-only secret
is never persisted. Equal exact revisions can be correlated through resumptions
within one `runtime_instance_id`; changed revisions retain the target reference
but change operation identity. Transient roots use their existing evidence
object's private random identity and have no invented target. Restart changes
these links, and an owner target is not a universal peer identity across consumers.
Raw targets, revisions, IDs, request arguments, result/exception text, credentials
and message contents are never persisted by this observer.

`attempt_ref` is the gate's random owner identifier, not a Telegram message or
session identifier. Physical claim and write-return samples have captured
`attempt_ordinal`, `replay_ordinal` and admission sequence. Response samples link
to their owner but deliberately do not identify which replay received the response.
`root_attempts`, budget remaining and deadline remaining are event-time snapshots;
a later response can observe a larger root count than its original claim.
`consumer_trigger` is the existing registry classification; `trigger` identifies
a known durable selection, otherwise remains unknown. The original business
invalidation cause is not inferred from either field. Retry reasons are bounded
existing classifications; replay identity alone does not establish its cause.

Observation wall time is captured when the event occurs, independently of delayed
persistence. Method bounds span their first event through flush; use complete
retained coverage windows for rates, not an active method's shorter event span.
Rejected summary submissions are merged into bounded counters with their original
start, so a later summary can cross several coverage windows. Do not treat it as
one normal-window count. Samples are dropped with loss evidence, not accumulated
unboundedly. Delayed flushes make one longer window, never catch-up windows. Sink
loss evidence remains authoritative when even a coverage row cannot be written.

Payloads retain the existing 1024-byte cap. Optional sample dimensions are omitted
in fixed order with `payload_truncated` if necessary. At the default 300-second
window, the added maximum is 12,096 rows/day and 11.8125 MiB/day of payload before
SQLite/index overhead. The existing shared row cap and TTL apply to all telemetry;
TTL is an upper bound, not a guaranteed retained interval. For example, 300,000
rows cover at most 24.8 days at this added maximum before any preexisting traffic.
Inspect effective configuration and the actual retained interval when reporting
coverage. No larger retention cap or guaranteed response time is implied.
