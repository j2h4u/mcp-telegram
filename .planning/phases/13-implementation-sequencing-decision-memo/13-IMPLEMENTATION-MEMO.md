# Phase 13 Implementation Memo

## Decision Posture

This memo is the primary handoff artifact for the next coding milestone. The redesign choice is
already made: the next milestone should implement the Phase 12 Medium path, and it should do so as
a migration stage toward a later Maximal redesign rather than as a final steady-state surface.

The planning posture is intentionally bounded:

- backward compatibility is not a default constraint;
- the seven-tool reflected runtime from 2026-03-13 is the real starting point;
- read-only Telegram scope, privacy-safe telemetry, explicit ambiguity handling, stateful runtime
  constraints, and recovery-critical cache behavior remain preserved invariants;
- this memo does not reopen Minimal versus Medium versus Maximal.

## Current Surface and Why It Must Change

The current brownfield baseline is the reflected seven-tool MCP surface:

- `GetMyAccount`
- `GetUsageStats`
- `GetUserInfo`
- `ListDialogs`
- `ListMessages`
- `ListTopics`
- `SearchMessages`

That surface is functional, but it still pushes avoidable burden onto the model in the workflows
that matter most:

- ordinary reads often start with helper-step choreography through `ListDialogs` before the actual
  reading task starts;
- forum reads frequently require `ListTopics` before `ListMessages(topic=...)`, which makes topic
  fidelity visible in the wrong place;
- adjacent navigation tasks teach different continuation concepts through `next_cursor`,
  `next_offset`, and `from_beginning=True`;
- useful recovery guidance exists inside handlers, but escaped failures can still collapse at the
  server boundary into `Tool <name> failed`;
- continuation and recovery cues are still embedded in readable prose that the model has to parse.

The next milestone therefore should not treat the existing public surface as the target to preserve.
It should treat it as a stateful brownfield boundary that needs a cleaner, lower-burden Medium-era
contract.

## Recommended Implementation Path

The recommended path is a Medium migration that keeps the strongest current capabilities while
reducing helper-first workflows and contract fragmentation.

The implementation should keep `ListMessages`, `SearchMessages`, and `GetUserInfo` as the main
user-task surfaces during Medium, while treating `ListDialogs`, `ListTopics`, `GetMyAccount`, and
`GetUsageStats` as secondary or helper/operator surfaces rather than the default first move.

The milestone should pursue five concrete outcomes:

1. clean up the server-boundary failure surface so unexpected exceptions preserve actionable
   recovery direction instead of degrading to generic failure wrappers;
2. introduce capability-oriented internal seams so public adapters are not permanently shaped like
   today’s tool names;
3. converge read and search onto one coherent continuation model rather than continuing to teach
   separate cursor and offset concepts;
4. reshape the read/search/topic workflow so common user jobs become more direct without weakening
   topic fidelity or ambiguity handling;
5. keep privacy-safe telemetry, entity/topic cache usage, and explicit disambiguation visible as
   preserved strengths rather than accidental leftovers.

For milestone planning, the implementation path should be read through three explicit boundaries:

- `must land for Medium`: error-surface cleanup, capability-layer preparation, continuation-model
  unification, workflow reshaping around `ListMessages`, `SearchMessages`, and `ListTopics`, and
  rollout verification tied to reflected runtime behavior;
- `prepare now to make Maximal cheaper`: keep public adapters separate from capability-oriented
  internals, normalize navigation/result framing, and document which surfaces are primary,
  secondary, merge, or future-removal candidates;
- `defer to later Maximal`: full role merging, aggressive surface compression, and larger result
  structure redesigns that would overshoot the Medium migration budget.

## Sequencing

The recommended sequence for the future coding milestone is:

1. Error-surface cleanup.
   Tighten `server.py` boundary behavior first so later schema changes are easier to reason about
   and debug.
2. Capability-layer preparation.
   Add internal execution seams that are capability-oriented instead of permanently mirroring
   `ListDialogs`, `ListMessages`, `ListTopics`, and `SearchMessages`.
3. Continuation-model unification.
   Replace the split `next_cursor` / `next_offset` / `from_beginning=True` burden with one shared
   navigation vocabulary.
4. Read/search/topic workflow reshaping.
   Make the primary user workflows more direct, especially for forum reads that currently rely on
   `ListTopics` before `ListMessages`.
5. Helper-tool demotion decisions.
   After the primary flows are stable, decide which helper/operator tools stay visible as secondary
   surfaces and which move toward future removal.
6. Rollout verification.
   Treat reflection checks, restart validation, and contract tests as acceptance gates rather than
   cleanup work at the end.

This order front-loads the work that reduces migration ambiguity and makes the later Maximal step
cheaper instead of forcing a second deep refactor.

## Validation Checkpoints

The next implementation milestone should treat validation as both repository validation and runtime
validation.

Required checkpoints:

- `ListMessages` contract gate: preserve topic fidelity, readable transcript output, and explicit
  ambiguity handling while reducing continuation burden.
- `SearchMessages` contract gate: preserve hit-local context while moving toward the shared
  navigation model.
- `ListTopics` fidelity gate: preserve topic-state semantics and deleted/inaccessible-topic
  recovery even if common forum reads become more direct.
- `server.py` boundary gate: prove the server boundary no longer discards useful recovery detail
  when unexpected failures escape.
- tool-test gate: keep `tests/test_tools.py` as the brownfield contract anchor for navigation,
  recovery, ambiguity, topic behavior, and helper-step expectations.
- telemetry gate: keep `tests/test_analytics.py` and `tests/privacy_audit.sh` proving that
  analytics remain privacy-safe and do not widen into message-content logging.

Because tool discovery is reflection-based and snapshotted at process start, runtime freshness is a
mandatory acceptance concern whenever public schemas move.

The minimum reflection workflow is:

1. run `uv run cli.py list-tools` before the contract change to capture the local baseline;
2. land the code and test changes;
3. run `uv run cli.py list-tools` again and inspect the reflected schema for the changed tools;
4. restart the long-lived runtime, and rebuild it first when the deployed image packages source at
   build time;
5. run `list-tools` against the restarted process and confirm the live surface matches the local
   expectation.

If a changed runtime still exposes stale schemas after restart, the work is not complete even if
tests pass. This check is especially important around `ListMessages`, `SearchMessages`, `ListTopics`,
and any effort to reduce `Tool <name> failed` boundary collapse.

## Open Questions Before Coding

The next implementation milestone should answer these questions explicitly before code planning
starts:

1. Which current tools remain primary during Medium, which are clearly secondary, and which are
   only temporary compatibility candidates if compatibility is reintroduced later?
2. What is the narrowest shared continuation contract that can unify reads and searches without
   flattening meaningful behavior differences?
3. How direct should common forum reading become in Medium before it starts overlapping the later
   Maximal redesign?
4. How much result-shape cleanup should Medium take on now without turning into a larger structured
   output redesign?
5. Which runtime freshness checks are mandatory after contract changes: local reflection only, or
   local plus restarted-runtime verification every time?

These are decision points for implementation planning, not reasons to reopen the redesign choice.

## Risks and Failure Modes

The main risks for the next milestone are:

- Medium work drifts into a speculative Maximal rewrite and loses the bounded migration posture.
- The implementation preserves helper-first choreography because existing tool names feel safer
  than cleaner workflow contracts.
- Continuation cleanup happens only in prose or output formatting and does not become a stable,
  shared contract.
- `server.py` remains a generic error-collapsing boundary, making later rollout debugging harder.
- Reflection-sensitive runtime behavior is validated only in tests, leaving stale long-lived
  processes serving old schemas after deployment.
- telemetry changes accidentally widen what gets recorded, weakening the privacy-safe telemetry
  guarantee.

## Deferred Work and Future Maximal Preparation

The next milestone should prepare for later Maximal work, but it should not execute that larger
redesign now.

Prepare now:

- keep public adapters separate from any new capability-oriented internals;
- normalize continuation framing in a way that can support a later stronger result structure;
- preserve topic/entity cache access, ambiguity handling, and privacy-safe telemetry as reusable
  building blocks;
- document which current surfaces are primary, secondary, merge candidates, or future-removal
  candidates as the implementation lands.

Defer to later Maximal:

- full merge of read/search/inspect roles into a more compressed public surface;
- aggressive removal of helper/operator tools instead of Medium-era demotion;
- larger result-shape redesigns that go well beyond readable text plus light metadata;
- any compatibility window, alias tool, or dual-surface rollout unless that becomes an explicit
  later requirement.
