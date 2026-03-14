# Phase 15: Capability Seams - Research

**Researched:** 2026-03-14

## Summary

Phase 15 should stay narrowly focused on turning the existing read, topic, and search internals in
`src/mcp_telegram/tools.py` into explicit capability-oriented seams without changing the public tool
surface yet. The brownfield code already contains reusable pieces for dialog resolution, topic
catalog loading, topic-scoped fetch/recovery, pagination, and transcript formatting, but the
public tools still own too much orchestration locally.

The main planning job is therefore not a speculative redesign. It is to expose bounded internal
capabilities so `ListMessages`, `SearchMessages`, and `ListTopics` become thin adapters over shared
read/search/topic paths. Phase 15 should stop before Phase 16's unified navigation contract and
Phase 17's direct workflow reshaping, except where those future phases influence where the seams
need to be cut.

## Research Question

What does the planner need in order to create executable Phase 15 plans that make capability seams
visible across read, search, and topic behavior without drifting into later contract changes?

## Brownfield Findings

### 1. The repo already has reusable internals, but the tools still orchestrate by tool name

`ListTopics` is already relatively shallow, but `ListMessages` and `SearchMessages` remain
tool-shaped orchestrators:

- `ListMessages` owns dialog resolution, cursor handling, sender resolution, topic resolution,
  unread behavior, fetch-strategy selection, message enrichment, formatting, and cursor emission
- `SearchMessages` separately owns dialog resolution, search fetch, context expansion, reaction
  enrichment, hit marking, and offset emission
- `ListTopics` owns its own dialog-resolution and topic-catalog path instead of calling one higher
  level topic capability

That makes shared read/search/topic changes expensive because maintainers still reason through
three public-tool bodies instead of a smaller set of internal execution paths.

### 2. Several real capability primitives already exist in `tools.py`

The strongest reusable brownfield anchors are already present:

- dialog resolution through `_resolve_dialog()`
- topic catalog normalization/loading through `_normalize_topic_metadata()`,
  `_fetch_all_forum_topics()`, `_refresh_topic_by_id()`, and `_load_dialog_topics()`
- topic-scoped history fetch/recovery through `_message_matches_topic()`,
  `_fetch_topic_messages()`, and `_fetch_messages_for_topic()`
- transcript rendering through `format_messages()`
- read pagination encoding/decoding through `encode_cursor()` and `decode_cursor()`
- reusable caches through `ReactionMetadataCache` and `TopicMetadataCache`

Phase 15 should package these into clearer internal seams rather than replace them with a new
framework.

### 3. The biggest remaining duplication is around adapter orchestration and enrichment

The code still duplicates or tool-couples several behaviors that should become capability paths:

- dialog ambiguity and not-found rendering is repeated across `ListTopics`, `ListMessages`, and
  `SearchMessages`
- sender cache warmup is duplicated between `ListMessages` and `SearchMessages`
- reaction-name enrichment is duplicated, and `SearchMessages` currently bypasses
  `ReactionMetadataCache`
- `ListMessages` and `ListTopics` both know too much about topic-catalog semantics and error
  wording
- `SearchMessages` has a search-specific context-window/result-shaping path that is still local to
  the tool body instead of expressed as a search capability feeding shared enrichment/rendering

This is the real CAP-01 gap: the shared behavior exists, but it is not framed as shared internal
capability.

### 4. Phase 15 should expose seams, not settle future public-contract questions

The Phase 13 implementation memo already locked the Medium-path sequence:

1. boundary recovery
2. capability-oriented internals
3. unified navigation contract
4. direct read/search workflow reshaping
5. helper-tool posture decisions

That means Phase 15 should not:

- rename tools
- merge tool roles
- replace `cursor` and `offset` with one public continuation contract
- make forum reads more direct at the public contract level

It should, however, choose internal seam boundaries that make those later phases cheaper.

### 5. The tests already expose stable behavior clusters that can anchor capability seams

The repo has strong brownfield anchors that are better than a generic refactor plan:

- shared dialog-resolution and actionable-recovery clusters across `ListTopics`, `ListMessages`,
  and `SearchMessages`
- topic catalog and topic tombstone/recovery tests around `_fetch_all_forum_topics()`,
  `_refresh_topic_by_id()`, and `_load_dialog_topics()`
- `ListMessages` topic-resolution, topic-failure, thread pagination, and leak-filtering tests
- `SearchMessages` context-window, hit-marking, no-hit, and offset-pagination tests
- rename-resistant primitive anchors in `tests/test_pagination.py`, `tests/test_cache.py`, and
  topic-related fixtures in `tests/conftest.py`

Phase 15 should use those clusters to prove the new seams are capability-shaped rather than only
reorganized helpers.

## Locked Planning Constraints

The Phase 15 plans should treat these as fixed inputs:

- the target requirement is `CAP-01`, not public-contract cleanup in `NAV-01`, `NAV-02`,
  `FLOW-01`, or `FLOW-02`
- public tool classes and reflected discovery remain stable during this phase
- read-only Telegram scope, privacy-safe telemetry, explicit ambiguity handling, and topic/entity
  fidelity remain preserved invariants
- avoid speculative architecture; prefer bounded extractions from `tools.py` over a broad new
  service framework
- runtime-affecting work must still remain compatible with later reflected-schema and restarted
  runtime checks even if Phase 15 itself is mostly internal

## Recommended Capability Boundaries

The planner should bias toward four internal seams.

### 1. Dialog target resolution seam

One reusable path should:

- resolve the dialog selector
- produce the optional `[resolved: ...]` prefix
- return stable actionable not-found and ambiguous responses for dialog-target tools

This is already latent in `_resolve_dialog()` plus duplicated formatting branches.

### 2. Forum topic capability seam

One reusable path should:

- load or refresh the topic catalog
- resolve requested topics including deleted and inaccessible cases
- expose stable topic metadata needed by both `ListTopics` and `ListMessages`
- keep topic recovery semantics explicit instead of hiding them in tool-local conditionals

This is the most concrete shared seam between topic listing and topic-scoped reading.

### 3. Message enrichment/rendering seam

One reusable path should:

- warm sender cache entries
- fetch reply-map data
- hydrate reaction names consistently, with cache usage where appropriate
- optionally label cross-topic forum messages
- hand off to `format_messages()`

This is the most plausible shared seam between history reads and search results without forcing
their fetch modes to merge yet.

### 4. Fetch-mode seams under shared adapters

Phase 15 should keep two execution modes distinct:

- a history-page capability for `ListMessages`
- a search-with-context capability for `SearchMessages`

Those modes should feed shared target-resolution, topic-resolution where relevant, enrichment, and
rendering paths instead of remaining end-to-end tool-local pipelines.

## Recommended Plan Split

Phase 15 is best planned as three executable plans across three waves.

### Plan 01: Capability Contract Anchors

Purpose:

- add or reshape tests so the intended internal seams are visible independently of public tool
  names
- lock the brownfield behavior clusters that future extraction must preserve
- define the thin-adapter target before moving orchestration code

Primary artifacts:

- `tests/test_tools.py`
- optional focused internal test module if it keeps the seam contract clearer than more assertions
  in `tests/test_tools.py`

Why first:

- the repo already has rich behavior coverage; Phase 15 needs explicit seam coverage before
  extraction work starts
- later refactors will be safer if the capability contracts are test-anchored first

### Plan 02: Extract Shared Read/Topic Capabilities

Purpose:

- move dialog-target resolution, topic-catalog/topic-resolution behavior, and shared message
  enrichment into explicit internal helpers or modules
- make `ListTopics` and `ListMessages` thin adapters over those capabilities
- preserve all existing topic recovery and pagination behavior

Primary artifacts:

- `src/mcp_telegram/tools.py`
- one or more new internal modules only if extraction meaningfully improves seam visibility
- related tests updated to target the new capability boundaries

Why second:

- `ListTopics` and `ListMessages` share the strongest topic-oriented seam today
- extracting this boundary first provides the clearest Phase 15 win without crossing into unified
  navigation

### Plan 03: Migrate Search to Shared Capability Paths and Prove Adapter Thinness

Purpose:

- refactor `SearchMessages` to reuse the new shared target-resolution and message-enrichment paths
- eliminate the remaining search-specific duplication that blocks shared behavior changes
- add an explicit proof that public tools now delegate to capability-oriented internals rather than
  re-owning the orchestration locally

Primary artifacts:

- `src/mcp_telegram/tools.py`
- any new internal capability module introduced in Plan 02
- focused tests showing search shares the same internal seams where intended

Why third:

- search is the last major tool-shaped pipeline
- this plan closes `CAP-01` by making read, topic, and search behavior evolvable through shared
  internals rather than per-tool rewrites

## Risks To Plan Around

### Risk 1: The phase becomes a framework rewrite

If the plan introduces a large new service layer, abstract base hierarchy, or speculative
cross-module architecture, it will overshoot the bounded Medium-path posture.

### Risk 2: The phase silently drifts into Phase 16 navigation work

It is valid to place seams where later navigation unification can plug in, but Phase 15 should not
replace `cursor` and `offset` with a new public contract yet.

### Risk 3: Internal extraction weakens explicit topic recovery

The current topic behavior includes deleted-topic, inaccessible-topic, stale-anchor refresh, and
dialog-scan fallback logic. Any seam extraction must preserve that explicit recovery surface.

### Risk 4: Shared enrichment stays half-shared

If `SearchMessages` keeps separate sender/reaction/context shaping while `ListMessages` gets a new
capability path, the phase will not fully satisfy `CAP-01`.

### Risk 5: Tests only prove behavior, not seam visibility

Existing tool-behavior coverage is strong, but Phase 15 also needs either internal tests or
clearer code boundaries that make the capability seams inspectable independently of the public tool
names.

## Validation Architecture

### Test infrastructure

- Primary validation mode: focused `pytest` runs plus the existing full suite
- Brownfield anchors:
  - `tests/test_tools.py`
  - `tests/test_pagination.py`
  - `tests/test_cache.py`
  - `tests/conftest.py`
- Main implementation anchors:
  - `src/mcp_telegram/tools.py`
  - any new internal capability module introduced by the phase

### Required verification themes

The Phase 15 plans should map their tasks to these verification themes:

1. thin-adapter proof for `ListTopics`, `ListMessages`, and `SearchMessages`
2. preserved dialog-resolution and actionable-ambiguity behavior
3. preserved topic catalog, tombstone, inaccessible-topic, and stale-anchor recovery behavior
4. shared enrichment behavior for sender cache warmup, reaction metadata, reply mapping, and topic
   labeling where applicable
5. unchanged public tool exposure and stable test suite behavior after internal extraction

### Expected validation commands

- `uv run pytest tests/test_tools.py -k "list_topics or list_messages or search_messages" -q`
- `uv run pytest tests/test_tools.py -k "topic or cursor or offset or reaction or telemetry" -q`
- `uv run pytest tests/test_pagination.py tests/test_cache.py -q`
- `uv run pytest`

## Phase 15 Is Ready For Planning Now

The phase is planning-ready:

- the requirement boundary is explicit and does not need more product discussion
- the brownfield capability primitives are already visible in the code
- the likely plan split is clear and stays aligned with the Phase 13 sequencing memo
- the tests already provide concrete anchors for seam extraction without reopening the public
  contract
