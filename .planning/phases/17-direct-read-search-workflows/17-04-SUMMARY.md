---
phase: 17-direct-read-search-workflows
plan: 04
subsystem: runtime
tags: [telegram, sqlite, cache, concurrency, runtime-verification]
requires:
  - phase: 17-direct-read-search-workflows
    provides: "Direct ListMessages/SearchMessages workflows and rebuilt-runtime verification discipline from Plans 01-03"
provides:
  - "Serialized shared-cache bootstrap that stays read-safe across parallel MCP session startup"
  - "Regression coverage for the diagnosed SQLite lock path and preserved direct read/search behavior"
  - "Restarted-runtime proof that parallel MCP sessions no longer fail with constructor-time cache locks"
affects: [cache, runtime-verification, ListMessages, SearchMessages, phase-17-gap-closure]
tech-stack:
  added: []
  patterns:
    - "One-time SQLite bootstrap work runs behind a dedicated lock instead of every process open"
    - "Runtime-only concurrency gaps close only after rebuilt-container reproduction is re-run successfully"
key-files:
  created: []
  modified:
    - src/mcp_telegram/cache.py
    - tests/test_cache.py
    - tests/test_tools.py
key-decisions:
  - "Serialize cache schema/bootstrap work with a lock file and a dedicated connection so normal cache opens stay read-safe under parallel startup."
  - "Keep the fix inside the cache boundary and preserve the existing direct read/search tool contract instead of changing public tool inputs."
patterns-established:
  - "Constructor-time SQLite maintenance such as schema setup and optimization must not run unguarded on every MCP process start."
  - "Parallel-session runtime failures require rebuilt-container proof in addition to repository tests."
requirements-completed: [FLOW-01, FLOW-02]
duration: 46 min
completed: 2026-03-14
---

# Phase 17 Plan 04: Shared cache bootstrap concurrency gap closure

**Shared SQLite cache bootstrap is now serialized and read-safe across parallel MCP session startup, with regression tests and rebuilt-runtime proof that direct read/search workflows still hold.**

## Performance

- **Duration:** 46 min
- **Started:** 2026-03-14T12:09:18Z
- **Completed:** 2026-03-14T12:55:01Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- Moved one-time cache bootstrap work behind a serialized file-lock path and a dedicated connection so normal `EntityCache` opens no longer perform lock-prone setup on every process start.
- Removed constructor-time maintenance pressure from the hot cache-open path while preserving cache schema, TTL semantics, and the direct read/search behavior introduced earlier in Phase 17.
- Added regression tests that cover the diagnosed lock shape and prove direct topic reads plus numeric-dialog searches still behave correctly after the cache hardening.
- Rebuilt and restarted the long-lived `mcp-telegram` container, then re-ran the parallel-session reproduction and in-container schema checks successfully.

## Task Commits

Each task was committed atomically:

1. **Task 1: Make shared cache initialization read-safe across parallel MCP processes** - `8d6c888` (`fix`)
2. **Task 2: Add regression proof for the diagnosed lock interleaving without regressing read/search flows** - `67f27fb` (`test`)
3. **Task 3: Rebuild and verify the long-lived runtime with parallel MCP sessions** - `b736e7b` (`chore`)

**Plan metadata:** recorded in the final docs commit for this summary and state/roadmap update.

## Files Created/Modified

- `src/mcp_telegram/cache.py` - Adds serialized bootstrap checks, dedicated schema setup helpers, and a read-safe normal connection path.
- `tests/test_cache.py` - Covers locked-writer cache open behavior and temporary WAL-setup lock tolerance.
- `tests/test_tools.py` - Keeps direct topic reads and numeric-dialog searches pinned after the cache hardening.

## Decisions Made

- The diagnosed lock was isolated to cache bootstrap, so no `tools.py` changes were needed after inspection.
- `PRAGMA optimize` and other maintenance-sensitive work stay out of normal per-process cache opens; bootstrap happens only when the schema or journal mode actually needs work.
- Live runtime verification remains the closure gate because the failure was discovered only through the long-lived container reproduction.

## Deviations from Plan

- The planned `src/mcp_telegram/tools.py` edit was unnecessary. The failure was resolved entirely at the cache boundary without reopening the public tool surface.

## Issues Encountered

- The Docker rebuild hit a local sandbox/buildx permission boundary during orchestration; rerunning the rebuild with approved escalated permissions completed successfully.

## User Setup Required

None.

## Next Phase Readiness

- Phase 17 now has its diagnosed runtime gap closed as well as repository and live-runtime proof.
- Phase 18 can proceed without carrying forward the parallel-session cache-startup failure.

## Verification

- `uv run pytest tests/test_cache.py -k "locked or cross_process or concurrent" -q`
- `uv run pytest tests/test_tools.py -k "(list_messages and (direct or topic or navigation)) or (search_messages and (hit or context or navigation)) or routes_numeric_dialog_to_exact_capability" -q`
- `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram`
- Parallel `ListMessages` calls through `devtools.mcp_client.cli` against the restarted container with no `database is locked` or `runtime execution failed` markers in either session log
- `SearchMessages` live call against the restarted container plus in-container schema assertions for `ListMessages` and `SearchMessages`

## Self-Check: PASSED

- Found `.planning/phases/17-direct-read-search-workflows/17-04-SUMMARY.md`
- Found task commits `8d6c888`, `67f27fb`, and `b736e7b` in git history

---
*Phase: 17-direct-read-search-workflows*
*Completed: 2026-03-14*
