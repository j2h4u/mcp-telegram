---
phase: 16-unified-navigation-contract
plan: 01
subsystem: api
tags: [telegram, navigation, pagination, capabilities, pytest]
requires:
  - phase: 15-capability-seams
    provides: history and search execution seams below the tool adapters
provides:
  - shared opaque navigation tokens for history and search capability flows
  - capability-level navigation metadata that later plans can surface publicly
  - mismatch-protection coverage for dialog, query, tool, and topic-scoped reuse
affects: [16-02, 16-03, ListMessages, SearchMessages]
tech-stack:
  added: []
  patterns: [opaque navigation token family, capability-level navigation bridge, legacy adapter preservation]
key-files:
  created: []
  modified:
    - src/mcp_telegram/pagination.py
    - src/mcp_telegram/capabilities.py
    - tests/test_capabilities.py
    - tests/test_tools.py
key-decisions:
  - "Keep the shared navigation contract internal to the capability layer for Plan 16-01 while ListMessages and SearchMessages continue exposing cursor/offset publicly."
  - "Encode history navigation with dialog and topic scope, and encode search navigation with dialog and query scope, so mismatched reuse fails before Telegram paging executes."
patterns-established:
  - "Capability navigation field: execution results can carry shared opaque continuation state without forcing immediate public-schema changes."
  - "Legacy adapter bridge: capability internals may converge first while tool adapters keep brownfield cursor/offset names until a later migration plan."
requirements-completed: [NAV-01, NAV-02]
duration: 14 min
completed: 2026-03-14
---

# Phase 16 Plan 01: Unified Navigation Contract Summary

**Shared opaque history/search navigation tokens with capability-level continuation metadata and mismatch-safe reuse guards**

## Performance

- **Duration:** 14 min
- **Started:** 2026-03-13T23:50:08Z
- **Completed:** 2026-03-14T00:04:08Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Added one opaque token family in [src/mcp_telegram/pagination.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py) that can encode history continuation and search continuation with dialog/topic/query scope.
- Extended [src/mcp_telegram/capabilities.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/capabilities.py) so history and search execution results now expose the same `navigation` shape while the legacy `next_cursor` and `next_offset` adapter fields remain intact.
- Added capability-first contract anchors in [tests/test_capabilities.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_capabilities.py) and adapter-preservation checks in [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py).
- Verified the seam with `uv run pytest tests/test_capabilities.py -k "history or search or cursor or offset or navigation" -q`, `uv run pytest tests/test_tools.py -k "list_messages or search_messages or cursor or offset or topic" -q`, and `uv run pytest tests/test_pagination.py -q`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Define one shared navigation primitive across history and search capabilities** - `943d3e9` (feat)
2. **Task 2: Add contract anchors for invalid and mismatched navigation reuse** - `9f344e0` (test)

## Files Created/Modified
- `src/mcp_telegram/pagination.py` - Shared opaque token encode/decode helpers plus history/search scope validation.
- `src/mcp_telegram/capabilities.py` - Shared capability-level `navigation` output, search token decoding, and action-oriented mismatch failures.
- `tests/test_capabilities.py` - Direct coverage for shared history/search tokens and mismatched dialog/query/tool reuse.
- `tests/test_tools.py` - Adapter-level checks that public footers stay on `next_cursor` and `next_offset` in this plan.

## Decisions Made
- Kept the public tool adapters unchanged in this plan so Phase 16 can prove the internal navigation contract before reflected schema migration begins.
- Used stricter shared-token scoping than the legacy adapters: history tokens validate dialog and topic, while search tokens validate dialog and exact query text.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Filled in Phase 16 plan count in STATE.md**
- **Found during:** Final state updates after Task 2
- **Issue:** `.planning/STATE.md` still had `Total Plans in Phase: TBD`, which caused `gsd-tools state advance-plan` to fail before the plan could be recorded complete.
- **Fix:** Updated `STATE.md` to reflect the three on-disk Phase 16 plan files, then reran the standard state/roadmap update commands.
- **Files modified:** `.planning/STATE.md`
- **Verification:** `state advance-plan`, `state update-progress`, and `roadmap update-plan-progress 16` all succeeded after the fix.
- **Committed in:** final docs commit for plan bookkeeping

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** No product-surface scope change. The fix only unblocked required GSD bookkeeping for this plan.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `ListMessages` can migrate to the shared public navigation vocabulary by consuming the capability-level `navigation` field instead of inventing new continuation logic in the adapter.
- `SearchMessages` is ready for the same migration, with query-safe reuse already covered at the capability boundary.
- Topic-scoped read pagination remains covered while the public schema is still on the legacy cursor vocabulary.

## Self-Check: PASSED

- Found `.planning/phases/16-unified-navigation-contract/16-01-SUMMARY.md`
- Found commit `943d3e9`
- Found commit `9f344e0`
