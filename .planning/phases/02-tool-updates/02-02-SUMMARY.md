---
phase: 02-tool-updates
plan: "02"
subsystem: api
tags: [telethon, sqlite, xdg-base-dirs, entity-cache, tools]

# Dependency graph
requires:
  - phase: 01-support-modules
    provides: EntityCache SQLite module with upsert() and all_names() API
  - phase: 02-tool-updates
    plan: "01"
    provides: conftest fixtures (mock_cache, mock_client, make_mock_message, async_iter), test stubs
provides:
  - GetDialog and GetMessage tools removed from tools.py (CLNP-01, CLNP-02)
  - get_entity_cache() singleton in tools.py (functools_cache decorator, xdg_state_home path)
  - ListDialogs updated with type= (user/group/channel/unknown) and last_message_at= fields
  - ListDialogs upserts each dialog entity into EntityCache on every call
affects:
  - 02-03: ListMessages name resolution (uses get_entity_cache() pattern)
  - 02-04: SearchMessages (uses get_entity_cache() pattern)

# Tech tracking
tech-stack:
  added: [xdg-base-dirs (get_entity_cache path)]
  patterns:
    - "get_entity_cache() singleton via @functools_cache: single DB connection per process"
    - "ListDialogs as EntityCache warm-up: upsert on every iter_dialogs call"
    - "TDD: RED (stub tests replaced with assertions) → GREEN (implementation) per task"

key-files:
  created: []
  modified:
    - src/mcp_telegram/tools.py
    - tests/test_tools.py

key-decisions:
  - "get_entity_cache() creates state directory with mkdir(parents=True, exist_ok=True) before opening SQLite — required for first-run correctness"
  - "ListDialogs loses 'unread' field — unread filter moves to ListMessages where it belongs semantically"
  - "_async_iter helper defined at module level in test_tools.py (prefix underscore) to distinguish from conftest async_iter"

patterns-established:
  - "EntityCache singleton: @functools_cache def get_entity_cache() pattern for all tools that need entity lookup"
  - "Dialog type detection: is_user/is_group/is_channel branches with fallback to 'unknown'"

requirements-completed: [TOOL-01, CLNP-01, CLNP-02]

# Metrics
duration: 2min
completed: 2026-03-10
---

# Phase 2 Plan 02: ListDialogs Update + Tool Cleanup Summary

**GetDialog and GetMessage removed; ListDialogs updated with type/last_message_at fields and EntityCache warm-up; get_entity_cache() singleton established for all subsequent tools**

## Performance

- **Duration:** 2 min
- **Started:** 2026-03-10T23:07:20Z
- **Completed:** 2026-03-10T23:10:15Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Removed GetDialog and GetMessage tools (no BC obligations; they required integer IDs unavailable in name-based API)
- Added get_entity_cache() singleton using @functools_cache and xdg_state_home — shared EntityCache opened once per process
- Updated ListDialogs to emit type= (user/group/channel/unknown) and last_message_at= (ISO or "unknown") per dialog
- ListDialogs now upserts each dialog entity into EntityCache on every call (lazy cache warm-up)

## Task Commits

Each task was committed atomically:

1. **Task 1 RED: CLNP test stubs** - `8a43e31` (test)
2. **Task 1 GREEN: Remove GetDialog/GetMessage + add get_entity_cache()** - `8b7f0d5` (feat)
3. **Task 2 RED: ListDialogs type/last_message_at test stubs** - `b40cc64` (test)
4. **Task 2 GREEN: Update ListDialogs handler** - `25e1c14` (feat)

_Note: TDD tasks have separate RED/GREEN commits per protocol_

## Files Created/Modified
- `src/mcp_telegram/tools.py` - Removed GetDialog/GetMessage; added get_entity_cache() singleton; updated ListDialogs handler
- `tests/test_tools.py` - Implemented 4 test stubs: test_get_dialog_removed, test_get_message_removed, test_list_dialogs_type_field, test_list_dialogs_null_date

## Decisions Made
- get_entity_cache() adds mkdir(parents=True, exist_ok=True) before EntityCache(db_path): xdg_state_home() dir may not exist on first run, SQLite connect would fail without it (Rule 2 auto-fix)
- _async_iter helper added at module level in test_tools.py with underscore prefix to distinguish from conftest async_iter (same pattern, separate scope)
- ListDialogs drops the `unread` field: the filter semantically belongs in ListMessages; removing it simplifies the dialog-listing contract

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 2 - Missing Critical] Added directory creation before EntityCache DB open**
- **Found during:** Task 1 (implementing get_entity_cache())
- **Issue:** `sqlite3.connect()` raised `OperationalError: unable to open database file` when xdg_state_home() / "mcp-telegram" directory did not exist
- **Fix:** Added `db_dir.mkdir(parents=True, exist_ok=True)` before `EntityCache(db_path)` in get_entity_cache()
- **Files modified:** src/mcp_telegram/tools.py
- **Verification:** `uv run python -c "from mcp_telegram.tools import get_entity_cache; print(type(get_entity_cache()))"` returned EntityCache
- **Committed in:** 8b7f0d5 (Task 1 feat commit)

---

**Total deviations:** 1 auto-fixed (1 missing critical)
**Impact on plan:** Essential for first-run correctness. No scope creep.

## Issues Encountered
None beyond the auto-fixed directory creation.

## User Setup Required
None - no external service configuration required.

## Next Phase Readiness
- get_entity_cache() singleton established — Plans 02-03 (ListMessages) and 02-04 (SearchMessages) can import and reuse it
- EntityCache warm-up via ListDialogs means entity names will be available for name resolution after the first ListDialogs call
- 26 tests passing (22 original + 4 new TOOL-01/CLNP tests), 10 remaining stubs for Plans 02-03 and 02-04

---
*Phase: 02-tool-updates*
*Completed: 2026-03-10*
