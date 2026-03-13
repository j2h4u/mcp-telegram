---
phase: 02-tool-updates
plan: "03"
subsystem: api
tags: [telethon, pydantic, mcp, pytest, rapidfuzz, pagination]

# Dependency graph
requires:
  - phase: 02-tool-updates-02
    provides: ListDialogs rewrite with EntityCache warm-up, resolver/formatter/pagination modules
  - phase: 01-support-modules-04
    provides: EntityCache with all_names(), resolver.resolve(), encode_cursor/decode_cursor, format_messages()
provides:
  - ListMessages rewritten with dialog: str name resolution via EntityCache + rapidfuzz resolver
  - Cursor-based pagination (encode_cursor/decode_cursor) replacing before_id: int
  - Sender filter via from_user= kwarg to iter_messages
  - Unread filter via GetPeerDialogsRequest reading read_inbox_max_id
  - 7 new ListMessages tests covering TOOL-02 through TOOL-05
affects: [02-04-SearchMessages, future tool development]

# Tech tracking
tech-stack:
  added: [telethon.tl.functions.messages.GetPeerDialogsRequest]
  patterns:
    - Name resolution pattern: resolve(name, cache.all_names()) -> Resolved | Candidates | NotFound
    - Cursor pagination pattern: encode/decode cursor around iter_messages, next_cursor appended to output text
    - Lazy cache upsert: sender entities populated from message payloads during fetch

key-files:
  created: []
  modified:
    - src/mcp_telegram/tools.py
    - tests/test_tools.py

key-decisions:
  - "Use mock_client.return_value (not __call__ override) for AsyncMock call stubbing in unread filter test"
  - "Resolve both dialog and sender names before entering async context to fail fast without opening client"
  - "next_cursor appended as plain text suffix to formatted output (not a separate TextContent item)"

patterns-established:
  - "Name resolution before client open: resolve dialog/sender first, return error TextContent on NotFound/Candidates"
  - "Lazy cache population: upsert sender entities from message payloads inside fetch loop"

requirements-completed: [TOOL-02, TOOL-03, TOOL-04, TOOL-05]

# Metrics
duration: 10min
completed: 2026-03-11
---

# Phase 2 Plan 03: ListMessages Rewrite Summary

**ListMessages rewritten to accept dialog name string with fuzzy resolution, cursor pagination, sender/unread filters, and format_messages() output — 7 new tests green (TOOL-02 through TOOL-05)**

## Performance

- **Duration:** ~10 min
- **Started:** 2026-03-10T23:05:00Z
- **Completed:** 2026-03-10T23:15:17Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments
- Replaced `dialog_id: int` with `dialog: str` in ListMessages; removed `before_id`, added `cursor`, `sender`, `unread`
- Added `GetPeerDialogsRequest` import and unread filter logic (reads `read_inbox_max_id` from raw TL dialog)
- Imported `format_messages`, `encode_cursor`/`decode_cursor`, `resolve`/`NotFound`/`Candidates` into tools.py
- Implemented 7 new ListMessages tests: by_name, not_found, ambiguous, cursor_present, no_cursor_last_page, sender_filter, unread_filter
- Test suite: 33 passed, 3 failed (only SearchMessages stubs remain)

## Task Commits

1. **Task 1: Rewrite ListMessages class and core name resolution + formatting** - `1dae03e` (feat)
2. **Task 2: Implement cursor pagination and sender/unread filter tests** - `61a9ed8` (test)

## Files Created/Modified
- `src/mcp_telegram/tools.py` - ListMessages class rewritten; new imports; handler fully replaced
- `tests/test_tools.py` - 7 stub tests replaced with real implementations

## Decisions Made
- Used `mock_client.return_value = peer_result` instead of `mock_client.__call__ = AsyncMock(...)` for the unread filter test — `AsyncMock.__call__` override doesn't propagate through `__aenter__` context manager chain correctly; `return_value` is the correct approach
- Dialog and sender names resolved before opening async client context — fail-fast without creating a Telethon connection
- `next_cursor` appended as plain text suffix (`"\n\nnext_cursor: {token}"`) to keep output as single TextContent

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed unread filter test mock setup**
- **Found during:** Task 2 (unread filter test)
- **Issue:** Plan used `mock_client.__call__ = AsyncMock(return_value=peer_result)` which doesn't work with AsyncMock — `tl_dialog.unread_count` remained a MagicMock causing `TypeError: '<' not supported between instances of 'int' and 'MagicMock'`
- **Fix:** Changed to `mock_client.return_value = peer_result` (correct AsyncMock API)
- **Files modified:** tests/test_tools.py
- **Verification:** test_list_messages_unread_filter passes
- **Committed in:** 61a9ed8 (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (1 bug in plan's test code)
**Impact on plan:** Minor fix to test mock approach. No scope creep.

## Issues Encountered
- AsyncMock `__call__` override does not work as expected when mock is used as context manager — standard Python mock behavior, fixed inline.

## Next Phase Readiness
- ListMessages fully complete; TOOL-02 through TOOL-05 satisfied
- SearchMessages stub tests (TOOL-06, TOOL-07) remain — ready for plan 02-04
- No blockers

---
*Phase: 02-tool-updates*
*Completed: 2026-03-11*
