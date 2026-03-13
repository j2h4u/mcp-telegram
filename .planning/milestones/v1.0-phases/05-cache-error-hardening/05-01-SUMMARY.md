---
phase: 05-cache-error-hardening
plan: 01
subsystem: testing
tags: [pytest, tdd, cache, entity-cache, ttl, cursor, search]

# Dependency graph
requires:
  - phase: 04-search-context-window
    provides: search_messages with context window and offset pagination
provides:
  - "5 failing TDD stubs covering all_names_with_ttl (CACH-01), search upsert (CACH-02), cursor error (TOOL-03)"
affects: [05-02]

# Tech tracking
tech-stack:
  added: []
  patterns: [monkeypatch.setattr on instance method for MagicMock spy, wraps=original for upsert spy]

key-files:
  created: []
  modified:
    - tests/test_cache.py
    - tests/test_tools.py

key-decisions:
  - "Stub stale-entity tool test uses monkeypatch on instance attribute (not class) to avoid polluting EntityCache for other tests"
  - "Upsert spy uses MagicMock(wraps=mock_cache.upsert) to capture calls while delegating to real implementation"
  - "Invalid cursor stub passes cursor before async client context opens — test needs no client interaction"

patterns-established:
  - "TDD Wave 0: write all stubs first, verify they fail for the right reason, then implement in Wave 1"
  - "Instance-level monkeypatching: monkeypatch.setattr(instance, 'method', MagicMock()) for per-test spy isolation"

requirements-completed: [CACH-01, CACH-02, TOOL-03]

# Metrics
duration: 5min
completed: 2026-03-11
---

# Phase 5 Plan 01: Cache Error Hardening Stubs Summary

**5 TDD Red stubs defining exact observable behaviour for TTL-filtered name resolution, search sender upsert, and cursor error message before Plan 02 implements production code**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-03-11T12:54:36Z
- **Completed:** 2026-03-11T12:59:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Added 2 failing stubs to test_cache.py targeting `all_names_with_ttl(user_ttl, group_ttl)` method (CACH-01)
- Added 3 failing stubs to test_tools.py covering stale entity resolver path (CACH-01 tool), search upsert (CACH-02), and invalid cursor error (TOOL-03)
- All 52 pre-existing tests continue to pass — zero regressions

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing stubs in test_cache.py** - `ba62f95` (test)
2. **Task 2: Write failing stubs in test_tools.py** - `e61ae24` (test)

## Files Created/Modified

- `tests/test_cache.py` - Appended `test_all_names_with_ttl_excludes_stale` and `test_all_names_with_ttl_user_vs_group_different_ttl`
- `tests/test_tools.py` - Appended `test_list_messages_stale_entity_excluded`, `test_search_messages_upserts_sender`, `test_list_messages_invalid_cursor_returns_error`

## Decisions Made

- Stub for stale entity uses `monkeypatch.setattr(mock_cache, "all_names_with_ttl", MagicMock(return_value={}))` on the instance rather than the class to avoid polluting other tests
- Upsert spy wraps real implementation (`MagicMock(wraps=mock_cache.upsert)`) so it captures calls while still delegating to actual SQLite — allows asserting call args without breaking cache state
- Invalid cursor test does not set up mock_client.iter_messages because cursor validation should short-circuit before any async client work

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All 5 stubs are red and ready for Plan 02 to implement: `all_names_with_ttl`, caller change in tools.py, `cache.upsert` in `search_messages`, and cursor decode error handling
- Exact assertion patterns are locked in — Plan 02 implementation must satisfy these exact contracts

---
*Phase: 05-cache-error-hardening*
*Completed: 2026-03-11*
