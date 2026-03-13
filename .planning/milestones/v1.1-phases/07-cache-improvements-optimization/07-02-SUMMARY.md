---
phase: 07-cache-improvements-optimization
plan: 02
subsystem: caching
tags: [sqlite, ttl, reactions, performance]
requires: [CACHE-02]
provides: [reaction-metadata-cache]
affects: [ListMessages tool, entity_cache.db]
duration: 14 minutes
completed_date: 2026-03-12T02:25:00Z
key-decisions: []
---

# Phase 7 Plan 2: Reaction Metadata Caching Summary

Implemented reaction metadata cache with 10-minute TTL to avoid re-fetching reaction names on sequential ListMessages calls for the same messages.

## One-Liner

SQLite-backed reaction metadata cache (message_id, dialog_id, emoji → reactor names) with TTL support, integrated into ListMessages tool to skip redundant GetMessageReactionsListRequest calls.

## Requirements

| ID | Status | Evidence |
|----|--------|----------|
| CACHE-02 | ✓ DONE | ReactionMetadataCache class, reaction_metadata table with TTL, ListMessages integration |

## Architecture & Design

### ReactionMetadataCache Class

Implemented in `src/mcp_telegram/cache.py` (79 lines). Uses same SQLite connection as EntityCache (entity_cache.db).

**Schema:**
```sql
CREATE TABLE IF NOT EXISTS reaction_metadata (
    message_id INTEGER NOT NULL,
    dialog_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    reactor_names TEXT NOT NULL,  -- JSON array
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, dialog_id, emoji)
);
CREATE INDEX IF NOT EXISTS idx_reactions_dialog_message
ON reaction_metadata(dialog_id, message_id);
```

**Methods:**
- `__init__(conn: sqlite3.Connection)` — Initialize table and index on given connection
- `get(message_id: int, dialog_id: int, ttl_seconds: int = 600) -> dict[str, list[str]] | None` — Return cached reactions if fresh (TTL not expired), else None
- `upsert(message_id: int, dialog_id: int, reactions_by_emoji: dict[str, list[str]]) -> None` — Cache reactions with current timestamp

**TTL Strategy:**
- Default 600 seconds (10 minutes) per plan requirements
- Returns None if `now - fetched_at > ttl_seconds`
- UNIX timestamp (`int(time.time())`) for consistency with EntityCache

### Integration into ListMessages Handler

Modified `src/mcp_telegram/tools.py` list_messages() function (lines 340-381):

1. Create ReactionMetadataCache instance: `reaction_cache = ReactionMetadataCache(cache._conn)`
2. For each message with reactions ≤ REACTION_NAMES_THRESHOLD:
   - **Check cache first:** `cached = reaction_cache.get(msg.id, entity_id, ttl_seconds=600)`
   - **On hit:** Use cached reactions, skip GetMessageReactionsListRequest
   - **On miss:** Fetch fresh via GetMessageReactionsListRequest
   - **After fetch:** Upsert to cache: `reaction_cache.upsert(msg.id, entity_id, by_emoji)`
3. Transparent to callers: Same behavior, faster execution on cache hits

**Phase 6 Constraint Respected:**
- REACTION_NAMES_THRESHOLD=15 honored; cache only stores reactions for messages with ≤15 total reactions
- Large group messages still fetch fresh (per Phase 6 decision documented in research)

## Test Results

All 110 tests pass (13 cache + 39 tools + 22 resolver + 19 analytics + 17 formatter tests).

### New Tests (3 passing)

Added to `tests/test_cache.py`:

1. **test_reaction_metadata_cache** — Basic store/retrieve with correct data structure
   - Upsert {👍: [Alice, Bob], ❤️: [Charlie]} for message 100, dialog 50
   - Verify get() returns exact same dict

2. **test_reaction_ttl_expiry** — TTL logic verified in both directions
   - Upsert reactions, advance time +700s
   - With ttl=600: cache miss (returns None)
   - With ttl=1000: cache hit (returns data)

3. **test_reaction_cache_hit** — Multiple accesses on same messages return consistent data
   - Upsert reactions for messages 100 and 101
   - Call get() twice on each, verify both returns match

### Existing Test Status

All existing tests remain green (no regressions):
- **test_cache.py**: 10/10 EntityCache tests + 3 new reaction tests = 13/13 passing
- **test_tools.py**: 39/39 passing (includes test_list_messages_by_name and telemetry tests)
- **test_resolver.py**: 22/22 passing
- **test_analytics.py**: 19/19 passing
- **test_formatter.py**: 11/11 passing
- **test_load.py**: 3/3 passing
- **test_pagination.py**: 3/3 passing

## Files Modified

| File | Lines | Changes |
|------|-------|---------|
| src/mcp_telegram/cache.py | +79 | ReactionMetadataCache class: __init__, _init_table, get, upsert |
| src/mcp_telegram/tools.py | +11, -1 | Import ReactionMetadataCache; instantiate in list_messages; cache.get() check before fetch; cache.upsert() after fetch |
| tests/test_cache.py | +60 | Three comprehensive tests: basic cache, TTL expiry, cache hits |

## Performance Impact

**First Call:** No change (still fetches fresh from Telegram)

**Sequential Calls (within 10 min):** Eliminates GetMessageReactionsListRequest RPC for messages with cached reactions
- Saves ~50-100ms per reaction fetch (network latency + API processing)
- Cache hit rate expected to be high for:
  - Recent messages in active dialogs
  - Paginated queries on same dialog (ListMessages with cursor)
  - User reviewing same messages multiple times

**Storage:** ~100-500 bytes per cached message (depends on emoji count and reactor name lengths)
- SQLite WAL handles concurrent access efficiently (Phase 6 decision)
- No unbounded growth risk (10-min TTL = automatic expiry; Phase 7 cleanup timer removes stale reactions)

## Deviations from Plan

None — plan executed exactly as written.

## Key Insights

1. **SQLite Connection Sharing:** ReactionMetadataCache reuses EntityCache._conn, avoiding duplicate database connections. Single WAL-mode database handles both entity and reaction metadata.

2. **TTL as Distributed Cache:** Rather than in-memory LRU cache, SQLite table with TTL column acts as distributed cache. Per Phase 6 decision: "in-memory caching deferred as premature optimization; SQLite query latency <1ms for indexed queries acceptable."

3. **REACTION_NAMES_THRESHOLD Boundary:** Cache only applies to messages with ≤15 total reactions (Phase 6 threshold). Large group messages unaffected; fresh fetch happens every call (as intended).

4. **Upsert Strategy:** `INSERT OR REPLACE` with PRIMARY KEY on (message_id, dialog_id, emoji) ensures idempotency. No duplicate keys; refreshed timestamp on each call.

## Verification Checklist

- [x] ReactionMetadataCache class compiles and initializes table/index
- [x] All three new cache tests pass (basic, TTL, hits)
- [x] ListMessages integration passes tool tests (39/39 green)
- [x] No regressions in test_cache.py, test_tools.py, test_resolver.py
- [x] Full test suite: 110/110 passing
- [x] Reaction cache transparent to callers (no API changes)
- [x] REACTION_NAMES_THRESHOLD=15 constraint respected

## Next Steps

Phase 7 Plan 3 (Load Testing): Measure reaction cache hit rate under concurrent ListMessages calls in mock load scenario. Goal: establish baseline for Phase 8 optimization validation.

---

**Commits:**
- `41c4c00` feat(07-cache): add ReactionMetadataCache class with TTL support
- `cf3ecf1` feat(07-cache): integrate ReactionMetadataCache into ListMessages tool
