---
phase: "21"
plan: "02"
subsystem: cache
tags: [cache, capability, history, search, tdd]
dependency_graph:
  requires: ["21-01"]
  provides: [cache-first-history-reads, search-cache-population, reply-map-cache-lookup]
  affects: [capability_history, capability_search, message_ops]
tech_stack:
  added: []
  patterns: [cache-first-read, bypass-rules, tdd-red-green]
key_files:
  created:
    - tests/test_capability_history.py
    - tests/test_capability_search.py
  modified:
    - src/mcp_telegram/capability_history.py
    - src/mcp_telegram/capability_search.py
    - src/mcp_telegram/message_ops.py
decisions:
  - "min_id=1 sentinel for OLDEST first page treated as anchor_id=None so message_id 1 is included in coverage check"
  - "cached_page typed as list[MessageLike] with type: ignore[assignment] — CachedMessage satisfies Protocol at runtime but frozen dataclass conflicts with Protocol's settable-attribute assumption"
  - "cast('MessageLike', CachedMessage) used in _build_reply_map result dict to satisfy mypy"
metrics:
  duration: "~35 minutes"
  completed: "2026-03-19T21:00:39Z"
  tasks: 2
  files_modified: 5
---

# Phase 21 Plan 02: Cache-First Reads and Bypass Rules Wiring Summary

Cache integration wired end-to-end: page 2+ ListMessages reads served from SQLite MessageCache when coverage exists, all Telegram API fetches populate the cache, bypass rules enforced (BYP-01/02/04), and reply-map lookups try cache before API.

## Tasks Completed

| Task | Type | Description | Commit |
|------|------|-------------|--------|
| 1 | RED | Write failing integration tests for cache-first reads and bypass rules | 7f4a2ee |
| 2 | GREEN | Wire MessageCache into capability_history, capability_search, reply map | a843a15 |

## What Was Built

### capability_history.py

After `iter_kwargs` is fully built (including unread `min_id`), a cache-first block runs:

1. `msg_cache = MessageCache(cache._conn)` — shares the existing EntityCache connection
2. Determine `cache_direction` and `cache_anchor_id` from `iter_kwargs` keys (`reverse`, `max_id`, `min_id`)
3. Special case: `min_id=1` sentinel (OLDEST first page, no cursor) treated as `anchor_id=None`
4. `_should_try_cache(navigation, unread=unread)` — returns False for BYP-01/02, True otherwise
5. If cache returns a full page: skip both fetch paths entirely
6. If cache miss: run existing fetch logic unchanged, then `msg_cache.store_messages()` after

The reply-map call now passes `msg_cache=msg_cache` so replied-to messages are looked up in cache before hitting the API.

### capability_search.py

After `_cache_message_senders()`, two lines added:

```python
msg_cache = MessageCache(cache._conn)
msg_cache.store_messages(entity_id, hits)
```

Search always hits API (BYP-04 — no cache read path added). Results populate cache for future ListMessages hits.

### message_ops.py

`_build_reply_map` gains optional `msg_cache: MessageCache | None = None`. When provided, iterates reply IDs and queries `message_cache` directly (single-row SELECT per ID). Uncached IDs fall back to `client.get_messages()`. Result uses `cast("MessageLike", CachedMessage...)` for mypy.

## Integration Tests (9 tests)

| Test | What it verifies |
|------|-----------------|
| `test_history_cache_hit_skips_api` | Full cache coverage → zero API calls |
| `test_history_cache_miss_falls_through_to_api` | Partial cache → API called |
| `test_history_cache_miss_populates_cache` | API result written to MessageCache |
| `test_history_newest_bypasses_cache` | BYP-01: navigation=None always hits API |
| `test_history_unread_bypasses_cache` | BYP-02: unread=True always hits API |
| `test_history_oldest_first_page_uses_cache` | 'oldest' navigation tried from cache |
| `test_history_always_populates_cache_on_api_fetch` | CACHE-05: bypassed fetch still stores |
| `test_search_always_hits_api` | BYP-04: search always calls API |
| `test_search_populates_cache_after_fetch` | Search results stored in MessageCache |

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] min_id=1 sentinel excluded message ID 1 from oldest cache lookup**
- **Found during:** Task 2 (GREEN) — `test_history_oldest_first_page_uses_cache` failed
- **Issue:** `_build_history_iter_kwargs` sets `min_id=1` as "from beginning" sentinel for OLDEST first page. The cache query translates this to `message_id > 1`, excluding the oldest message (ID 1).
- **Fix:** Added guard: `min_id > 1` sets `cache_anchor_id`, otherwise `None` (no anchor filter)
- **Files modified:** `src/mcp_telegram/capability_history.py`
- **Commit:** a843a15

**2. [Rule 2 - Missing] MessageLike not imported in capability_history.py**
- **Found during:** Task 2 (GREEN) — mypy error `Name "MessageLike" is not defined`
- **Fix:** Added `MessageLike` to the models import block
- **Files modified:** `src/mcp_telegram/capability_history.py`
- **Commit:** a843a15

## Verification Results

```
330 passed (321 pre-existing + 9 new integration tests)
mypy: 0 errors on all modified files
```

## Self-Check: PASSED

Files exist:
- tests/test_capability_history.py: FOUND
- tests/test_capability_search.py: FOUND
- src/mcp_telegram/capability_history.py: FOUND (contains MessageCache, _should_try_cache, store_messages)
- src/mcp_telegram/capability_search.py: FOUND (contains msg_cache.store_messages)
- src/mcp_telegram/message_ops.py: FOUND (contains msg_cache: MessageCache | None = None)

Commits:
- 7f4a2ee: test(21-02): RED phase — FOUND
- a843a15: feat(21-02): GREEN phase — FOUND
