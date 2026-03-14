# Phase 17: Direct Read/Search Workflows - Research

**Researched:** 2026-03-14

## Summary

Phase 17 should reduce helper-first choreography by making `ListMessages` and `SearchMessages`
better at operating on already-known targets directly, while preserving the ambiguity, topic, and
hit-local guarantees that the repo has already hardened in Phases 9, 15, and 16.

The brownfield code already gives both tools one shared navigation model, but the common workflow
still has avoidable setup cost:

- both primary tools always start with dialog resolution, even when the caller already knows the
  exact numeric dialog id;
- topic reads always load and resolve the full topic catalog when `topic=` is used, even when the
  caller already knows the exact topic id or cached metadata;
- `SearchMessages` still rebuilds hit windows and hit markers in the tool adapter, leaving part of
  the user-facing workflow logic outside the capability layer.

Phase 17 therefore should not reopen navigation unification or helper-surface classification. It
should add the narrowest exact-target fast paths and search/read workflow shaping needed to satisfy
`FLOW-01` and `FLOW-02`.

## Research Question

What does the planner need to know in order to create executable Phase 17 plans that make common
read and search jobs more direct without weakening topic fidelity, explicit ambiguity handling, or
hit-local search context?

## Brownfield Findings

### 1. The current public tools are primary, but they still pay helper-style setup costs

The code already routes both `ListMessages` and `SearchMessages` through capability entrypoints,
which is the right Medium-path shape. However, each call still begins with dialog resolution via
cache lookup and live dialog warmup rather than supporting a known-id fast path.

That means the main remaining burden is no longer split pagination vocabulary. The burden is
workflow setup and target acquisition.

### 2. There is no exact-target fast path for reads today

`ListMessages` always resolves the dialog first and, when `topic=` is present, always loads the
dialog topic catalog before it can read one thread.

This is true even when the caller already has exact identifiers from a previous `ListDialogs` or
`ListTopics` result. The bounded opportunity is to let the read flow accept known dialog/topic
identifiers so it can bypass fuzzy resolution and full catalog loading when the target is already
known.

### 3. The safest direct-read seam is id/metadata-based, not title-based

The repo's strongest read-side invariants live below the resolver:

- topic leak filtering
- stale-anchor refresh via topic-by-id retry
- tombstone handling for deleted topics
- inaccessible-topic recovery and fallback scan behavior
- topic-scoped navigation based on emitted thread messages

Those behaviors already sit behind `execute_history_read_capability()`,
`fetch_messages_for_topic()`, `fetch_topic_messages()`, `TopicMetadataCache.get_topic()`, and
`refresh_topic_by_id()`.

That means the narrowest safe direct-read move is not "be more permissive with fuzzy topic names."
It is "support exact dialog/topic selectors that can enter the existing capability flow later,
after ambiguity has already been resolved elsewhere."

### 4. Name-based ambiguity handling remains non-negotiable

The current resolver behavior is a preserved strength:

- fuzzy dialog and topic names produce explicit candidate lists;
- topic ambiguity includes cached metadata like deleted or previously inaccessible status;
- the system does not silently auto-pick approximate matches.

Phase 17 must preserve that contract. Direct fast paths should be exact-target lanes, not a reason
to weaken ambiguity behavior for name-based requests.

### 5. Topic fidelity remains the main regression risk on the read side

The planner must treat the following as locked invariants:

- topic resolution stays dialog-scoped;
- `General` stays canonicalized and avoids `reply_to` fetch mode;
- deleted topics remain tombstones instead of becoming empty reads;
- inaccessible topics still surface explicit action-oriented recovery;
- stale `TOPIC_ID_INVALID` anchors still get one bounded by-id refresh and fallback handling;
- topic pages keep stripping adjacent-topic and general-message leakage;
- topic navigation remains anchored to the last emitted topic message, not the last raw fetched
  message.

Any direct-read work that bypasses the catalog must still preserve those exact behaviors.

### 6. Search still has user-facing workflow logic outside the capability seam

`SearchMessages` now shares the Phase 16 navigation contract, but the tool adapter still does a
large amount of search-specific workflow assembly:

- grouping hit-local before/after context
- sorting each hit window
- calling `format_messages()`
- injecting the `[HIT]` marker
- wrapping windows with `--- hit N/M ---`
- choosing the empty state
- appending `next_navigation`

That adapter-side choreography is not just an internal cleanliness issue. It leaves part of the
search workflow shape outside the capability layer that Phase 15 created for this exact purpose.

### 7. Search guarantees are already strong and should be preserved as-is

Phase 17 does not need to reinvent search behavior. It needs to preserve what already works:

- search stays scoped to one resolved dialog;
- navigation tokens remain bound to both dialog and query;
- hit-local context remains bounded to neighboring messages in the same dialog;
- reaction-name enrichment stays bounded to hit messages;
- readable search output keeps explicit hit marking and grouped local context.

The phase should therefore reduce search burden by moving workflow assembly into the right seam and
supporting exact-target entry where appropriate, not by broadening search scope or changing the
context model.

### 8. The best bounded public-surface move is exact selectors on primary tools

The most plausible user-facing way to reduce helper-first choreography in Medium is:

- keep `ListMessages` and `SearchMessages` as the primary read/search surfaces;
- add exact-target selector support for callers that already know ids from earlier steps;
- preserve existing name-based arguments and recovery behavior for exploratory flows;
- avoid making `ListDialogs` or `ListTopics` disappear before Phase 18 classifies surface posture.

This satisfies the roadmap intent: helper tools remain available, but exact known-target work no
longer has to repeat fuzzy discovery machinery by default.

### 9. Phase 17 should not reopen Phase 18

The planner should explicitly stay out of:

- primary vs secondary helper-surface classification as the phase's main deliverable;
- privacy-audit expansion beyond bounded regression coverage;
- runtime-proof framing as a phase goal in its own right.

Phase 17 can still include local reflection checks, contract tests, and repo-required runtime
discipline where schema changes land, but the phase goal is workflow shape, not final rollout
proof or helper demotion policy.

## Locked Planning Constraints

The Phase 17 plans should treat these as fixed inputs:

- requirements are `FLOW-01` and `FLOW-02`
- Phase 16's shared `navigation` / `next_navigation` vocabulary is already chosen and should not be
  redesigned again
- `ListMessages` and `SearchMessages` remain the primary implementation focus
- `ListDialogs` and `ListTopics` stay available; this phase reduces default dependence on them but
  does not settle their final posture
- explicit ambiguity handling remains required for name-based inputs
- topic fidelity, deleted-topic tombstones, inaccessible-topic recovery, readable transcript
  formatting, and hit-local search context remain preserved invariants
- privacy-safe telemetry remains mandatory and must not widen into content logging
- trust current code/tests/runtime over older planning notes where they disagree

## Recommended Contract Direction

The planner should bias toward one bounded contract direction:

- support exact known dialog ids through the existing dialog contract or an equivalent bounded
  exact-target lane, and for `ListMessages`, add the topic-id or cached-topic path needed for
  direct forum reads
- keep existing name-based `dialog` and `topic` entry paths for exploratory or ambiguous jobs
- route both exact-target and name-based paths through the same capability-layer fetch/recovery
  behavior once a target is known
- move search hit-window assembly closer to the capability/formatter seam so the tool adapter stops
  owning workflow-specific reconstruction

The exact public field names do not need to be frozen in research, but the direction should be:

- exact-target handling is an opt-in fast path, not a replacement for natural-name use
- mutual exclusion and validation rules must be explicit so callers cannot send conflicting exact
  and fuzzy selectors silently
- `SearchMessages` should prefer using the current `dialog` contract with smarter exact-id handling
  below the adapter rather than adding speculative new public fields unless implementation proves
  that the existing surface cannot express the needed direct path cleanly
- direct topic reads should reuse cache-backed metadata or topic-id refresh paths instead of
  requiring full-catalog resolution every time

## Recommended Plan Split

Phase 17 is best planned as three executable plans across three waves.

### Plan 01: Exact-Target Capability Lanes and Contract Anchors

Purpose:

- introduce exact-target capability inputs for known dialog ids and topic ids/metadata
- keep name-based resolution and ambiguity behavior intact
- add tests that pin the distinction between exact-target fast paths and fuzzy helper flows

Primary artifacts:

- `src/mcp_telegram/capabilities.py`
- `src/mcp_telegram/cache.py`
- `tests/test_capabilities.py`
- `tests/test_tools.py`

Why first:

- it creates the internal seam both primary tools can rely on
- it lets later public-schema work stay thin instead of duplicating exact-target logic in
  `tools.py`

### Plan 02: Reshape `ListMessages` Around Direct Reads Without Losing Topic Fidelity

Purpose:

- expose the exact-target read path at the public `ListMessages` surface
- preserve topic ambiguity, tombstones, inaccessible-topic recovery, `General` behavior, unread
  behavior, and topic-scoped navigation
- prove that known-target forum reads no longer require the default `ListTopics` helper path

Primary artifacts:

- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/capabilities.py`
- `tests/test_tools.py`
- `tests/test_server.py`

Why second:

- read-side topic fidelity is the hardest part of the phase
- landing it first reduces ambiguity before applying the same exact-target posture to search

### Plan 03: Reshape `SearchMessages` Around Direct Search Workflows

Purpose:

- reduce `SearchMessages` workflow burden without speculative schema expansion
- move hit-window assembly into capability/formatter support instead of manual adapter logic
- preserve bounded hit-local context, query/dialog-scoped navigation, readable hit markers, and
  privacy-safe telemetry semantics

Primary artifacts:

- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/capabilities.py`
- `src/mcp_telegram/formatter.py`
- `tests/test_tools.py`
- `tests/test_capabilities.py`
- `tests/test_server.py`
- `tests/test_analytics.py`

Why third:

- it closes `FLOW-02` after the shared exact-target seam exists
- it keeps search shaping bounded to the primary tool rather than reopening surface posture or
  rollout-proof scope early

## Risks To Plan Around

### Risk 1: Exact-target support silently weakens ambiguity behavior

If the implementation starts auto-resolving fuzzy names more aggressively instead of adding exact
selector lanes, the phase will reduce helper steps by reducing safety, which is out of bounds.

### Risk 2: Direct topic reads bypass the tombstone/recovery logic

If `topic_id` fast paths skip cache-backed tombstones, by-id refresh, or fallback scan behavior,
forum fidelity will regress even if the happy path gets faster.

### Risk 3: Search shaping stays split between capability and adapter

If Phase 17 keeps `SearchMessages` on the same thin-looking public contract but leaves hit-window
assembly in `tools.py`, the tool remains more choreography-heavy than the rest of the Phase 15/16
design without creating real workflow improvement.

### Risk 4: The phase drifts into helper-surface posture decisions

If plans start deciding final primary/secondary/helper classifications or removal policy for
`ListDialogs` and `ListTopics`, Phase 17 will overlap Phase 18 instead of staying bounded.

### Risk 5: Schema changes are planned without reflection coverage

If new direct-target fields are added without `tests/test_server.py` and local `cli.py list-tools`
checks, the phase can look complete in behavior tests while reflected tool schemas drift.

### Risk 6: Telemetry grows in scope while search/read flows change

If the implementation records new identifying payloads to make the workflow feel easier to debug,
the phase will violate a preserved invariant. Telemetry changes must stay bounded and privacy-safe.

## Validation Architecture

### Test infrastructure

- Primary validation mode: focused `pytest` runs plus local reflection checks and the existing full
  suite
- Brownfield anchors:
  - `tests/test_tools.py`
  - `tests/test_capabilities.py`
  - `tests/test_server.py`
  - `tests/test_analytics.py`
- Main implementation anchors:
  - `src/mcp_telegram/tools.py`
  - `src/mcp_telegram/capabilities.py`
  - `src/mcp_telegram/cache.py`
  - `src/mcp_telegram/formatter.py`
  - `cli.py`

### Required verification themes

The Phase 17 plans should map their tasks to these verification themes:

1. exact known-target dialog reads can bypass discovery-oriented setup without weakening the
   existing name-based path
2. exact known-target forum reads can bypass full topic-helper choreography while preserving
   tombstones, inaccessible-topic recovery, `General`, leak filtering, unread behavior, and
   topic-scoped navigation
3. name-based dialog and topic ambiguity remain explicit and action-oriented
4. exact known-target searches stay dialog-scoped and preserve query-bound navigation tokens
5. hit-local search context, hit markers, and readable grouped output remain intact after the
   search workflow shaping moves deeper into the capability/formatter seam
6. reflected tool schemas expose the intended direct-workflow contract locally
7. telemetry remains privacy-safe and bounded after the workflow changes

### Expected validation commands

- `uv run pytest tests/test_capabilities.py -k "history or search or direct or topic or navigation" -q`
- `uv run pytest tests/test_tools.py -k "list_messages or search_messages or direct or topic or ambiguity or navigation" -q`
- `uv run pytest tests/test_server.py -q`
- `uv run pytest tests/test_analytics.py -q`
- `uv run cli.py list-tools`
- `uv run pytest`

## Phase 17 Is Ready For Planning Now

The phase is planning-ready:

- the roadmap and requirements boundary is explicit in `ROADMAP.md` and `REQUIREMENTS.md`
- Phase 15 and Phase 16 already created the capability seam and shared navigation contract that
  Phase 17 can build on
- current code/tests make the remaining burden concrete: exact-target setup cost on reads and
  searches, plus adapter-owned search workflow assembly
- the likely plan split is clear and stays bounded to direct workflow shaping rather than helper
  posture or rollout-proof work
- the preserved invariants and regression risks are explicit enough to plan executable work now
