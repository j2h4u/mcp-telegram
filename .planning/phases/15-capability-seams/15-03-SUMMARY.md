---
phase: 15-capability-seams
plan: 03
subsystem: api
tags: [telegram, telethon, capabilities, search, testing, docker]
requires:
  - phase: 15-02
    provides: read/topic execution seams and thin public adapters
provides:
  - shared search execution seam for target resolution, sender warmup, context loading, and reaction enrichment
  - thin SearchMessages adapter that keeps only hit-local formatting and telemetry
  - CAP-01 proof through capability-level and adapter-level tests plus live runtime verification
affects: [phase-16-unified-navigation-contract, phase-17-direct-read-search-workflows, runtime-verification]
tech-stack:
  added: []
  patterns: [execute_*_capability adapters, capability seam tests, restarted-runtime verification]
key-files:
  created: []
  modified:
    - src/mcp_telegram/capabilities.py
    - src/mcp_telegram/tools.py
    - tests/test_capabilities.py
    - tests/test_tools.py
key-decisions:
  - "Keep SearchMessages responsible for hit-local context rendering and [HIT] marking while moving dialog resolution, sender warmup, context loading, reaction enrichment, and offset progression into capabilities.py."
  - "Prove CAP-01 with one capability-level search test and one thin-adapter test instead of brittle implementation-shape heuristics."
  - "Record runtime rebuild/restart verification as an empty chore commit because the task changes live state, not repository files."
patterns-established:
  - "Public read/search/topic tools delegate to capability execution helpers and keep MCP args, final text assembly, and telemetry local."
  - "Shared seam proof patches capability entrypoints directly so future internal changes do not require tool-body duplication."
requirements-completed: [CAP-01]
duration: 2m 28s
completed: 2026-03-13
---

# Phase 15 Plan 03: Search seam closure and live runtime proof

**Shared SearchMessages execution seam with centralized dialog resolution, enrichment, seam-proof tests, and restarted runtime validation**

## Performance

- **Duration:** 2m 28s
- **Started:** 2026-03-13T23:33:51Z
- **Completed:** 2026-03-13T23:36:19Z
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments
- Moved `SearchMessages` target resolution, sender cache warmup, context fetching, reaction-name enrichment, and `next_offset` calculation into `execute_search_messages_capability`.
- Reduced [`src/mcp_telegram/tools.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py) to a thin `SearchMessages` adapter that preserves hit-local context grouping, `[HIT]` labeling, and telemetry.
- Added seam-proof coverage in [`tests/test_capabilities.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_capabilities.py) and [`tests/test_tools.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py), then rebuilt and verified the live container.

## Task Commits

1. **Task 1: Rewire `SearchMessages` to shared target-resolution and enrichment paths** - `b9e5a4b` (`feat`)
2. **Task 2: Add final CAP-01 seam proof and run the full regression loop** - `8f6269e` (`test`)
3. **Task 3: Rebuild and restart the runtime, then verify the final Phase 15 surface in-container** - `e116202` (`chore`)

## Files Created/Modified
- [`src/mcp_telegram/capabilities.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/capabilities.py) - adds `SearchExecution`, shared search execution, and shared context-message loading.
- [`src/mcp_telegram/tools.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py) - delegates `SearchMessages` to the capability seam and keeps only output shaping.
- [`tests/test_capabilities.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_capabilities.py) - proves shared search enrichment and sender warmup happen in the capability layer.
- [`tests/test_tools.py`](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py) - proves `SearchMessages` now renders delegated capability results instead of rebuilding them locally.

## Decisions Made
- Kept search-specific hit grouping and `[HIT]` marking explicit in the tool adapter so the public output contract stays stable while the shared seam moves underneath it.
- Let the capability layer own `next_offset` calculation alongside shared enrichment so later navigation work can evolve one internal path rather than special-casing tool bodies.
- Verified the restarted container with an in-container source inspection that confirms both the public tools and `execute_search_messages_capability` are present in the live image.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 15 now has repository-level and runtime-level proof that read/search/topic behavior can evolve through capability-oriented internals.
- Phase 16 can build the unified navigation contract on top of one shared read/search substrate instead of a remaining tool-local search path.

## Self-Check: PASSED

- Verified [`15-03-SUMMARY.md`](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/15-capability-seams/15-03-SUMMARY.md) exists.
- Verified task commits `b9e5a4b`, `8f6269e`, and `e116202` exist in git history.

---
*Phase: 15-capability-seams*
*Completed: 2026-03-13*
