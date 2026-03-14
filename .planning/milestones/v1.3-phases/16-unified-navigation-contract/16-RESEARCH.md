# Phase 16: Unified Navigation Contract - Research

**Researched:** 2026-03-14

## Summary

Phase 16 should replace the current split read/search continuation vocabulary with one shared
navigation contract at the public tool surface, but it should do so by building on the Phase 15
capability seams rather than by reshaping workflows or merging tool roles early.

The brownfield repo now has a clean internal split between history reads and searches in
`src/mcp_telegram/capabilities.py`, but those paths still expose different continuation concepts
to callers:

- `ListMessages` uses `cursor`, `next_cursor`, and `from_beginning`
- `SearchMessages` uses `offset` and `next_offset`
- both tools render continuation cues as prose footers instead of one shared contract term

The planning task is therefore not to invent a larger redesign. It is to define one coherent
navigation vocabulary that both public tools can expose while preserving:

- topic-scoped read fidelity
- explicit dialog/topic/sender ambiguity handling
- readable transcript output
- hit-local search context
- reflection-safe schema changes and restarted-runtime freshness checks

## Research Question

What does the planner need to know in order to create executable Phase 16 plans that unify
read/search continuation semantics without weakening current topic, ambiguity, or output fidelity?

## Brownfield Findings

### 1. The split contract is public, reflected, and test-anchored today

The current navigation split is not just internal implementation detail.

- `src/mcp_telegram/tools.py` exposes `ListMessages.cursor`, `ListMessages.from_beginning`, and
  `SearchMessages.offset` through reflected `ToolArgs` schemas
- `list_messages()` appends `next_cursor: ...` while `search_messages()` appends `next_offset: ...`
- `src/mcp_telegram/server.py` reflects tool schemas directly from those classes at process start
- `tests/test_tools.py` already asserts the current schema names and output footer terms

That means Phase 16 is a real public-contract change. It must be planned as schema work,
contract-test work, and runtime-freshness work, not as a formatter-only cleanup.

### 2. Phase 15 created the right internal seam for a unified contract

The best place to unify navigation now is the capability boundary introduced in Phase 15.

- `HistoryReadExecution` currently carries `next_cursor`
- `SearchExecution` currently carries `next_offset`
- `execute_history_read_capability()` already centralizes read-side cursor decoding, direction
  handling, and topic-scoped pagination
- `execute_search_messages_capability()` already centralizes search-side offset pagination and
  hit/context shaping

Phase 16 should converge those capability outputs toward one shared navigation result shape before
or while the tool adapters change. That keeps the adapters thin and avoids duplicating navigation
translation logic in `tools.py`.

### 3. History reads have one extra constraint: direction is part of the contract today

`ListMessages` pagination is not only about continuation tokens.

- `cursor` is an opaque token validated against `dialog_id` in `src/mcp_telegram/pagination.py`
- `from_beginning=True` switches history iteration from newest-first/backward paging to
  oldest-first/forward paging
- the capability translates that into `max_id` versus `min_id` and `reverse=False` versus
  `reverse=True`

Phase 16 therefore needs one shared vocabulary that covers both:

- continuation from a previous page
- first-page starting posture for history reads

The contract should not leak Telethon terms like `min_id`, `max_id`, `reverse`, or `add_offset`,
but it does need one coherent way to express "start from newest", "start from oldest", and
"continue from prior page" without keeping `from_beginning` as a separate concept.

### 4. Search pagination has weaker scoping today than history cursors

The current search continuation is a plain integer offset.

- it does not encode dialog identity
- it does not encode the query
- it does not protect callers from cross-dialog or cross-query reuse the way history cursors do

That makes Phase 16 an opportunity to harden search continuation, not just rename it.

The planner should bias toward a shared opaque navigation token that can carry enough context to
reject mismatched reuse safely. At minimum, the search continuation path should preserve the same
anti-mismatch posture that read cursors already have for dialog scope.

### 5. Topic fidelity is the main regression risk on the read side

Phase 16 must preserve the topic behavior that is already anchored in code and tests.

Key invariants:

- topic resolution stays dialog-scoped
- ambiguous topics remain explicit and keep enriched candidate metadata
- deleted topics remain tombstones with explicit recovery text
- inaccessible topics still surface actionable recovery
- `General` remains canonicalized and can still read without `reply_to`
- topic pagination must keep stripping adjacent-topic leaks
- continuation for topic reads must still advance from the last emitted topic message, not the
  last raw fetched message

This means the unified navigation contract should wrap existing topic-aware pagination behavior,
not replace it with a generic paging layer that ignores thread semantics.

### 6. Search fidelity is the main regression risk on the search side

Phase 16 should not weaken what makes `SearchMessages` usable today.

Preserved behavior:

- hit-local context stays attached to hits
- hit marking remains readable
- dialog resolution and ambiguity handling remain explicit
- search continuation still pages through Telegram search results correctly

The shared navigation model should therefore change the caller-facing vocabulary while leaving the
underlying search fetch mode as a distinct capability path. Phase 16 is navigation unification, not
history/search execution merging.

### 7. Formatter and footer wording are part of the migration surface

The current tool outputs mix transcript text with continuation footer text:

- `next_cursor: ...`
- `next_offset: ...`
- optional `[topic: ...]` prefixes
- `[resolved: ...]` prefixes
- `Action:` failure bodies

Phase 16 should leave transcript readability intact, but it should make the continuation footer
shared and stable across both tools. The planner should treat output wording changes as contract
changes that need dedicated tests.

### 8. Telemetry semantics need bounded attention during the migration

The repo records `has_cursor` in telemetry events today.

- `ListMessages` sets it from `args.cursor is not None`
- `SearchMessages` currently hardcodes `has_cursor=False`

Phase 16 does not need a broad telemetry redesign, but the planner should account for the fact that
the old field name becomes semantically stale if both tools adopt one shared navigation token.
Either:

- keep the internal telemetry column temporarily and document the mismatch as bounded debt, or
- rename/broaden the in-process semantics in the same phase without widening privacy scope

The important constraint is unchanged: no message content or identifying payloads may be logged.

## Locked Planning Constraints

The Phase 16 plans should treat these as fixed inputs:

- requirements are `NAV-01` and `NAV-02`
- tool names stay the same; this phase changes navigation vocabulary, not tool inventory
- Phase 16 should not do Phase 17's helper-first workflow simplification work
- explicit ambiguity handling, topic fidelity, readable transcript output, privacy-safe telemetry,
  and read-only Telegram scope remain preserved invariants
- reflected local schemas plus restarted-runtime freshness checks are mandatory acceptance gates for
  any public schema change
- trust current code/tests/runtime over older planning notes where they disagree

## Recommended Contract Direction

The planner should bias toward the narrowest shared navigation contract that meets the roadmap goal.

Recommended shape:

- one shared caller-facing continuation input name for both `ListMessages` and `SearchMessages`
- one shared caller-facing continuation output/footer name for both tools
- one shared first-page navigation vocabulary for `ListMessages` that absorbs today's
  `from_beginning` concept into the same family instead of leaving it as a separate knob
- opaque token payloads rather than exposed raw offsets or Telethon paging primitives

Implementation notes the planner should account for:

- read tokens need to preserve current dialog-scoping guarantees
- search tokens should add mismatch protection for dialog/query reuse
- token decoding/validation should live below the tool adapters
- error text should stay action-oriented when tokens are malformed or mismatched

The planner does not need to lock the exact field names in research, but it should keep the final
implementation bounded to one coherent public vocabulary rather than multiple aliases or a
compatibility window by default.

## Recommended Plan Split

Phase 16 is best planned as three executable plans across three waves.

### Plan 01: Shared Navigation Primitive and Contract Anchors

Purpose:

- define the shared internal navigation primitive that can carry both history and search state
- add contract tests that pin the intended unified vocabulary and mismatch protections
- prepare the capability layer so tool adapters do not own token translation

Primary artifacts:

- `src/mcp_telegram/pagination.py` or a dedicated navigation module
- `src/mcp_telegram/capabilities.py`
- `tests/test_capabilities.py`
- `tests/test_tools.py`

Why first:

- the repo already has strong behavior coverage, but the unified contract needs explicit test
  anchors before public-tool migration begins
- search hardening and read-direction unification are easier to reason about once one shared
  primitive exists

### Plan 02: Migrate `ListMessages` to the Unified Vocabulary

Purpose:

- replace `cursor` and `from_beginning` at the public `ListMessages` surface with the new shared
  navigation vocabulary
- preserve topic-scoped pagination, ambiguity handling, sender filtering, and readable transcript
  output
- prove local reflection now exposes the intended `ListMessages` contract

Primary artifacts:

- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/capabilities.py`
- `tests/test_tools.py`
- `tests/test_capabilities.py`
- `tests/test_server.py` or `cli.py` reflection checks if needed

Why second:

- `ListMessages` carries the harder navigation problem because it currently combines continuation
  with direction selection
- proving the read side first reduces ambiguity before the search side adopts the same contract

### Plan 03: Migrate `SearchMessages`, Remove the Split Vocabulary, and Prove Runtime Freshness

Purpose:

- replace `offset` / `next_offset` with the same navigation vocabulary now used by
  `ListMessages`
- preserve hit-local context and search paging fidelity
- run local reflection plus restarted-runtime verification so the changed public schema is proven
  live

Primary artifacts:

- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/capabilities.py`
- `tests/test_tools.py`
- `tests/test_server.py`
- `tests/test_analytics.py` if telemetry semantics move

Why third:

- it closes `NAV-01` by removing the last public split term
- it is the right place to do the reflection/restart proof because the full unified contract is
  visible only after both tools land

## Risks To Plan Around

### Risk 1: The phase only renames footer text

If the work changes `next_cursor` / `next_offset` labels in tool output but leaves reflected input
schemas split, the phase will not actually satisfy `NAV-01`.

### Risk 2: The phase leaks transport-specific paging concepts

If the new contract exposes `max_id`, `min_id`, `reverse`, or raw `add_offset`, the public surface
gets more coherent in naming but worse in abstraction quality.

### Risk 3: Topic pagination regresses under a generic token model

If navigation state is advanced from raw fetched batches instead of emitted topic messages, topic
reads will reintroduce adjacent-topic leakage or broken continuation.

### Risk 4: Search hardening is skipped

If the new search continuation remains effectively an unscoped integer wrapped in a new name, the
phase misses a clean chance to add mismatch protection comparable to history cursors.

### Risk 5: Compatibility pressure bloats the phase

If the implementation keeps both old and new navigation vocabularies by default, the bounded Medium
migration posture will drift toward shim-heavy rollout work that belongs only if a concrete client
constraint appears.

### Risk 6: Runtime freshness is treated as optional

Because tool reflection is process-start-bound, Phase 16 can look complete in repo tests while a
long-lived runtime still serves the old schema. Restart verification is mandatory.

## Validation Architecture

### Test infrastructure

- Primary validation mode: focused `pytest` runs plus reflection checks and the existing full suite
- Brownfield anchors:
  - `tests/test_tools.py`
  - `tests/test_capabilities.py`
  - `tests/test_server.py`
  - `tests/test_pagination.py`
  - `tests/test_analytics.py`
- Main implementation anchors:
  - `src/mcp_telegram/tools.py`
  - `src/mcp_telegram/capabilities.py`
  - `src/mcp_telegram/pagination.py`
  - `src/mcp_telegram/server.py`
  - `cli.py`

### Required verification themes

The Phase 16 plans should map their tasks to these verification themes:

1. both tools expose one shared continuation vocabulary in reflected schemas
2. `ListMessages` preserves newest-first, oldest-first, and continuation behavior under the new
   model
3. topic-scoped reads preserve leak filtering, tombstones, inaccessible-topic recovery, and
   ambiguity handling
4. `SearchMessages` preserves hit-local context and correct pagination under the new model
5. malformed or mismatched navigation tokens return action-oriented failures
6. local reflection and restarted runtime expose the same intended contract after the migration
7. telemetry remains privacy-safe and does not widen logged payloads

### Expected validation commands

- `uv run pytest tests/test_capabilities.py -k "history or search or cursor or offset or navigation" -q`
- `uv run pytest tests/test_tools.py -k "list_messages or search_messages or cursor or offset or from_beginning or topic" -q`
- `uv run pytest tests/test_server.py -q`
- `uv run pytest tests/test_pagination.py tests/test_analytics.py -q`
- `uv run cli.py list-tools`
- `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram`
- `docker exec mcp-telegram mcp-telegram run --help`

## Phase 16 Is Ready For Planning Now

The phase is planning-ready:

- the requirement boundary is explicit in `ROADMAP.md` and `REQUIREMENTS.md`
- Phase 15 already exposed the capability seam where navigation can be unified cleanly
- current code/tests identify the exact contract terms and regression risks
- the likely plan split is clear and stays bounded to navigation unification rather than broader
  workflow redesign
- the required runtime-freshness discipline is already known from the repo's deployment posture
