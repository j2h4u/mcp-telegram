---
phase: 17-direct-read-search-workflows
plan: 02
subsystem: api
tags: [telegram, tools, capabilities, forum-topics, schema, testing]
requires:
  - phase: 17-direct-read-search-workflows
    provides: exact-target capability seams for known dialogs and topics
provides:
  - direct `ListMessages` selectors for known dialog and topic targets
  - explicit selector-conflict validation at the MCP boundary
  - regression and reflection coverage for direct forum reads and schema visibility
affects: [tools, direct-workflows, forum-topics, schema-reflection]
tech-stack:
  added: []
  patterns: [thin tool adapters, opt-in exact selectors, reflected schema proof]
key-files:
  created: []
  modified:
    - src/mcp_telegram/tools.py
    - src/mcp_telegram/capabilities.py
    - tests/test_tools.py
    - tests/test_server.py
key-decisions:
  - "ListMessages now accepts either natural-name selectors or exact dialog/topic ids, but rejects mixed fuzzy and exact selector combinations instead of silently choosing one."
  - "Direct forum reads continue through the shared capability seam so deleted topics, inaccessible topics, General handling, unread filtering, and navigation stay aligned with the brownfield path."
patterns-established:
  - "Primary tool schema changes should ship with reflected schema assertions and boundary validation tests in the same plan."
  - "Exact-target public fields should expose the direct path while leaving name-based ambiguity handling intact as the default exploratory workflow."
requirements-completed: [FLOW-01, FLOW-02]
duration: 11 min
completed: 2026-03-14
---

# Phase 17 Plan 02: Direct `ListMessages` workflow for known targets

**`ListMessages` now exposes exact dialog and topic selectors, keeps forum-topic fidelity intact, and proves the changed contract through brownfield tests and local reflection**

## Performance

- **Duration:** 11 min
- **Started:** 2026-03-14T10:16:13Z
- **Completed:** 2026-03-14T10:27:09Z
- **Tasks:** 2
- **Files modified:** 4

## Accomplishments
- Added `exact_dialog_id` and `exact_topic_id` to `ListMessages`, made `dialog` optional, and added validation that rejects missing selectors or mixed fuzzy/exact selector combinations.
- Routed direct read calls through the Phase 17 capability seam so known dialog and topic targets bypass discovery-oriented setup without losing deleted-topic tombstones, inaccessible-topic recovery, `General` semantics, unread scoping, or shared navigation behavior.
- Added regression coverage for direct forum reads and reflection coverage proving the new `ListMessages` schema and boundary validation are visible in local tooling.

## Task Commits

Each task was committed atomically:

1. **Task 1: Expose exact-target direct-read entry on `ListMessages`** - `f2b4c98` (`feat`)
2. **Task 2: Preserve forum-read fidelity and prove the reflected `ListMessages` schema** - `1d63c3b` (`test`)

## Files Created/Modified
- `src/mcp_telegram/tools.py` - Added exact dialog/topic selector fields, selector validation, and direct capability routing for `ListMessages`.
- `src/mcp_telegram/capabilities.py` - Kept exact-topic read failures and cross-topic labeling aligned with the shared history-read seam.
- `tests/test_tools.py` - Added direct-read regressions for known topics, General, unread continuation, deleted topics, and inaccessible-topic handling.
- `tests/test_server.py` - Added reflected schema and MCP-boundary validation checks for the new direct-read contract.

## Decisions Made
- `ListMessages` now teaches the direct-read path explicitly through schema fields rather than relying on helper choreography when the dialog or topic is already known.
- Conflicting selector combinations fail at validation time so the tool surface stays predictable and ambiguity-safe.

## Deviations from Plan

None.

## Issues Encountered

- One legacy adapter assertion still expected exact-target kwargs to be absent on the fuzzy path; updated it to assert explicit `None` values once the new capability call shape landed.
- `uv run cli.py list-tools` initially failed due to sandbox-local cache permissions; reran successfully with `UV_CACHE_DIR=/tmp/.uv-cache`.

## User Setup Required

None.

## Next Phase Readiness

- `SearchMessages` can now mirror the direct-workflow posture without inventing a separate selector model for reads.
- The runtime-sensitive schema proof for Phase 17 has a clean local reflection baseline before the rebuild/restart work in Plan 03.

## Verification

- `UV_CACHE_DIR=/tmp/.uv-cache uv run pytest tests/test_tools.py -k "list_messages and (direct or topic or ambiguity or navigation)" -q`
- `UV_CACHE_DIR=/tmp/.uv-cache uv run pytest tests/test_tools.py -k "list_messages or topic or unread or ambiguity or direct" -q`
- `UV_CACHE_DIR=/tmp/.uv-cache uv run pytest tests/test_server.py -q`
- `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`

## Self-Check: PASSED

- Found `.planning/phases/17-direct-read-search-workflows/17-02-SUMMARY.md`
- Found task commits `f2b4c98` and `1d63c3b` in git history

---
*Phase: 17-direct-read-search-workflows*
*Completed: 2026-03-14*
