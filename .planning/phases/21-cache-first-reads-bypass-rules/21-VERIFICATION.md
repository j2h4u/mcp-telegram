---
phase: 21-cache-first-reads-bypass-rules
verified: 2026-03-20T00:00:00Z
status: passed
score: 6/6 must-haves verified
---

# Phase 21: Cache-First Reads and Bypass Rules — Verification Report

**Phase Goal:** History reads serve pages 2+ from cache when available; bypass rules ensure live data where required
**Verified:** 2026-03-20
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| #  | Truth                                                                                                      | Status     | Evidence                                                                                                                  |
|----|------------------------------------------------------------------------------------------------------------|------------|---------------------------------------------------------------------------------------------------------------------------|
| 1  | Page 2+ of ListMessages is served from cache when the range is covered — no Telegram API call              | VERIFIED   | `capability_history.py:176-183` calls `msg_cache.try_read_page()`; `if cached_page is not None: raw_messages = cached_page` at line 185-186 skips `client.iter_messages`. `test_history_cache_hit_skips_api` asserts zero API calls. |
| 2  | `navigation="newest"` (first page) always fetches from Telegram API — never served stale                   | VERIFIED   | `_should_try_cache()` in `cache.py:504` returns `False` when `navigation is None or navigation == "newest"`. `test_history_newest_bypasses_cache` passes. |
| 3  | `unread=True` in ListMessages always fetches live regardless of cache state                                | VERIFIED   | `_should_try_cache()` at `cache.py:502-503` returns `False` when `unread`. `test_history_unread_bypasses_cache` passes. |
| 4  | ListUnreadMessages always fetches live (entire tool bypasses cache)                                        | VERIFIED   | `capability_unread.py` contains zero references to `MessageCache`, `msg_cache`, or `_should_try_cache`. No cache read path added — inherently live. |
| 5  | SearchMessages always fetches live; results are written to MessageCache for future ListMessages hits        | VERIFIED   | `capability_search.py:78-86` always calls `client.iter_messages` with `search=`. After fetch, lines 94-95 run `msg_cache.store_messages(entity_id, hits)`. `test_search_always_hits_api` and `test_search_populates_cache_after_fetch` both pass. |
| 6  | Cache coverage tracking is topic-aware — interleaved message IDs across topics do not produce false hits   | VERIFIED   | `MessageCache.try_read_page()` in `cache.py:456-457` applies `forum_topic_id IS NULL` or `forum_topic_id = ?` clause. `test_message_cache_topic_isolation` and `test_message_cache_topic_null_isolation` confirm cross-topic false hits cannot occur. |

**Score:** 6/6 truths verified

### Required Artifacts

| Artifact                               | Expected                                               | Status     | Details                                                                                     |
|----------------------------------------|--------------------------------------------------------|------------|---------------------------------------------------------------------------------------------|
| `src/mcp_telegram/cache.py`            | `MessageCache`, `_should_try_cache`, `PRAGMA optimize` | VERIFIED   | `class MessageCache` at line 366; `_should_try_cache` at line 491; `PRAGMA optimize` at line 238 inside `_bootstrap_cache_schema` |
| `tests/test_cache.py`                  | 14+ unit tests for Plan 01 behaviors                   | VERIFIED   | 14 new test functions present (lines 882-1163), all passing                                 |
| `src/mcp_telegram/capability_history.py` | Cache-first block with `_should_try_cache`           | VERIFIED   | Imports `MessageCache, _should_try_cache` at line 11; cache block at lines 157-183; `store_messages` at line 242 |
| `src/mcp_telegram/capability_search.py` | Cache population after search                         | VERIFIED   | Imports `MessageCache` at line 8; `msg_cache.store_messages(entity_id, hits)` at line 95   |
| `src/mcp_telegram/message_ops.py`      | Reply map cache lookup before API fallback             | VERIFIED   | `_build_reply_map` signature includes `msg_cache: MessageCache | None = None` at line 218; cache lookup loop at lines 232-243 |
| `tests/test_capability_history.py`     | 7 integration tests for cache-first history reads      | VERIFIED   | 7 async test functions, all passing                                                         |
| `tests/test_capability_search.py`      | 2 integration tests for search cache population        | VERIFIED   | 2 async test functions, all passing                                                         |

### Key Link Verification

| From                                           | To                                         | Via                                     | Status  | Details                                                                            |
|------------------------------------------------|--------------------------------------------|-----------------------------------------|---------|------------------------------------------------------------------------------------|
| `capability_history.py`                        | `cache.py::MessageCache`                   | `MessageCache(cache._conn)` at line 158 | WIRED   | Import confirmed line 11; instantiation confirmed line 158                         |
| `capability_history.py`                        | `cache.py::_should_try_cache`              | Called at line 176                      | WIRED   | `_should_try_cache(navigation, unread=unread)` guards the cache read path          |
| `capability_history.py`                        | `cache.py::MessageCache.store_messages`    | `msg_cache.store_messages` at line 242  | WIRED   | Called in the `else` branch after every API fetch                                  |
| `capability_search.py`                         | `cache.py::MessageCache.store_messages`    | `msg_cache.store_messages` at line 95   | WIRED   | Called unconditionally after search `hits` are collected                           |
| `message_ops.py::_build_reply_map`             | `cache.py::MessageCache`                   | `msg_cache._conn.execute(...)` per reply ID | WIRED | Cache lookup loop at lines 233-243; uncached IDs fall back to `client.get_messages` |
| `capability_history.py::_build_reply_map call` | `message_ops.py::_build_reply_map`         | `msg_cache=msg_cache` kwarg at line 261 | WIRED   | `msg_cache` instance passed through from the history capability                    |

### Requirements Coverage

| Requirement | Source Plan | Description                                                                                       | Status    | Evidence                                                                                                        |
|-------------|-------------|---------------------------------------------------------------------------------------------------|-----------|-----------------------------------------------------------------------------------------------------------------|
| CACHE-03    | 21-01, 21-02 | Cache-first reads in capability_history for paginated pages                                      | SATISFIED | `_should_try_cache` + `try_read_page` + `if cached_page is not None` in `capability_history.py`                |
| CACHE-04    | 21-01       | Coverage tracking per (dialog_id, topic_id) — topic-aware                                        | SATISFIED | `try_read_page` WHERE clause filters by `forum_topic_id`; returns `None` on partial coverage (`len(rows) < limit`) |
| CACHE-05    | 21-01, 21-02 | Every Telegram API fetch writes results to MessageCache; reply map served from cache when possible | SATISFIED | `msg_cache.store_messages` called in history `else` branch and in search; reply map cache lookup in `_build_reply_map` |
| CACHE-06    | 21-01       | No TTL; `PRAGMA optimize` on bootstrap                                                            | SATISFIED | No TTL field or expiry logic in `MessageCache`; `PRAGMA optimize` at `cache.py:238`                            |
| BYP-01      | 21-01, 21-02 | navigation="newest"/None always fetches from Telegram API                                         | SATISFIED | `_should_try_cache` returns `False` for `navigation is None or navigation == "newest"`                         |
| BYP-02      | 21-01, 21-02 | unread=True always fetches live                                                                   | SATISFIED | `_should_try_cache` returns `False` when `unread=True`                                                         |
| BYP-03      | 21-02       | ListUnreadMessages always fetches live (no cache code added)                                      | SATISFIED | `capability_unread.py` has zero cache imports or calls — confirmed by grep                                      |
| BYP-04      | 21-02       | SearchMessages always hits API; results written to cache                                           | SATISFIED | Search always calls `client.iter_messages`; results stored via `msg_cache.store_messages`                      |

All 8 requirements from phase 21 plans are accounted for. No orphaned requirements.

### Anti-Patterns Found

None. No TODO/FIXME/placeholder comments in modified files. No empty implementations. No stub handlers. No ignored fetch results.

### Human Verification Required

None required. All phase 21 behaviors are verifiable programmatically:
- Cache hit/miss is a pure function of database state and navigation token type
- Bypass rules are implemented as a simple conditional (`_should_try_cache`)
- Test suite provides end-to-end integration coverage for all six truths

## Test Results

```
tests/test_cache.py          — 64 passed (14 new Phase 21 tests + 50 pre-existing)
tests/test_capability_history.py — 7 passed
tests/test_capability_search.py  — 2 passed
Full suite: 330 passed in 2.22s
mypy: 0 errors on cache.py, capability_history.py, capability_search.py, message_ops.py
```

## Summary

Phase 21 goal is fully achieved. All six observable truths are implemented, substantive, and wired:

- The cache-first read path in `capability_history.py` correctly gates on `_should_try_cache`, which encodes BYP-01 (newest/None = always live) and BYP-02 (unread = always live). On a cache hit, `client.iter_messages` is never called.
- Every API fetch in both history and search writes results to `MessageCache` before returning (CACHE-05).
- `capability_unread.py` has no cache code at all, satisfying BYP-03 by design.
- `try_read_page` uses a topic-aware WHERE clause (`forum_topic_id IS NULL` vs `forum_topic_id = ?`) and returns `None` on partial coverage, preventing false hits from interleaved topic message IDs (CACHE-04).
- The `min_id=1` sentinel edge case (OLDEST first page, no cursor) was correctly handled with a guard (`min_id > 1`) to avoid excluding message ID 1 from coverage.

---

_Verified: 2026-03-20_
_Verifier: Claude (gsd-verifier)_
