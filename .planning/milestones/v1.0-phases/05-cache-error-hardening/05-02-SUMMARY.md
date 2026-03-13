---
phase: 05-cache-error-hardening
plan: 02
subsystem: cache, tools
tags: [tdd, cache, ttl, cursor-error, search-upsert, entity-cache]

# Dependency graph
requires:
  - phase: 05-cache-error-hardening
    plan: 01
    provides: 5 failing TDD stubs for all_names_with_ttl, search upsert, cursor error
provides:
  - "EntityCache.all_names_with_ttl(user_ttl, group_ttl) with USER_TTL/GROUP_TTL constants"
  - "TTL-filtered name resolution in list_messages, search_messages, get_user_info"
  - "Sender entity upsert in search_messages after hits assembly"
  - "Invalid cursor error handling in list_messages returning TextContent"
affects: []

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Type-specific TTL SQL filter: WHERE (type='user' AND updated_at>=?) OR (type!='user' AND updated_at>=?)"
    - "try/except Exception as exc wrapping decode_cursor for graceful error TextContent"
    - "Sender upsert loop pattern (copy from list_messages) applied to search_messages"

key-files:
  created: []
  modified:
    - src/mcp_telegram/cache.py
    - src/mcp_telegram/tools.py

key-decisions:
  - "USER_TTL=2_592_000 (30 days), GROUP_TTL=604_800 (7 days) exported as module-level constants — allows override in tests"
  - "all_names() kept unchanged — existing callers (none active) unaffected; all_names_with_ttl is the new default for tools.py"
  - "Cursor error catch uses bare Exception (not ValueError) — catches binascii.Error, json.JSONDecodeError, and ValueError uniformly"

# Metrics
duration: 5min
completed: 2026-03-11
---

# Phase 5 Plan 02: Cache Error Hardening Implementation Summary

**TTL-filtered entity resolution and cursor error hardening — EntityCache.all_names_with_ttl, 4 call-site updates, search_messages sender upsert, and list_messages cursor try/except — all 57 tests green**

## Performance

- **Duration:** ~5 min
- **Completed:** 2026-03-11
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Added `USER_TTL=2_592_000` and `GROUP_TTL=604_800` module-level constants to `cache.py`
- Added `all_names_with_ttl(user_ttl, group_ttl)` method to `EntityCache` with type-specific SQL filter
- Updated `tools.py` import to include `USER_TTL` and `GROUP_TTL`
- Replaced all 4 `cache.all_names()` call sites with `cache.all_names_with_ttl(USER_TTL, GROUP_TTL)` in `list_messages`, `search_messages`, and `get_user_info`
- Wrapped `decode_cursor` call in `list_messages` with `try/except` returning `TextContent(text="Invalid cursor: ...")`
- Added sender upsert loop in `search_messages` after hits assembly (mirrors existing pattern in `list_messages`)
- All 57 tests pass (52 original + 5 stubs from Plan 01)

## Task Commits

Each task was committed atomically:

1. **Task 1: Add all_names_with_ttl to EntityCache with TTL constants** - `0779edc` (feat)
2. **Task 2: Update tools.py — TTL resolution, search upsert, cursor error** - `7a847c2` (feat)

## Files Created/Modified

- `src/mcp_telegram/cache.py` — Added `USER_TTL`, `GROUP_TTL` constants and `all_names_with_ttl()` method
- `src/mcp_telegram/tools.py` — Updated import, 4 call-site replacements, cursor try/except, search upsert loop

## Decisions Made

- `USER_TTL=2_592_000` (30 days) and `GROUP_TTL=604_800` (7 days) exported as module-level constants — allows per-call override and easy test verification
- `all_names()` kept unchanged — preserves backward compatibility for any future callers that want TTL-free access
- Cursor error handler uses bare `Exception` (not `ValueError`) — uniformly catches `binascii.Error`, `json.JSONDecodeError`, and `ValueError` from `decode_cursor`
- Sender upsert loop placed before Step 2 (context fetch) inside `async with` block — hits are populated before any secondary fetch, consistent with `list_messages` placement

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- CACH-01, CACH-02, and TOOL-03 requirements are fully satisfied
- Phase 5 (v1.0 milestone) is complete
- No pending tech debt from this plan

---
## Self-Check: PASSED

- src/mcp_telegram/cache.py: FOUND
- src/mcp_telegram/tools.py: FOUND
- .planning/phases/05-cache-error-hardening/05-02-SUMMARY.md: FOUND
- commit 0779edc: FOUND
- commit 7a847c2: FOUND

---
*Phase: 05-cache-error-hardening*
*Completed: 2026-03-11*
