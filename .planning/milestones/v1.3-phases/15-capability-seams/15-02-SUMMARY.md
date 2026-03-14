---
phase: 15-capability-seams
plan: 02
subsystem: api
tags: [telegram, telethon, capabilities, topics, pagination, testing]
requires:
  - phase: 15-01
    provides: dialog-target and forum-topic capability primitives used by the thin adapters
provides:
  - thin ListTopics adapter over capability execution
  - thin ListMessages adapter over shared history-read execution
  - capability-level read/topic seam tests plus adapter delegation proofs
affects: [15-03, SearchMessages, runtime-verification]
tech-stack:
  added: []
  patterns:
    - thin public adapters over capability execution results
    - capability-owned topic read, pagination, and enrichment flow
key-files:
  created:
    - .planning/phases/15-capability-seams/15-02-SUMMARY.md
  modified:
    - src/mcp_telegram/capabilities.py
    - src/mcp_telegram/tools.py
    - tests/test_capabilities.py
    - tests/test_tools.py
key-decisions:
  - "Keep ListTopics and ListMessages responsible only for MCP args, telemetry, and final text assembly."
  - "Move history-read cursor handling, sender resolution, topic recovery, and enrichment hooks into capabilities.py so SearchMessages can reuse them later."
  - "Prove adapter thinness explicitly with tests that patch the capability execution entrypoints instead of only asserting public behavior."
patterns-established:
  - "Capability execution first: tools call a capability entrypoint and only adapt its result to MCP text."
  - "Brownfield behavior stays anchored by both direct capability tests and public tool regressions."
requirements-completed: [CAP-01]
duration: 2 min
completed: 2026-03-13
---

# Phase 15 Plan 02: Read/Topic Adapter Thinning Summary

**Shared capability execution now owns topic listing, history-read setup, topic recovery, and message enrichment while `ListTopics` and `ListMessages` stay thin MCP adapters**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-13T23:24:20Z
- **Completed:** 2026-03-13T23:25:45Z
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments
- Moved `ListTopics` dialog/topic orchestration behind `execute_list_topics_capability()`.
- Moved `ListMessages` cursor handling, sender resolution, topic-scoped fetch/recovery, reply/reaction enrichment, and cross-topic labeling behind `execute_history_read_capability()`.
- Added direct capability tests and adapter-delegation tests that preserve deleted, inaccessible, stale-topic, cursor, unread, sender, and `from_beginning` behavior.
- Quick-test command used: `uv run pytest tests/test_capabilities.py -q && uv run pytest tests/test_tools.py -k "topic or cursor or unread or from_beginning" -q`

## Task Commits

Each task was committed atomically:

1. **Task 1: Rewire `ListTopics` and `ListMessages` to delegate to shared capabilities** - `c3bab10` (feat)
2. **Task 2: Prove preserved topic recovery and read-path fidelity after adapter thinning** - `6de9b76` (test)
3. **Task 3: Refresh the runtime and verify the thinned read/topic adapters in-container** - `25c370b` (chore)

**Plan metadata:** pending final docs commit

## Files Created/Modified
- `src/mcp_telegram/capabilities.py` - Added high-level topic-list and history-read execution seams plus shared enrichment helpers.
- `src/mcp_telegram/tools.py` - Reduced `ListTopics` and `ListMessages` to capability delegation, formatting, and telemetry.
- `tests/test_capabilities.py` - Added direct execution-seam coverage for topic listing, topic-thread sender filtering, and invalid cursor handling.
- `tests/test_tools.py` - Added adapter delegation tests while keeping the existing brownfield regressions intact.

## Decisions Made
- Kept the public tool schemas and cursor contract unchanged so Phase 15 stays below the Phase 16 navigation boundary.
- Left SearchMessages migration for Plan 15-03, but moved shared history enrichment into the capability layer now so that migration does not need another foundation extraction.
- Recorded runtime freshness with a separate task commit because the live container restart is part of the plan’s required proof even though it does not change tracked source files.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `SearchMessages` can now migrate onto the same capability-owned dialog resolution and enrichment flow without another foundation refactor.
- Topic deletion, inaccessibility, stale-anchor recovery, cursor emission, unread filtering, sender filtering, and reverse pagination remain anchored by repository tests.
- The restarted `mcp-telegram` container has already been rebuilt and verified to expose `ListTopics`, `ListMessages`, and `SearchMessages`.

## Self-Check: PASSED

---
*Phase: 15-capability-seams*
*Completed: 2026-03-13*
