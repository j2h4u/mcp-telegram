# Phase 21 Research: Cache-First Reads & Bypass Rules

**Researched:** 2026-03-20
**Status:** Complete

## Phase Goal

History reads serve pages 2+ from cache when available; bypass rules ensure live data where required.

## Requirements Mapping

| REQ-ID | Summary | Complexity |
|--------|---------|------------|
| CACHE-03 | Cache-first reads for paginated pages (page 2+) | High |
| CACHE-04 | Coverage tracking per (dialog_id, topic_id) | Medium |
| CACHE-05 | Cache population on every API fetch | Medium |
| CACHE-06 | No TTL, PRAGMA optimize on bootstrap | Low |
| BYP-01 | navigation="newest" always live | Low |
| BYP-02 | unread=True always live | Low |
| BYP-03 | ListUnreadMessages always live | Low |
| BYP-04 | SearchMessages always live, results written to cache | Low-Medium |

## Codebase Analysis

### Current Data Flow (no cache)

```
ListMessages tool (tools/reading.py)
  → execute_history_read_capability (capability_history.py)
    → _build_history_iter_kwargs (message_ops.py)  — builds kwargs
    → client.iter_messages(**iter_kwargs)           — ALWAYS hits Telegram API
    → _cache_message_senders / _build_reply_map     — post-fetch processing
  → format_messages                                 — renders to text

SearchMessages tool (tools/reading.py)
  → execute_search_messages_capability (capability_search.py)
    → client.iter_messages(search=query)            — ALWAYS hits Telegram API

ListUnreadMessages tool (tools/unread.py)
  → execute_unread_messages_capability (capability_unread.py)
    → client.iter_messages(min_id=read_inbox_max_id) — ALWAYS hits Telegram API
```

### Phase 20 Deliverables Available

- `message_cache` table: 11 columns, WITHOUT ROWID, PK (dialog_id, message_id)
- `idx_message_cache_dialog_sent` on (dialog_id, sent_at DESC) — range reads
- `CachedMessage.from_row()` — constructs MessageLike-compatible proxy from SQLite row
- Formatter transparency verified: `format_messages([CachedMessage(...)])` works

### Key Integration Point

`execute_history_read_capability()` in `capability_history.py` is the single function where cache-first logic must be inserted. The decision point is **after** `_build_history_iter_kwargs()` succeeds and **before** `client.iter_messages()` is called.

### Navigation Token Structure

```python
NavigationToken(kind="history", value=message_id, dialog_id=..., topic_id=..., direction=...)
```

- `navigation=None` or `"newest"` → first page, newest direction → **BYP-01: always live**
- `navigation="oldest"` → first page, oldest direction → cacheable (historical data)
- `navigation=<base64 token>` → page 2+, any direction → **cacheable if coverage exists**

### Bypass Decision Matrix

| Tool | Condition | Cache? | Rationale |
|------|-----------|--------|-----------|
| ListMessages | navigation="newest"/None | NO | BYP-01: first page must be fresh |
| ListMessages | navigation="oldest" (no token) | YES | Historical, immutable |
| ListMessages | navigation=token (page 2+) | YES | CACHE-03 |
| ListMessages | unread=True | NO | BYP-02: read state changes in real time |
| ListUnreadMessages | always | NO | BYP-03: entire tool bypasses |
| SearchMessages | always | NO | BYP-04: server-side search, not cacheable |

## Design Decisions

### Cache Coverage Strategy

**Approach: Query-based coverage check (no separate coverage table)**

Rather than maintaining a separate coverage tracking table, check coverage by querying `message_cache` directly:

1. For a page 2+ read with `max_id=X, limit=N` (newest direction):
   - Query: `SELECT * FROM message_cache WHERE dialog_id=? AND (topic_id condition) AND message_id < ? ORDER BY message_id DESC LIMIT ?`
   - If result count == N → cache hit
   - If result count < N → cache miss, fall back to API

2. For oldest direction with `min_id=X, limit=N`:
   - Query: `SELECT * FROM message_cache WHERE dialog_id=? AND (topic_id condition) AND message_id > ? ORDER BY message_id ASC LIMIT ?`

**Why no separate coverage table:**
- The message_cache table IS the source of truth for what's cached
- Querying it directly avoids dual bookkeeping and consistency bugs
- The `idx_message_cache_dialog_sent` index makes these range queries efficient
- Coverage tracking per (dialog_id, topic_id) is naturally achieved by including `forum_topic_id` in the WHERE clause

**Topic-awareness (CACHE-04):** Messages from different topics interleave by message_id within a dialog. The query includes `forum_topic_id IS NULL` (no topic) or `forum_topic_id = ?` (specific topic) to avoid false coverage from other topics' messages.

### Cache Population Points (CACHE-05)

Three population sites:

1. **capability_history.py** — after `client.iter_messages()` returns in `execute_history_read_capability()`
2. **capability_search.py** — after `client.iter_messages(search=...)` returns (BYP-04: search results written to cache)
3. **Reply map** — `_build_reply_map()` fetches replied-to messages; cache those too

Population is a fire-and-forget write: INSERT OR REPLACE into message_cache, mapping Telethon message attributes to the 11-column schema.

### MessageCache Class Design

New class in `cache.py` sharing the same SQLite connection (like ReactionMetadataCache and TopicMetadataCache):

```python
class MessageCache:
    def __init__(self, conn: sqlite3.Connection) -> None: ...

    def try_read_page(
        self, dialog_id: int, *, topic_id: int | None,
        anchor_id: int | None, limit: int, direction: HistoryDirection,
    ) -> list[CachedMessage] | None:
        """Return cached page or None on miss. None = fall back to API."""

    def store_messages(self, dialog_id: int, messages: Iterable[MessageLike]) -> None:
        """INSERT OR REPLACE messages into cache. Extracts fields from MessageLike."""

    def store_message_rows(self, rows: list[tuple]) -> None:
        """Batch insert raw tuples (for bulk operations)."""
```

`try_read_page()` returns `list[CachedMessage]` on hit (count == limit) or `None` on miss. The caller then either uses the cached result or falls through to `client.iter_messages()`.

### PRAGMA Optimize (CACHE-06)

Add `conn.execute("PRAGMA optimize")` to `_bootstrap_cache_schema()` — runs SQLite's built-in query planner optimizer on first open. Safe to call repeatedly (no-op if stats are fresh).

### Integration in capability_history.py

The cache-first check slots in between `_build_history_iter_kwargs()` and `client.iter_messages()`:

```python
# After iter_kwargs is built, before API call:
if _should_try_cache(navigation, unread):
    msg_cache = MessageCache(cache._conn)
    cached = msg_cache.try_read_page(
        entity_id, topic_id=topic_id, anchor_id=..., limit=limit, direction=...
    )
    if cached is not None:
        raw_messages = cached  # Skip API call entirely
    else:
        raw_messages = [msg async for msg in client.iter_messages(**iter_kwargs)]
        msg_cache.store_messages(entity_id, raw_messages)  # Populate cache
else:
    raw_messages = [msg async for msg in client.iter_messages(**iter_kwargs)]
    msg_cache.store_messages(entity_id, raw_messages)  # Always populate
```

The `_should_try_cache()` function encodes all bypass rules:
- Returns False if navigation is None/"newest" (BYP-01)
- Returns False if unread=True (BYP-02)
- Returns True otherwise (page 2+, or oldest first page)

### Reply Map from Cache

When serving from cache, `_build_reply_map()` currently fetches from Telegram API. For cached pages, replied-to messages may also be in cache. Try cache first, fall back to API for misses.

### What This Phase Does NOT Do

- No prefetch (Phase 23: PRE-01 through PRE-05)
- No lazy refresh (Phase 23: REF-01 through REF-03)
- No edit detection (Phase 22: EDIT-01 through EDIT-03)
- No timer-based refresh (out of scope permanently)

## Risk Assessment

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Cache hit returns fewer messages than expected (gaps) | Medium | Return None on count < limit, fall back to API |
| Topic interleaving causes false coverage | Medium | WHERE clause always includes forum_topic_id condition |
| CachedMessage missing fields formatter needs | Low | Already verified in Phase 20 (formatter transparency test) |
| SQLite contention during concurrent writes | Low | WAL mode + busy timeout already configured |

## Validation Architecture

### Unit Tests
- MessageCache.try_read_page() returns None on empty cache
- MessageCache.try_read_page() returns messages when fully covered
- MessageCache.try_read_page() returns None when partially covered (count < limit)
- MessageCache.store_messages() writes all 11 fields correctly
- Topic-aware coverage: messages from topic A don't satisfy queries for topic B
- Bypass rules: _should_try_cache() returns False for newest, unread, True for page 2+

### Integration Tests
- Full round-trip: store messages → read page → verify CachedMessage fields match originals
- Cache population after API fetch → subsequent read serves from cache
- Search results stored in cache → ListMessages page 2+ can serve them

### Smoke Tests
- ListMessages page 1 always hits API (mock verification)
- ListMessages page 2+ serves from cache when covered (no API call)
- ListMessages with unread=True always hits API
- SearchMessages always hits API, results appear in cache afterward

## Suggested Plan Structure

**Wave 1 (foundation):**
- Plan 01: MessageCache class + PRAGMA optimize + cache population
- Plan 02: Coverage query logic (try_read_page) + bypass rules

**Wave 2 (integration):**
- Plan 03: Wire into capability_history.py + capability_search.py + reply map caching

Two waves because Plans 01-02 are independent data layer work, Plan 03 depends on both.

---
*Phase: 21-cache-first-reads-bypass-rules*
*Research completed: 2026-03-20*

## RESEARCH COMPLETE
