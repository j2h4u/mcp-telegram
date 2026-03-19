---
phase: 23-prefetch-lazy-refresh
plan: "01"
subsystem: cache
tags: [asyncio, prefetch, sqlite, telethon, tdd]

requires:
  - phase: 22-edit-detection
    provides: MessageCache.store_messages() write path reused by prefetch tasks
  - phase: 21-cache-first-reads
    provides: HistoryDirection enum and try_read_page coverage detection

provides:
  - PrefetchCoordinator class with schedule()/dedup/_in_flight lifecycle
  - _prefetch_task coroutine with NEWEST/OLDEST iter_messages semantics
  - _delta_refresh_task coroutine with min_id=last_id semantics
  - _next_prefetch_anchor helper for computing next-page anchor from message list

affects:
  - 23-02 (Plan 02 wires PrefetchCoordinator into capability_history)

tech-stack:
  added: []
  patterns:
    - "Fire-and-forget asyncio tasks via create_task() with dedup set"
    - "coro.close() to suppress ResourceWarning when dedup rejects a coroutine"
    - "TYPE_CHECKING guard + runtime import inside functions for HistoryDirection (avoids circular import)"
    - "finally: _in_flight.discard(key) ensures key release on success and failure"

key-files:
  created:
    - src/mcp_telegram/prefetch.py
    - tests/test_prefetch.py
  modified: []

key-decisions:
  - "coro.close() called on rejected duplicate coroutine to prevent 'coroutine never awaited' ResourceWarning"
  - "type: ignore[attr-defined] on client.iter_messages — client typed as object to avoid coupling to TelegramClient"
  - "topic_id=1 sentinel (General topic) excluded from reply_to scoping — same convention as cache.py and capability_history"

patterns-established:
  - "PrefetchKey tuple[int, str, int | None, int | None] as dedup key type alias"
  - "Background task wrapper _run() with try/except/finally separates lifecycle from business logic"

requirements-completed: [PRE-04, PRE-05, REF-02, REF-03]

duration: 7min
completed: "2026-03-19"
---

# Phase 23 Plan 01: PrefetchCoordinator + Task Coroutines Summary

**asyncio-based PrefetchCoordinator with in-flight dedup set, NEWEST/OLDEST prefetch coroutine, and min_id delta refresh coroutine — all backed by MessageCache.store_messages()**

## Performance

- **Duration:** 7 min
- **Started:** 2026-03-19T23:16:12Z
- **Completed:** 2026-03-19T23:18:37Z
- **Tasks:** 1
- **Files modified:** 2

## Accomplishments

- PrefetchCoordinator.schedule() returns True on first call for a key, False for duplicates — dedup prevents redundant API calls (PRE-05)
- Keys always released in finally block so transient failures allow retry on next user read
- _prefetch_task uses max_id for NEWEST direction and min_id + reverse=True for OLDEST, matching Telethon iter_messages semantics (PRE-04)
- _delta_refresh_task passes min_id=last_id + reverse=True to pull only newer messages into cache (REF-02)
- No asyncio.sleep, no Timer, no periodic loop — all prefetch is demand-triggered (REF-03)
- 24 unit tests added; full suite 363 passing; zero mypy errors

## Task Commits

Each task was committed atomically:

1. **Task 1: PrefetchCoordinator + task coroutines with full TDD coverage** - `1489e1c` (feat)

**Plan metadata:** (docs commit follows)

_Note: TDD task — RED skeleton, GREEN implementation, REFACTOR (mypy type-ignore fix)_

## Files Created/Modified

- `src/mcp_telegram/prefetch.py` - PrefetchCoordinator, _prefetch_task, _delta_refresh_task, _next_prefetch_anchor
- `tests/test_prefetch.py` - 24 unit tests covering all behaviors from plan

## Decisions Made

- `coro.close()` on rejected duplicate: prevents ResourceWarning from unawaited coroutine when dedup fires
- `type: ignore[attr-defined]` on `client.iter_messages`: client parameter typed as `object` to avoid importing TelegramClient (no new deps in prefetch.py)
- `topic_id=1` excluded from `reply_to` scoping: General topic in Telethon doesn't need reply_to — same convention already established in cache.py

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed incorrect `type: ignore` comment code**
- **Found during:** Task 1 (REFACTOR phase, mypy check)
- **Issue:** Plan scaffold used `# type: ignore[union-attr]` but mypy reported `attr-defined` as the actual error code
- **Fix:** Updated both occurrences in prefetch.py to `# type: ignore[attr-defined]`
- **Files modified:** src/mcp_telegram/prefetch.py
- **Verification:** `uv run mypy src/mcp_telegram/prefetch.py --no-error-summary` exits 0
- **Committed in:** 1489e1c (Task 1 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - wrong type: ignore code in plan scaffold)
**Impact on plan:** Trivial fix required for zero mypy errors. No scope creep.

## Issues Encountered

None — plan executed with one minor type-annotation correction.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- PrefetchCoordinator and all coroutines ready for Plan 02 integration into capability_history
- Plan 02 needs to: instantiate PrefetchCoordinator, call schedule() after live reads, pass _next_prefetch_anchor output as anchor_id
- No blockers

---
*Phase: 23-prefetch-lazy-refresh*
*Completed: 2026-03-19*

## Self-Check: PASSED

- FOUND: src/mcp_telegram/prefetch.py
- FOUND: tests/test_prefetch.py
- FOUND: .planning/phases/23-prefetch-lazy-refresh/23-01-SUMMARY.md
- FOUND: commit 1489e1c (feat(23-01): PrefetchCoordinator + background task coroutines)
