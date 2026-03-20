---
phase: 21-cache-first-reads-bypass-rules
plan: "01"
subsystem: cache
tags: [cache, sqlite, tdd, bypass-rules]
dependency_graph:
  requires: [Phase 20 message_cache schema]
  provides: [MessageCache.store_messages, MessageCache.try_read_page, _should_try_cache]
  affects: [cache.py, tests/test_cache.py]
tech_stack:
  added: []
  patterns: [INSERT OR REPLACE batch, coverage-based cache miss, topic-aware WHERE clause]
key_files:
  created: []
  modified:
    - src/mcp_telegram/cache.py
    - tests/test_cache.py
decisions:
  - "HistoryDirection imported via TYPE_CHECKING at module level + runtime import inside try_read_page to avoid any potential circular import"
  - "media_description uses type(msg.media).__name__ when media is present — avoids calling .to_dict() on live objects"
  - "forum_topic_id=1 sentinel for General topic (reply_to_top_id=None but forum_topic=True)"
  - "try_read_page returns None when len(rows) < limit — strict partial coverage detection"
  - "PRAGMA optimize placed before conn.commit() in _bootstrap_cache_schema"
metrics:
  duration_minutes: 2
  completed_date: "2026-03-20"
  tasks_completed: 2
  files_modified: 2
---

# Phase 21 Plan 01: MessageCache Data-Access Layer Summary

**One-liner:** SQLite MessageCache with store/read round-trip, topic-aware coverage detection, and bypass rules (_should_try_cache) for BYP-01/BYP-02.

## What Was Built

`MessageCache` class in `cache.py` — a pure data-access layer for the `message_cache` table:

- `store_messages(dialog_id, messages)` — extracts all 11 structured fields from Telethon message objects via `getattr` with safe fallbacks. Uses `executemany` + `INSERT OR REPLACE` for upsert semantics.
- `try_read_page(dialog_id, *, topic_id, anchor_id, limit, direction)` — builds a topic-aware WHERE clause, queries with direction-appropriate ORDER BY, and returns `None` on partial coverage (fewer rows than `limit`).
- `_should_try_cache(navigation, *, unread)` — module-level bypass function encoding BYP-01 (newest/None = always live) and BYP-02 (unread = always live).
- `PRAGMA optimize` added to `_bootstrap_cache_schema()` just before `conn.commit()`.

14 new tests added in `tests/test_cache.py` under the `# MessageCache data-access tests (Phase 21, Plan 01)` section.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | RED — Write failing tests | 54d3431 | tests/test_cache.py |
| 2 | GREEN — Implement MessageCache | 3eb1870 | src/mcp_telegram/cache.py |

## Success Criteria

- [x] `MessageCache.store_messages()` correctly extracts all 11 fields from Telethon message objects
- [x] `MessageCache.try_read_page()` returns CachedMessage list on full coverage, None on partial
- [x] Topic-aware queries isolate messages by forum_topic_id (NULL isolation included)
- [x] `_should_try_cache()` encodes bypass rules for BYP-01 and BYP-02
- [x] PRAGMA optimize runs during bootstrap
- [x] All 321 tests pass, mypy zero errors on cache.py

## Deviations from Plan

None — plan executed exactly as written.

## Self-Check: PASSED
