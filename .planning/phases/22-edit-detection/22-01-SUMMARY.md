---
phase: 22-edit-detection
plan: 01
subsystem: database
tags: [sqlite, cache, versioning, formatter, tdd]

# Dependency graph
requires:
  - phase: 20-cache-foundation
    provides: message_versions table schema (DDL already present, now populated)
  - phase: 21-cache-first-reads
    provides: MessageCache class, store_messages() method, CachedMessage with edit_date field
provides:
  - MessageCache._record_versions_if_changed() — writes old text to message_versions on text change
  - store_messages() calls versioning helper before INSERT OR REPLACE (same transaction)
  - format_messages() appends [edited HH:mm] marker for messages with truthy edit_date
affects: [formatter, cache, reading, capability_history]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "Batch version detection: single SELECT IN query to check all incoming messages at once before writing"
    - "Application-level versioning: detect text change in Python, insert old row to message_versions before overwrite"
    - "edit_date polymorphism: formatter handles both int (CachedMessage) and datetime (Telethon) via isinstance guard"

key-files:
  created: []
  modified:
    - src/mcp_telegram/cache.py
    - src/mcp_telegram/formatter.py
    - tests/test_cache.py
    - tests/test_formatter.py

key-decisions:
  - "Version write and cache INSERT OR REPLACE share a single transaction — _record_versions_if_changed writes before executemany, commit() covers both"
  - "Batch SELECT IN for existing rows and batch SELECT MAX(version) for version numbers — avoids N+1 per message"
  - "Only text change triggers versioning — edit_date changes alone (without text change) do not produce a version row"
  - "cast() used in incoming_for_version to satisfy mypy — rows list is tuple[object,...], cast makes types explicit"

patterns-established:
  - "Application-level versioning: read current state → diff → write old state to versions table → overwrite"
  - "edit_date int/datetime polymorphism via isinstance(edit_date_raw, datetime) in formatter"

requirements-completed: [EDIT-01, EDIT-02, EDIT-03]

# Metrics
duration: 3min
completed: 2026-03-19
---

# Phase 22 Plan 01: Edit Detection Summary

**Application-level message versioning via _record_versions_if_changed() and [edited HH:mm] marker in format_messages() using SQLite message_versions table**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-19T21:26:57Z
- **Completed:** 2026-03-19T21:30:00Z
- **Tasks:** 2 (TDD: RED + GREEN)
- **Files modified:** 4

## Accomplishments

- MessageCache._record_versions_if_changed() writes old text (and edit_date) to message_versions when text changes; skips first-time stores and unchanged text
- store_messages() calls versioning helper before INSERT OR REPLACE within the same transaction — no extra commit needed
- format_messages() appends [edited HH:mm] after message text and before reactions for any message with truthy edit_date (int or datetime)
- 9 new tests (5 cache versioning, 4 formatter marker), all passing; 339 total tests pass; mypy zero errors

## Task Commits

Each task was committed atomically:

1. **Task 1: RED — Write failing tests** - `fdecf52` (test)
2. **Task 2: GREEN — Implement versioning and edited marker** - `8af182a` (feat)

**Plan metadata:** _(docs commit below)_

_Note: TDD tasks have separate RED and GREEN commits_

## Files Created/Modified

- `src/mcp_telegram/cache.py` — Added _record_versions_if_changed() method and call site in store_messages()
- `src/mcp_telegram/formatter.py` — Added datetime/timezone import and [edited HH:mm] marker logic
- `tests/test_cache.py` — 5 new version recording tests under "Edit detection versioning tests (Phase 22)"
- `tests/test_formatter.py` — Added edit_date field to MockMessage; 4 new edited marker tests

## Decisions Made

- Version write and cache INSERT OR REPLACE share a single transaction — _record_versions_if_changed inserts before executemany, commit() at end covers both. No extra intermediate commits.
- Only text change triggers versioning — edit_date-only changes are not versioned (aligns with EDIT-02 spec: "old text preserved").
- Batch detection: SELECT IN for all incoming message_ids at once, then SELECT MAX(version) IN for changed ids only — O(1) round trips per store_messages call regardless of batch size.
- cast() wrapping for incoming_for_version list — satisfies mypy strict typing on tuple[object,...] rows.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed incorrect timestamp in test_edited_marker_shown_when_edit_date_is_int**
- **Found during:** Task 2 (GREEN — running tests after implementation)
- **Issue:** Plan specified timestamp `1718462400` should map to `15:20` UTC, but it actually maps to `14:40` UTC. Assertion `[edited 15:20]` failed.
- **Fix:** Changed timestamp to `1718464800` which correctly maps to `2024-06-15 15:20:00 UTC`.
- **Files modified:** tests/test_formatter.py
- **Verification:** Test passes with corrected timestamp; datetime verified via Python REPL
- **Committed in:** `8af182a` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 — bug in plan's test spec)
**Impact on plan:** Minor — only affected test data, not implementation logic. Behavior spec and implementation are correct.

## Issues Encountered

None beyond the timestamp bug documented above.

## Next Phase Readiness

- EDIT-01, EDIT-02, EDIT-03 requirements fulfilled
- message_versions table is now populated; GetMessageVersions or similar tool can be added in a follow-on phase if needed
- edit_date surfaced in formatted output — LLMs reading history can now distinguish original from edited messages

---
*Phase: 22-edit-detection*
*Completed: 2026-03-19*

## Self-Check: PASSED

- src/mcp_telegram/cache.py — FOUND
- src/mcp_telegram/formatter.py — FOUND
- tests/test_cache.py — FOUND
- tests/test_formatter.py — FOUND
- .planning/phases/22-edit-detection/22-01-SUMMARY.md — FOUND
- commit fdecf52 (RED) — FOUND
- commit 8af182a (GREEN) — FOUND
