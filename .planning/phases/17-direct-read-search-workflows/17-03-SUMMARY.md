---
phase: 17-direct-read-search-workflows
plan: 03
subsystem: api
tags: [telegram, telethon, mcp, search, schema, telemetry]
requires:
  - phase: 17-direct-read-search-workflows
    provides: "Exact-target capability lanes and direct ListMessages selectors from Plans 01-02"
provides:
  - "Direct SearchMessages workflow through the existing dialog/query contract"
  - "Search hit-window rendering owned by formatter/capability seams instead of tools.py"
  - "Reflected local and restarted-runtime schema proof for the final Phase 17 read/search surface"
affects: [18-surface-posture-rollout-proof, SearchMessages, ListMessages, runtime-verification]
tech-stack:
  added: []
  patterns:
    - "Signed numeric dialog selectors route through exact-target capability inputs without new public fields"
    - "Search adapters consume pre-rendered capability output and only add MCP-specific framing"
    - "Schema-sensitive changes require both local reflection and rebuilt-container verification"
key-files:
  created: []
  modified:
    - src/mcp_telegram/tools.py
    - src/mcp_telegram/capabilities.py
    - src/mcp_telegram/formatter.py
    - tests/test_tools.py
    - tests/test_capabilities.py
    - tests/test_server.py
    - tests/test_analytics.py
key-decisions:
  - "Kept SearchMessages on the existing dialog/query contract and treated signed numeric dialog strings as the exact-target fast path."
  - "Moved hit-window grouping and hit markers into formatter/capability helpers so the adapter only handles MCP framing and telemetry."
patterns-established:
  - "Primary tool schema simplification can happen without adding parallel public exact-selector fields when the existing contract can carry an internal fast path."
  - "Runtime-sensitive MCP schema changes close only after the long-lived container is rebuilt and reflected in-container."
requirements-completed: [FLOW-01, FLOW-02]
duration: 9 min
completed: 2026-03-14
---

# Phase 17 Plan 03: Direct Search Workflow Summary

**SearchMessages now accepts direct numeric dialog selectors through the existing contract, renders hit-local groups from the shared seam, and is proven in both local reflection and the rebuilt runtime.**

## Performance

- **Duration:** 9 min
- **Started:** 2026-03-14T10:35:01Z
- **Completed:** 2026-03-14T10:43:49Z
- **Tasks:** 3
- **Files modified:** 7

## Accomplishments
- Routed direct known-dialog searches through the exact-target capability seam without adding a new public search field.
- Moved search hit-window assembly and hit markers out of `tools.py` and into formatter/capability helpers while keeping bounded local context and readable grouped output.
- Proved the final contract with local tests, local reflection, and a rebuilt `mcp-telegram` container exposing the same `ListMessages` and `SearchMessages` schemas.

## Task Commits

Each task was committed atomically:

1. **Task 1: Expose direct search entry and move hit-window assembly below the adapter** - `c1eb3d2` (`feat`)
2. **Task 2: Update schema and telemetry coverage for the final Phase 17 primary-tool contract** - `7049a17` (`test`)
3. **Task 3: Rebuild and restart the runtime to prove the direct-workflow contract is live** - `3dc000d` (`chore`)

**Plan metadata:** recorded in the final docs commit for this summary and state update.

## Files Created/Modified
- `src/mcp_telegram/tools.py` - Detects signed numeric dialog selectors and keeps `SearchMessages` on the thin adapter path.
- `src/mcp_telegram/capabilities.py` - Produces pre-rendered search output from the shared search execution seam.
- `src/mcp_telegram/formatter.py` - Builds hit-local windows and inserts `[HIT]` markers through reusable formatting helpers.
- `tests/test_tools.py` - Covers direct search routing, tool schema expectations, and seam-owned output behavior.
- `tests/test_capabilities.py` - Proves rendered search groups and exact-dialog search execution at the capability layer.
- `tests/test_server.py` - Verifies reflected `SearchMessages` schema teaches the direct numeric dialog path clearly.
- `tests/test_analytics.py` - Guards telemetry against leaking exact-selector fields or other identifying payloads.

## Decisions Made
- Kept `SearchMessages(dialog, query, navigation)` as the public surface because the existing `dialog` field can carry both fuzzy and exact-id flows cleanly.
- Used cache-backed dialog names when available for numeric direct-path searches so no-hit and reflected output stay readable without extra public inputs.
- Recorded runtime verification as its own atomic task commit because the rebuild/restart step changes release confidence rather than source behavior.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered
- `uv run cli.py list-tools` could not use the sandboxed `~/.cache/uv` path; rerunning with the approved `uv run cli.py` prefix resolved local reflection verification.
- One schema regression test still assumed pre-Plan-02 required fields; it was updated to match the current direct-read/search reflected contract.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 17 is complete: direct read and search workflows now land on the simplified primary-tool contract with runtime proof.
- Phase 18 can focus on helper-surface posture and rollout-proof policy without reopening the search/read workflow shape.

## Self-Check: PASSED

- Found summary file: `.planning/phases/17-direct-read-search-workflows/17-03-SUMMARY.md`
- Found task commits: `c1eb3d2`, `7049a17`, `3dc000d`
