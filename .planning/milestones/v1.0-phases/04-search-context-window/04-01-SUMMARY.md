---
phase: 04-search-context-window
plan: 01
subsystem: testing
tags: [pytest, tdd, asyncmock, search, context-window]

# Dependency graph
requires:
  - phase: 03-new-tools
    provides: SearchMessages implementation, test infrastructure (conftest, make_mock_message)
provides:
  - 4 failing TDD stubs for TOOL-06 context window, hit marker, and reaction names
  - Updated test_search_messages_context with get_messages mock (Wave 1 ready)
affects:
  - 04-02 (Wave 1 — green phase that implements context window against these stubs)

# Tech tracking
tech-stack:
  added: []
  patterns:
    - TDD red baseline: write failing tests before implementation to lock behaviour contract
    - AsyncMock for get_messages: pattern for mocking batch-fetch Telegram API calls in tests

key-files:
  created: []
  modified:
    - tests/test_tools.py

key-decisions:
  - "Hit marker assertion uses [HIT]/>>>/=== HIT === (not date separator ---) to avoid false-green against current formatter output"
  - "context_window and context_after_hit written as separate test functions for clearer failure messages"
  - "test_search_messages_context updated with get_messages=AsyncMock(return_value=[]) before search_messages call so it does not crash when Wave 1 adds context fetch"

patterns-established:
  - "TDD stub pattern: mock get_messages = AsyncMock(return_value=[...]) alongside iter_messages for context fetch tests"

requirements-completed:
  - TOOL-06

# Metrics
duration: 5min
completed: 2026-03-11
---

# Phase 04 Plan 01: Search Context Window TDD Stubs Summary

**4 failing TOOL-06 tests establishing red baseline for ±3 context messages, hit marker, and reaction names fetch in SearchMessages**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-03-11T06:09:55Z
- **Completed:** 2026-03-11T06:14:00Z
- **Tasks:** 1
- **Files modified:** 1

## Accomplishments
- Updated `test_search_messages_context` to add `get_messages = AsyncMock(return_value=[])` — survives Wave 1 context fetch
- Added `test_search_messages_context_window`: asserts context messages before hit appear in output
- Added `test_search_messages_context_after_hit`: asserts context messages after hit appear in output
- Added `test_search_messages_hit_marker`: asserts `[HIT]`, `>>>`, or `=== HIT ===` marker present (not just date separator)
- Added `test_search_messages_reaction_names_fetched`: asserts `client.__call__` was invoked for GetMessageReactionsListRequest
- Full suite: 48 pass, 4 fail (exactly as planned)

## Task Commits

Each task was committed atomically:

1. **Task 1: Write 4 failing TOOL-06 tests and update existing context test** - `d147129` (test)

**Plan metadata:** _(docs commit follows)_

## Files Created/Modified
- `tests/test_tools.py` — 83 lines added: 1 updated test, 4 new failing stubs

## Decisions Made
- Hit marker assertion tightened from `"---" in text` (too loose — date separator already matches) to require `[HIT]`, `>>>`, or `=== HIT ===` — ensures test is genuinely RED against current formatter
- Kept context_window and context_after_hit as separate functions rather than combining, for distinct failure messages

## Deviations from Plan

None — plan executed exactly as written, with one minor adjustment: the hit marker assertion was initially too loose (passing due to date separator `---`), tightened on the same task before commit to ensure genuine red baseline.

## Issues Encountered
- Initial hit marker assertion `("---" in text)` passed because formatter already emits `--- 2024-01-15 ---` date separators. Caught and fixed within Task 1 before commit — no additional commit needed.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Red baseline established: 4 tests fail against current tools.py
- Plan 04-02 (Wave 1) can implement context fetch, hit marker, and reaction names loop to make all 4 green
- No blockers

---
*Phase: 04-search-context-window*
*Completed: 2026-03-11*
