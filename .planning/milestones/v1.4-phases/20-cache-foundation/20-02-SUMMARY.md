---
phase: 20-cache-foundation
plan: "02"
subsystem: database
tags: [sqlite, cache, dataclass, protocol, tdd, mypy]

requires:
  - phase: 20-01
    provides: message_cache DDL with 11-column schema (dialog_id, message_id, sent_at, text, sender_id, sender_first_name, media_description, reply_to_msg_id, forum_topic_id, edit_date, fetched_at)

provides:
  - CachedMessage frozen dataclass in cache.py satisfying MessageLike Protocol
  - _CachedSender frozen dataclass satisfying SenderLike Protocol
  - _CachedReplyHeader frozen dataclass satisfying ReplyHeaderLike Protocol
  - CachedMessage.from_row() classmethod for constructing from SQLite SELECT rows
  - 7 round-trip and edge case tests in test_cache.py
  - 1 formatter transparency smoke test in test_formatter.py

affects: [21-cache-reads, 22-cache-writes, formatter, capability_history]

tech-stack:
  added: []
  patterns:
    - "Frozen dataclass proxy pattern for MessageLike Protocol satisfaction"
    - "from_row() classmethod unpacks SQLite rows by positional tuple index"
    - "tuple[object, ...] + cast() for mypy-clean SQLite row unpacking"

key-files:
  created: []
  modified:
    - src/mcp_telegram/cache.py
    - tests/test_cache.py
    - tests/test_formatter.py

key-decisions:
  - "tuple[object, ...] parameter type for from_row() — SQLite rows are mixed-type; cast() at use sites satisfies mypy without unsafe ignores"
  - "media_description used as .message fallback when text is None — preserves formatter readability for media-only messages"
  - "edit_date stored as int | None (Unix timestamp) — Phase 22 will convert to [edited] marker in formatter"
  - "reactions=None, media=None always — cached data doesn't include reaction counts or media blobs; formatter gracefully handles None"

patterns-established:
  - "Protocol satisfaction via structural subtyping: frozen dataclass with matching field names/types is sufficient — no explicit Protocol inheritance needed"
  - "Formatter transparency: format_messages([CachedMessage(...)], {}) works without modification — read-side cache path reuses existing formatter"

requirements-completed: [CACHE-02]

duration: 3min
completed: "2026-03-20"
---

# Phase 20 Plan 02: CachedMessage Proxy Summary

**CachedMessage frozen dataclass proxy satisfying MessageLike Protocol via structural subtyping, with from_row() classmethod mapping SQLite row positions to typed fields**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-19T20:10:58Z
- **Completed:** 2026-03-19T20:14:00Z
- **Tasks:** 2 (TDD: RED then GREEN)
- **Files modified:** 3

## Accomplishments

- CachedMessage frozen dataclass with all 7 MessageLike fields (.id, .date, .message, .sender, .reply_to, .reactions, .media) plus .edit_date for Phase 22
- _CachedSender and _CachedReplyHeader nested stubs satisfying SenderLike and ReplyHeaderLike Protocols
- from_row() classmethod unpacks 11-column message_cache rows by position with media_description fallback
- 307 tests passing, mypy zero errors — formatter accepts CachedMessage transparently without any modifications to formatter.py

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing tests (RED)** - `ec5851a` (test)
2. **Task 2: Implement CachedMessage proxy (GREEN)** - `328666c` (feat)

## Files Created/Modified

- `src/mcp_telegram/cache.py` - Added _CachedSender, _CachedReplyHeader, CachedMessage frozen dataclasses with from_row() classmethod
- `tests/test_cache.py` - 7 new tests: from_row basic, with reply, media fallback, no sender, date timezone, edit_date, frozen
- `tests/test_formatter.py` - 1 new formatter transparency smoke test (CACHE-02)

## Decisions Made

- Used `tuple[object, ...]` as from_row() parameter type (not `tuple[int, ...]`) since SQLite rows contain mixed types (int, str, None). Applied `cast()` at use sites for mypy satisfaction.
- Kept reactions and media always None on CachedMessage — cache doesn't store reaction counts or media blobs; media_description folds into .message instead.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed mypy errors in from_row() due to wrong tuple type annotation**
- **Found during:** Task 2 (implement CachedMessage)
- **Issue:** Initial `tuple[int, ...]` annotation caused mypy errors — SQLite rows contain mixed types (int, str, None), so `message` and `sender_first_name` were typed as `int` at the call sites
- **Fix:** Changed parameter to `tuple[object, ...]` and added `cast()` at each use site for the fields fed into the constructor
- **Files modified:** src/mcp_telegram/cache.py
- **Verification:** `uv run mypy src/` reports zero errors
- **Committed in:** 328666c (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - Bug)
**Impact on plan:** Necessary for mypy zero-error requirement. No scope creep.

## Issues Encountered

None beyond the mypy type annotation fix documented above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- CachedMessage is ready for Phase 21 (cache-first history reads) to use as the read-side proxy
- format_messages([CachedMessage(...)], {}) verified working — no formatter changes needed in Phase 21
- edit_date field on CachedMessage ready for Phase 22 formatter [edited] marker feature

---
*Phase: 20-cache-foundation*
*Completed: 2026-03-20*

## Self-Check: PASSED

- FOUND: src/mcp_telegram/cache.py (CachedMessage, _CachedSender, _CachedReplyHeader)
- FOUND: tests/test_cache.py (7 CachedMessage tests)
- FOUND: tests/test_formatter.py (1 transparency test)
- FOUND: .planning/phases/20-cache-foundation/20-02-SUMMARY.md
- FOUND: commit ec5851a (test RED phase)
- FOUND: commit 328666c (feat GREEN phase)
