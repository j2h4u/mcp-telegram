---
phase: 14-boundary-recovery
plan: 02
subsystem: api
tags: [mcp, server-boundary, error-handling, pytest, docker]
requires:
  - phase: 14-boundary-recovery
    provides: explicit boundary contract tests for escaped validation and runtime failures
provides:
  - stage-aware escaped-error recovery text in server.call_tool
  - preserved handler-local recovery and telemetry regression proof
  - restarted-runtime verification for actionable boundary failures in the live container
affects: [ERR-01, 15-capability-seams, src/mcp_telegram/server.py]
tech-stack:
  added: []
  patterns: [boundary-local escaped-error shaping, restarted-runtime verification against the long-lived docker container]
key-files:
  created: [.planning/phases/14-boundary-recovery/14-02-SUMMARY.md]
  modified: [src/mcp_telegram/server.py]
key-decisions:
  - "Keep the fix bounded to server.py with one helper instead of introducing a new cross-repo exception framework."
  - "Handle tool_args and tool_runner failures in separate branches so validation and runtime stages return different actionable guidance."
patterns-established:
  - "Escaped boundary failures must include the tool name, failure stage, safe detail, and one next-step hint."
  - "Runtime-affecting contract changes are not complete until the long-lived container is rebuilt, restarted, and probed from inside the container."
requirements-completed: [ERR-01]
duration: 8 min
completed: 2026-03-14
---

# Phase 14 Plan 02: Boundary Recovery Summary

**`server.call_tool()` now returns stage-aware actionable escaped-error text and the restarted container proves the live boundary matches the repo contract.**

## Performance

- **Duration:** 8 min
- **Started:** 2026-03-13T20:55:10Z
- **Completed:** 2026-03-13T21:03:10Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Replaced the generic `Tool <name> failed` collapse with boundary-local error text that names the tool, distinguishes validation versus runtime failures, and adds one recovery step.
- Kept the change bounded to `src/mcp_telegram/server.py` while preserving the pass-through handler recovery contract and telemetry-on-error regression anchors.
- Rebuilt and restarted the long-lived `mcp-telegram` container, then proved from inside the restarted runtime that an escaped failure returns actionable detail instead of the old generic wrapper.

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement a safe escaped-error formatter at the server boundary** - `18dc232` (fix)
2. **Task 2: Prove telemetry safety and restarted-runtime behavior** - `d3536d7` (chore)

**Plan metadata:** pending

## Files Created/Modified
- `src/mcp_telegram/server.py` - Added `_safe_boundary_error_text()` and split `call_tool()` into validation and runtime error branches with stage-specific actionable messages.
- `.planning/phases/14-boundary-recovery/14-02-SUMMARY.md` - Execution record for the boundary recovery implementation, regression proof, and restarted-runtime verification.
- `.planning/STATE.md` - Advanced execution state after Plan 14 completion.
- `.planning/ROADMAP.md` - Marked Phase 14 as complete and updated milestone progress.
- `.planning/REQUIREMENTS.md` - Marked `ERR-01` complete in the requirement checklist and traceability table.

## Decisions Made
- The Phase 14 fix stays in `server.py`; tool-level recovery behavior and telemetry handling remain authoritative where they already exist.
- Safe escaped-error detail uses the exception text only after whitespace normalization, traceback suppression, and truncation, with fallback to the exception type when needed.
- Runtime proof uses an in-container deterministic escaped failure so the acceptance gate checks the rebuilt image instead of only the local checkout.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Normalized planning state after `state advance-plan` failed to parse the inherited STATE body**
- **Found during:** Post-task state updates
- **Issue:** `state advance-plan` returned `Cannot parse Current Plan or Total Plans in Phase from STATE.md`, leaving the frontmatter and human-readable state out of sync even though the rest of the close-out updates succeeded.
- **Fix:** Manually normalized `.planning/STATE.md` to point to Phase 15 as the next ready-to-plan target and aligned the stale Phase 14 roadmap detail text to show `02 complete`.
- **Files modified:** `.planning/STATE.md`, `.planning/ROADMAP.md`
- **Verification:** `STATE.md` now records Phase 15 / `ready_to_plan` / `100%`, and `ROADMAP.md` shows Phase 14 as `2/2` complete.
- **Committed in:** final docs commit

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** Limited to execution metadata normalization so the planning state reflects the completed Phase 14 work. No scope change to the server-boundary implementation.

## Issues Encountered
- The first `docker exec ... python -c` runtime probe used an invalid inline `async def` form and failed with `SyntaxError`. The verification was rerun immediately with a loop-based probe that exercised the same `server.call_tool()` boundary inside the restarted container.
- `state advance-plan` still could not parse the inherited `STATE.md` body format, so the final next-phase position was normalized manually after the other GSD state updates completed.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- `ERR-01` is complete with both repo-level and restarted-runtime proof, so Phase 15 can assume the boundary no longer collapses escaped failures to the generic wrapper.
- The existing handler-local recovery paths in `tests/test_tools.py` stayed green, so later work can build on the current tool behavior without reopening this boundary fix.

## Self-Check: PASSED
- Verified `.planning/phases/14-boundary-recovery/14-02-SUMMARY.md` exists.
- Verified task commits `18dc232` and `d3536d7` exist in git history.

---
*Phase: 14-boundary-recovery*
*Completed: 2026-03-14*
