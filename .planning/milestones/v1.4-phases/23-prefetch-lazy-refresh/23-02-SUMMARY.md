---
phase: 23-prefetch-lazy-refresh
plan: "02"
subsystem: capability_history + tools layer
tags: [prefetch, integration, tdd, singleton, background-tasks]
dependency_graph:
  requires: [23-01]
  provides: [prefetch-wired-to-reads]
  affects: [capability_history, tools/reading, tools/_base]
tech_stack:
  added: []
  patterns: [functools_cache singleton, TYPE_CHECKING import guard, list[object] cast for mypy invariance]
key_files:
  created: []
  modified:
    - src/mcp_telegram/capability_history.py
    - src/mcp_telegram/tools/_base.py
    - src/mcp_telegram/tools/reading.py
    - tests/test_capability_history.py
decisions:
  - "list[MessageLike] cast to list[object] via intermediate variable for _next_prefetch_anchor — list is invariant in mypy, Sequence would be covariant but _next_prefetch_anchor signature uses list[object]"
  - "Test expectations adjusted for REF-01 co-firing on cache hits — plan described collapse scenarios in isolation but cache-hit + prefetch both fire in the same call path"
metrics:
  duration_seconds: 261
  completed_date: "2026-03-20"
  tasks_completed: 2
  tasks_total: 2
  files_modified: 4
  tests_added: 8
  tests_total: 371
---

# Phase 23 Plan 02: Prefetch Coordinator Integration Summary

**One-liner:** Prefetch scheduling wired into capability_history via `_schedule_prefetch_tasks` + per-process `PrefetchCoordinator` singleton in tools layer.

## What Was Built

Connected the standalone `PrefetchCoordinator` (Plan 01) to the actual read path so PRE-01/02/03 and REF-01 work end-to-end.

### capability_history.py changes

- Added `prefetch_coordinator: PrefetchCoordinator | None = None` parameter to `execute_history_read_capability`
- Added `_schedule_prefetch_tasks` function implementing all prefetch trigger rules:
  - **PRE-01**: `navigation=None` or `"newest"` schedules next NEWEST page (anchor=min ids) + oldest page (anchor=None)
  - **PRE-02**: `navigation=base64 token` schedules next page in token direction only
  - **PRE-03**: `navigation="oldest"` schedules next OLDEST page forward (anchor=max ids); no redundant oldest-page task since direction already matches
  - **REF-01**: `cached_page is not None` schedules delta refresh (last_id=max cached ids)
  - **Guards**: `unread=True` and empty messages return early without scheduling anything
- Call site added at end of function, just before `return HistoryReadExecution(...)` — non-blocking

### tools/_base.py changes

- Added `get_prefetch_coordinator()` singleton factory using `@functools_cache` — same pattern as `get_entity_cache()`
- Lazy import of `PrefetchCoordinator` inside function body to avoid circular import

### tools/reading.py changes

- Added `get_prefetch_coordinator` to the `_base` import line
- Passed `prefetch_coordinator=get_prefetch_coordinator()` to `execute_history_read_capability` in `list_messages`

## Tests Added (TDD)

8 new tests in `tests/test_capability_history.py`:

| Test | Requirement | Verifies |
|------|-------------|---------|
| `test_first_page_schedules_dual_prefetch` | PRE-01 | navigation=None → 2 schedule calls (next NEWEST + oldest) |
| `test_first_page_oldest_schedules_next_oldest` | PRE-01+PRE-03 | navigation="oldest" → next OLDEST key present, no redundant anchor=None |
| `test_subsequent_page_schedules_next_prefetch` | PRE-02 | base64 token → next-page key present, no dual oldest task |
| `test_cache_hit_triggers_delta_refresh` | REF-01 | cached_page not None → delta key (entity_id, "delta", max_id, None) scheduled |
| `test_unread_skips_all_prefetch` | guard | unread=True → schedule never called |
| `test_empty_messages_no_prefetch` | guard | empty API result → no schedule calls |
| `test_prefetch_coordinator_none_no_error` | safety | prefetch_coordinator=None → no AttributeError |
| `test_prefetch_does_not_block_response` | non-blocking | HistoryReadExecution returned even when schedule fires |

## Verification

- `uv run pytest tests/test_prefetch.py tests/test_capability_history.py`: 39 passed
- `uv run pytest tests/`: 371 passed, 0 failures
- `uv run mypy src/mcp_telegram/prefetch.py src/mcp_telegram/capability_history.py src/mcp_telegram/tools/_base.py src/mcp_telegram/tools/reading.py --no-error-summary`: zero errors

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Test expectations adjusted for REF-01 co-firing with prefetch**
- **Found during:** Task 1 GREEN phase
- **Issue:** Plan described `test_first_page_oldest_schedules_next_oldest` and `test_subsequent_page_schedules_next_prefetch` with `call_count == 1`, but both test scenarios involve a cache hit (pre-seeded cache), which triggers REF-01 delta refresh alongside the prefetch task, making `call_count == 2`
- **Fix:** Rewrote assertions from `call_count == N` to key-presence checks — verified specific keys are present and unwanted keys (e.g., redundant `oldest, None, None`) are absent
- **Files modified:** `tests/test_capability_history.py`
- **Commit:** af46e62 (RED), bd414ac (GREEN)

**2. [Rule 1 - Bug] mypy invariance error on list[MessageLike] vs list[object]**
- **Found during:** Task 2 mypy check
- **Issue:** `_next_prefetch_anchor` expects `list[object]` but `_schedule_prefetch_tasks` passes `list[MessageLike]` — mypy rejects this because `list` is invariant
- **Fix:** Added `messages_as_objects: list[object] = list(messages)` intermediate variable with comment explaining the cast
- **Files modified:** `src/mcp_telegram/capability_history.py`
- **Commit:** 2117300

## Commits

| Hash | Type | Description |
|------|------|-------------|
| af46e62 | test | RED: 8 failing tests for prefetch scheduling |
| bd414ac | feat | GREEN: _schedule_prefetch_tasks + prefetch_coordinator param |
| 2117300 | feat | Task 2: get_prefetch_coordinator singleton + tools wiring |

## Self-Check: PASSED
