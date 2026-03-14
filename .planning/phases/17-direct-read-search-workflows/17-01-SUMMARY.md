---
phase: 17-direct-read-search-workflows
plan: 01
subsystem: api
tags: [telegram, capabilities, forum-topics, navigation, testing]
requires:
  - phase: 16-unified-navigation-contract
    provides: shared history/search navigation tokens and capability seams
provides:
  - exact dialog-id entrypoints for history and search capabilities
  - exact topic-id resolution through cache metadata or one by-id refresh
  - contract tests that pin direct-target scope and preserved ambiguity behavior
affects: [tools, direct-workflows, forum-topics, search-workflows]
tech-stack:
  added: []
  patterns: [opt-in exact-target capability lanes, cache-first topic lookup with bounded refresh]
key-files:
  created: []
  modified:
    - src/mcp_telegram/capabilities.py
    - src/mcp_telegram/cache.py
    - tests/test_capabilities.py
    - tests/test_tools.py
key-decisions:
  - "Exact dialog and topic selectors stay internal and opt-in so name-based ambiguity handling remains unchanged until later Phase 17 plans expose public fields."
  - "Exact topic resolution prefers cached metadata and falls back to one topic-by-id refresh, preserving deleted-topic tombstones and the existing fetch/recovery path once a target is known."
patterns-established:
  - "Direct-target workflows should enter below the tool adapters and reuse the same history/search execution path after target selection."
  - "Topic-by-id recovery should reuse cached metadata, including stale tombstones, instead of reloading full forum catalogs by default."
requirements-completed: [FLOW-01, FLOW-02]
duration: 8 min
completed: 2026-03-14
---

# Phase 17 Plan 01: Exact-target capability foundations for direct dialog and forum reads

**Exact dialog-id and topic-id capability entrypoints with cache-backed topic recovery and contract tests that keep ambiguity and topic-fidelity guarantees intact**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-14T10:04:34Z
- **Completed:** 2026-03-14T10:12:34Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Added opt-in exact dialog targeting for `execute_history_read_capability()` and `execute_search_messages_capability()` so later public adapters can bypass fuzzy discovery when a dialog id is already known.
- Added exact topic resolution through cached metadata or one bounded `refresh_topic_by_id()` call, while preserving deleted-topic tombstones and the existing thread fetch, refresh, and fallback scan behavior after the topic is known.
- Added capability and adapter contract tests proving direct targets bypass discovery-oriented setup while legacy tool adapters still stay on the name-based path and preserve explicit ambiguity handling.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add exact-target capability lanes for known dialog and topic identifiers** - `c9bf733` (`feat`)
2. **Task 2: Add contract tests that pin fast-path scope and preserved forum guarantees** - `412c1c8` (`test`)

## Files Created/Modified
- `src/mcp_telegram/capabilities.py` - Added exact dialog/topic capability inputs, cache-first exact topic resolution, and shared topic-catalog construction for both named and exact paths.
- `src/mcp_telegram/cache.py` - Allowed stale topic lookups during by-id refresh so deleted-topic tombstones keep their cached title and metadata.
- `tests/test_capabilities.py` - Added direct-target coverage for exact dialog ids, exact topic ids, tombstone reuse, bounded refresh, and exact-path read/search execution.
- `tests/test_tools.py` - Added adapter assertions that public tools still do not pass exact-target kwargs before later Phase 17 plans expose new fields.

## Decisions Made
- Exact-target support stays below the public adapters for this plan so later tool-surface changes can stay thin and reuse the same seam instead of re-implementing resolution logic in `tools.py`.
- Exact topic lookup must prefer cache metadata and only perform a single by-id refresh on cache miss, which keeps deleted-topic tombstones and stale-anchor recovery aligned with the existing forum-read behavior.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `ListMessages` can now expose exact dialog/topic selectors without reopening topic fetch, stale-anchor refresh, or navigation behavior in the adapter.
- `SearchMessages` can reuse the same exact dialog seam instead of adding a search-only shortcut.
- Legacy name-based ambiguity and topic-fidelity coverage remains intact for the later public-surface plans.

## Verification

- `uv run pytest tests/test_capabilities.py -k "history or search or direct or topic or navigation" -q`
- `uv run pytest tests/test_capabilities.py -k "direct or history or search or topic" -q`
- `uv run pytest tests/test_tools.py -k "list_messages or search_messages or topic or ambiguity or direct" -q`

## Self-Check: PASSED

- Found `.planning/phases/17-direct-read-search-workflows/17-01-SUMMARY.md`
- Found task commits `c9bf733` and `412c1c8` in git history

---
*Phase: 17-direct-read-search-workflows*
*Completed: 2026-03-14*
