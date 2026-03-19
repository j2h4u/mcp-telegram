---
phase: 20-cache-foundation
plan: "01"
subsystem: database
tags: [sqlite, cache, schema, tdd, bootstrap]

requires:
  - phase: 19-dialog-metadata-enrichment
    provides: EntityCache bootstrap pattern and entity_cache.db as shared DB file

provides:
  - message_cache SQLite table (11 columns, WITHOUT ROWID, composite PK)
  - message_versions SQLite table (5 columns, WITHOUT ROWID, 3-part PK)
  - idx_message_cache_dialog_sent index on (dialog_id, sent_at DESC)
  - Extended _database_bootstrap_required with 3 new guards
  - Extended _bootstrap_cache_schema with 3 new DDL executions
  - 9 schema-verification tests in tests/test_cache.py

affects:
  - 20-02 (CachedMessage proxy — consumes message_cache schema)
  - 21-cache-reads-writes (populates and reads message_cache)
  - 22-edit-detection (populates message_versions)

tech-stack:
  added: []
  patterns:
    - "DDL constant at module level + bootstrap guard + bootstrap execution (existing pattern extended)"
    - "WITHOUT ROWID tables with composite PKs for cache tables"

key-files:
  created: []
  modified:
    - src/mcp_telegram/cache.py
    - tests/test_cache.py

key-decisions:
  - "WITHOUT ROWID chosen for message_cache and message_versions — composite PK always known on lookup, eliminates secondary B-tree"
  - "message_versions schema-only (no populate logic) — Phase 22 writes to it; schema-first keeps bootstrap idempotent now"
  - "Bootstrap extended in-place via existing pattern — no new mechanisms, no separate DB file"

patterns-established:
  - "TDD RED-GREEN for cache schema: write sqlite_master + PRAGMA table_info assertions first, then add DDL"

requirements-completed: [CACHE-01, CACHE-07]

duration: 1min
completed: "2026-03-19"
---

# Phase 20 Plan 01: Cache Foundation — Schema Summary

**message_cache (11-col WITHOUT ROWID) and message_versions (5-col WITHOUT ROWID) tables added to entity_cache.db bootstrap via 3 new DDL constants, 3 bootstrap guards, and 9 schema-verification tests**

## Performance

- **Duration:** ~1 min
- **Started:** 2026-03-19T20:06:38Z
- **Completed:** 2026-03-19T20:08:00Z
- **Tasks:** 2 (TDD RED + GREEN)
- **Files modified:** 2

## Accomplishments

- Added `message_cache` table (dialog_id, message_id PK WITHOUT ROWID, 9 additional fields) to EntityCache bootstrap
- Added `message_versions` table (dialog_id, message_id, version PK WITHOUT ROWID, old_text, edit_date) — schema-only foundation for Phase 22
- Added `idx_message_cache_dialog_sent` covering index on (dialog_id, sent_at DESC) for efficient range reads
- 9 schema-verification tests covering table existence, column schema, PK constraints, WITHOUT ROWID NOT NULL enforcement, index existence, same-DB co-location, and backward compatibility

## Task Commits

Each task was committed atomically:

1. **Task 1: Write failing tests for message_cache and message_versions schema** - `65ecb46` (test)
2. **Task 2: Add DDL constants and extend bootstrap** - `19c5ff2` (feat)

_Note: TDD tasks have two commits (test RED → feat GREEN)_

## Files Created/Modified

- `src/mcp_telegram/cache.py` — Added `_MESSAGE_CACHE_TABLE_DDL`, `_MESSAGE_CACHE_INDEX_DDL`, `_MESSAGE_VERSIONS_TABLE_DDL` constants; extended `_database_bootstrap_required` with 3 guards; extended `_bootstrap_cache_schema` with 3 DDL executions; added new tables to `_ALLOWED_TABLE_NAMES`
- `tests/test_cache.py` — Added 9 new tests: `test_message_cache_table_exists`, `test_message_cache_schema`, `test_message_cache_pk_constraint`, `test_message_cache_without_rowid`, `test_message_cache_index_exists`, `test_message_versions_table_exists`, `test_message_versions_schema`, `test_message_cache_same_db_as_entities`, `test_existing_entity_cache_still_works_after_bootstrap`

## Decisions Made

- WITHOUT ROWID for both tables — composite PK always known on lookup, no hidden rowid needed, range scans via covering index are efficient
- message_versions is schema-only for now — Phase 22 (edit detection) populates it; bootstrapping the schema early keeps the guard idempotent once Phase 22 ships
- Followed existing DDL-constant + bootstrap-guard + bootstrap-execute pattern exactly — no new mechanisms introduced

## Deviations from Plan

None — plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None — no external service configuration required.

## Next Phase Readiness

- message_cache and message_versions tables created on every fresh EntityCache init
- Bootstrap is idempotent — re-opening an existing DB is a no-op
- 299 tests passing, mypy zero errors
- Ready for Plan 20-02: CachedMessage proxy class (MessageLike-compatible dataclass)

---
*Phase: 20-cache-foundation*
*Completed: 2026-03-19*
