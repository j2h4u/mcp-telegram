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
