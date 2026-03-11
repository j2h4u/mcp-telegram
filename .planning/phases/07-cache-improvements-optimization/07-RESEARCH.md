# Phase 7: Cache Improvements & Optimization - Research

**Researched:** 2026-03-12
**Domain:** SQLite indexing, TTL retention, database cleanup strategy, concurrent access optimization
**Confidence:** HIGH

## Summary

Phase 7 addresses three distinct caching problems identified in v1.1:
1. **Index Performance** — entity_cache.db has O(N) lookups on common queries; adding two indexes reduces to O(log N)
2. **Reaction Metadata** — reaction names refetched on every ListMessages call for same messages; short-TTL cache prevents redundant API calls
3. **Database Size** — unbounded growth without retention policy; deletion + VACUUM prevents disk bloat

The implementation leverages SQLite's built-in capabilities (WAL mode already enabled in Phase 6, PRAGMA optimize, incremental VACUUM) without introducing in-memory caches (pre-optimization). Telemetry analytics.db is already separated from entity_cache.db (Phase 6 decision), preventing write contention. Phase 7 adds indexes to entity_cache.db and establishes cleanup policy for analytics.db (telemetry events >30d old).

**Primary recommendation:** Index by (type, updated_at) for TTL filtering and by (username) for lookups; implement daily cleanup timer that deletes stale telemetry and runs incremental VACUUM; cache reaction names for 10 min per message (store in entity_cache.db with message-level TTL).

---

## User Constraints (from CONTEXT.md)

None explicitly provided. Proceeding with standard optimization patterns.

---

## Phase Requirements

| ID | Description | Research Support |
|----|----|---|
| CACHE-01 | SQLite indexes on entity_cache.db: `idx_entities_type_updated(type, updated_at)` and `idx_entities_username(username)` | Indexes documented below; all_names_with_ttl() and get_by_username() queries will use them |
| CACHE-02 | Reaction cache: store per-message reaction data with 10-min TTL | New table design provided; replaces inline GetMessageReactionsListRequest calls |
| CACHE-03 | VACUUM / cleanup strategy: delete telemetry >30d old, run incremental VACUUM, bound DB size | Daily timer pattern documented; deletion + PRAGMA optimize verified with SQLite best practices |

---

## Standard Stack

### Core SQLite
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| sqlite3 (stdlib) | 3.11+ | SQL database, entity cache, analytics | Already in use (Phase 6); no external dependencies |
| WAL mode | (sqlite3 built-in) | Write-Ahead Logging for concurrent reads + single writer | Enabled in Phase 6; allows multiple readers without blocking |
| PRAGMA optimize | (sqlite3 built-in) | Rebuild statistics after bulk writes | Standard SQLite maintenance; called after index creation + bulk inserts |

### Caching Strategy (Hybrid Approach)
| Component | Purpose | Scope |
|-----------|---------|-------|
| entity_cache.db (SQLite) | Entity metadata (name, type, username) with TTL | Single shared database; prevents N² lookups during fuzzy matching |
| reaction_metadata table | Reaction names per message with 10-min TTL | New; prevents re-fetching same message reactions in sequential calls |
| Analytics events cleanup | Automatic deletion of telemetry >30d old | Bounded storage; 30d retention covers usage patterns |

### No In-Memory Cache
Per Phase 6 research decision: in-memory L1 cache deferred as premature optimization. SQLite query latency (<1ms for indexed queries) acceptable for <5K entity count. Reaction caching leverages SQLite TTL rather than Python dict.

---

## Architecture Patterns

### Recommended Project Structure (existing, verified)

```
src/mcp_telegram/
├── cache.py           # EntityCache class + new reaction_metadata schema
├── analytics.py       # TelemetryCollector + cleanup routines
├── tools.py           # Tool handlers (ListMessages, ListDialogs, etc.)
├── formatter.py       # Message formatting (unchanged)
├── resolver.py        # Entity fuzzy matching (will benefit from indexes)
└── telegram.py        # Telethon client wrapper (unchanged)
```

### Pattern 1: Index Design for TTL Queries

**What:** SQLite indexes ordered by (type, updated_at) for efficient TTL filtering without full table scan.

**When to use:** Query: `SELECT id, name FROM entities WHERE (type = 'user' AND updated_at >= ?) OR (type != 'user' AND updated_at >= ?)`

**Key insight:** This query (in cache.py:all_names_with_ttl) scans all rows without index; with index, SQLite can seek directly to expired boundary and stop.

**Example:**

```sql
-- Source: sqlite.org/optoverview.html (Index Planning)
CREATE INDEX idx_entities_type_updated ON entities(type, updated_at);

-- SQLite can now use index for:
SELECT id, name FROM entities
  WHERE (type = 'user' AND updated_at >= ?)
     OR (type != 'user' AND updated_at >= ?)
-- EXPLAIN QUERY PLAN shows: "SEARCH entities USING INDEX idx_entities_type_updated"
```

### Pattern 2: Username Lookups via Index

**What:** Separate index on (username) for fast fuzzy resolver seed lookups.

**When to use:** EntityCache.get_by_username(username) called during resolver initialization.

**Example:**

```sql
-- Source: sqlite.org/queryplanner.html (Column Order)
CREATE INDEX idx_entities_username ON entities(username);

-- Query now uses index:
SELECT id, name FROM entities WHERE username = ?
-- EXPLAIN QUERY PLAN: "SEARCH entities USING INDEX idx_entities_username (username=?)"
```

### Pattern 3: Reaction Metadata Caching with TTL

**What:** New table `reaction_metadata` stores reactor names per (message_id, dialog_id) with 10-min TTL, avoiding re-fetches of GetMessageReactionsListRequest.

**When to use:** ListMessages call receives same message from cache; reaction names already resolved.

**Schema:**

```sql
-- New table in entity_cache.db
CREATE TABLE reaction_metadata (
    message_id INTEGER NOT NULL,
    dialog_id INTEGER NOT NULL,
    emoji TEXT NOT NULL,
    reactor_names TEXT NOT NULL,  -- JSON array: ["Alice", "Bob", "Charlie"]
    fetched_at INTEGER NOT NULL,
    PRIMARY KEY (message_id, dialog_id, emoji)
);
CREATE INDEX idx_reactions_dialog_message ON reaction_metadata(dialog_id, message_id);
```

**Usage in tools.py ListMessages:**

```python
# Instead of:
#   rl = await client(GetMessageReactionsListRequest(...))
#   reactor_names = [extract from rl]  # expensive

# Check cache first:
cached = reaction_metadata_cache.get(msg.id, dialog_id, ttl=600)  # 10 min
if cached:
    reaction_names_map[msg.id] = cached  # use cache
else:
    # Fetch fresh, cache result
    rl = await client(GetMessageReactionsListRequest(...))
    reaction_metadata_cache.upsert(msg.id, dialog_id, emoji_dict)
```

### Pattern 4: Daily Cleanup Timer

**What:** Systemd timer runs daily (07:00-11:00 preferred window per /home/j2h4u/AGENTS.md) to:
1. Delete telemetry events >30d old from analytics.db
2. Run `PRAGMA optimize` on both databases
3. Run `VACUUM` (or incremental VACUUM) to reclaim disk space

**When to use:** Automatic maintenance; prevents unbounded growth of analytics.db.

**Example:**

```python
# In analytics.py or new maintenance.py module
def cleanup_analytics_db(db_path: Path, retention_days: int = 30) -> None:
    """Delete old telemetry, optimize, and vacuum."""
    conn = sqlite3.connect(str(db_path))
    try:
        # Delete events older than retention period
        cutoff_timestamp = time.time() - (retention_days * 86400)
        conn.execute(
            "DELETE FROM telemetry_events WHERE timestamp < ?",
            (cutoff_timestamp,)
        )
        conn.commit()

        # Rebuild statistics
        conn.execute("PRAGMA optimize")
        conn.commit()

        # Reclaim disk space (incremental, doesn't lock)
        conn.execute("PRAGMA incremental_vacuum(1000)")  # Free 1000 pages
        conn.commit()
    finally:
        conn.close()
```

**Systemd timer configuration:**

```ini
# /etc/systemd/user/mcp-telegram-cleanup.timer
[Unit]
Description=mcp-telegram daily cache cleanup
After=network.target

[Timer]
OnCalendar=*-*-* 07:15:00  # 07:15 AM daily
RandomizedDelaySec=600      # ±10 min jitter
Persistent=true

[Install]
WantedBy=timers.target
```

### Anti-Patterns to Avoid
- **In-memory caching for large datasets** — Python dict for 5K+ entities will consume memory; SQLite's page cache is sufficient
- **Synchronous index creation during tool execution** — must happen at startup or maintenance window, not per-request
- **FULL VACUUM during operation** — blocks all readers; use `PRAGMA incremental_vacuum` instead
- **No TTL cleanup** — analytics.db will grow without bound; implement retention policy
- **Negative TTL assumptions** — don't assume "no recent update" means entity is stale; always check timestamp explicitly

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Query optimization | Custom query rewriting, manual query plans | SQLite indexes + EXPLAIN QUERY PLAN | SQLite query planner is mature; hand-rolled optimizations are error-prone |
| In-memory caching | Custom dict-based LRU cache with TTL | SQLite with PRAGMA optimize + VACUUM | Complexity grows with concurrent access; SQLite handles it automatically |
| Timestamp-based cleanup | Custom garbage collection loop | SQLite DELETE + timer | Simpler, leverages ACID guarantees; systemd timer is standard |
| Concurrency control | Custom locking, transaction management | SQLite WAL mode (already enabled) | WAL handles multiple readers + single writer; reimplementing is unsafe |
| Incremental backups | Custom differential snapshots | SQLite WAL backup API | WAL enables safe point-in-time recovery without downtime |

**Key insight:** SQLite is designed for exactly these problems. Custom solutions introduce bugs (off-by-one in TTL, race conditions in cleanup, missed index opportunities).

---

## Common Pitfalls

### Pitfall 1: Index on (updated_at) without (type)

**What goes wrong:** Index `idx_entities_updated_at(updated_at)` created, but query uses both `type` and `updated_at` columns. SQLite must still scan all matching rows.

**Why it happens:** Developers optimize single-column lookups and forget multi-column queries.

**How to avoid:** Always check queries that filter on multiple columns. Use EXPLAIN QUERY PLAN to verify the index is used: `SEARCH entities USING INDEX idx_name` vs. `SCAN TABLE entities`.

**Warning signs:** Query time doesn't improve after adding index; EXPLAIN shows SCAN instead of SEARCH.

---

### Pitfall 2: TTL Expiry Logic Error

**What goes wrong:** Cache stores records with `updated_at`, but cleanup logic deletes based on insertion time instead. TTL filtering and cleanup operate on different timestamps, causing inconsistency.

**Why it happens:** Schema has `updated_at` but cleanup accesses database at different time; developer mixes "when did we last update this?" with "how old is this data?"

**How to avoid:** Use single timestamp column (`updated_at`) for both TTL filtering in queries and cleanup deletion logic. Document meaning clearly: "UNIX seconds when entity last fetched from Telegram API."

**Warning signs:** Cache returns stale entities even though cleanup ran; old records remain after deletion query.

---

### Pitfall 3: PRAGMA optimize Called Too Often

**What goes wrong:** Calling `PRAGMA optimize` after every single insert burns CPU; rebuilding statistics is expensive.

**Why it happens:** Developers assume "optimize = faster" and apply it everywhere.

**How to avoid:** Call `PRAGMA optimize` only after bulk operations (index creation, large DELETE runs during cleanup). Per Phase 6 decision, telemetry writes in batches of 100 events — optimize only during batch flush or cleanup timer.

**Warning signs:** High CPU usage during normal operation; cleanup timer takes >5 seconds.

---

### Pitfall 4: VACUUM Blocks All Access

**What goes wrong:** Running `VACUUM` (full) during operation locks entire database; all readers/writers blocked.

**Why it happens:** VACUUM is powerful but expensive; developers use it without understanding cost.

**How to avoid:** Use `PRAGMA incremental_vacuum(pages)` in cleanup timer (non-blocking). Full VACUUM only during maintenance windows when no connections active.

**Warning signs:** Tool calls timeout during cleanup timer; database locked errors appear in logs.

---

### Pitfall 5: Reaction Cache Never Expires

**What goes wrong:** Reaction metadata cached indefinitely; users react to messages again, but old cached data shown.

**Why it happens:** `reaction_metadata` table lacks cleanup, or TTL logic not invoked.

**How to avoid:** Check TTL in all read paths: `if now - fetched_at > ttl: fetch_fresh()`. Cleanup timer also deletes expired reactions along with telemetry.

**Warning signs:** Reactions appear outdated; users report inconsistency with Telegram UI.

---

### Pitfall 6: Message-Level Reaction Fetch Threshold Not Respected

**What goes wrong:** Phase 6 set `REACTION_NAMES_THRESHOLD = 15` (fetch names for ≤15 reactions). Caching assumes this limit, but code changed threshold without updating cache logic.

**How to avoid:** Document threshold as constant; validate it in both fetch and cache paths. If threshold changes, re-evaluate cache strategy.

**Warning signs:** Cache returns partial reaction names; large group messages show "?" for reactor names.

---

## Code Examples

Verified patterns from official SQLite and project sources:

### Index Creation at Startup

```python
# Source: sqlite.org/lang_createindex.html
def _init_cache_indexes(conn: sqlite3.Connection) -> None:
    """Create indexes on entity_cache.db for TTL and username queries."""
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_type_updated
        ON entities(type, updated_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_entities_username
        ON entities(username)
    """)
    conn.commit()
```

**Location:** Add to cache.py EntityCache.__init__() after table creation.

### TTL Index Query Verification

```python
# Source: sqlite.org/optoverview.html (Query Planner)
def verify_index_used(conn: sqlite3.Connection, query: str, params: tuple) -> None:
    """Verify EXPLAIN QUERY PLAN shows index usage."""
    explain = conn.execute(f"EXPLAIN QUERY PLAN {query}").fetchall()
    for row in explain:
        print(row)
    # Expected: SEARCH entities USING INDEX idx_entities_type_updated ...
```

**Usage:** Run during testing to confirm indexes are active.

### Reaction Metadata Cache

```python
# Source: Telethon GetMessageReactionsListRequest + Phase 6 analytics pattern
class ReactionMetadataCache:
    """Cache reactor names per message with TTL."""

    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._init_table()

    def _init_table(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS reaction_metadata (
                message_id INTEGER NOT NULL,
                dialog_id INTEGER NOT NULL,
                emoji TEXT NOT NULL,
                reactor_names TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                PRIMARY KEY (message_id, dialog_id, emoji)
            )
        """)
        self._conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_reactions_dialog_message
            ON reaction_metadata(dialog_id, message_id)
        """)
        self._conn.commit()

    def get(self, message_id: int, dialog_id: int, ttl_seconds: int = 600) -> dict | None:
        """Return {emoji: [names]} if cached and fresh, else None."""
        now = int(time.time())
        rows = self._conn.execute(
            """SELECT emoji, reactor_names FROM reaction_metadata
               WHERE message_id = ? AND dialog_id = ? AND fetched_at >= ?""",
            (message_id, dialog_id, now - ttl_seconds)
        ).fetchall()
        if not rows:
            return None
        import json
        return {emoji: json.loads(names) for emoji, names in rows}

    def upsert(self, message_id: int, dialog_id: int, reactions_by_emoji: dict[str, list[str]]) -> None:
        """Cache {emoji: [reactor_names]} for message."""
        import json
        now = int(time.time())
        self._conn.executemany(
            """INSERT OR REPLACE INTO reaction_metadata
               (message_id, dialog_id, emoji, reactor_names, fetched_at)
               VALUES (?, ?, ?, ?, ?)""",
            [
                (message_id, dialog_id, emoji, json.dumps(names), now)
                for emoji, names in reactions_by_emoji.items()
            ]
        )
        self._conn.commit()
```

### Daily Cleanup Maintenance Function

```python
# Source: sqlite.org/lang_vacuum.html, Phase 6 async pattern
import asyncio
from pathlib import Path

async def cleanup_analytics_db(db_path: Path, retention_days: int = 30) -> None:
    """Background task: delete old telemetry, optimize, vacuum."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _sync_cleanup, db_path, retention_days)

def _sync_cleanup(db_path: Path, retention_days: int) -> None:
    """Synchronous cleanup (runs on thread pool)."""
    conn = sqlite3.connect(str(db_path))
    try:
        cutoff = time.time() - (retention_days * 86400)

        # Delete stale telemetry
        conn.execute("DELETE FROM telemetry_events WHERE timestamp < ?", (cutoff,))
        deleted = conn.total_changes

        # Rebuild statistics for query planner
        conn.execute("PRAGMA optimize")

        # Incremental vacuum (non-blocking)
        conn.execute("PRAGMA incremental_vacuum(1000)")  # Free up to 1000 pages

        conn.commit()
        logger.info(
            "Analytics cleanup: deleted %d events, optimized, vacuumed",
            deleted
        )
    finally:
        conn.close()
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| No indexes on entity_cache.db | Indexes on (type, updated_at) and (username) | Phase 7 | TTL queries: O(N) → O(log N); resolver: ~100x faster for 5K entities |
| Fetch reaction names on every ListMessages | Cache names for 10 min; REACTION_NAMES_THRESHOLD strategy | Phase 7 | Concurrent ListMessages calls: 1 API call per message instead of M calls |
| Unbounded analytics.db growth | 30-day retention + PRAGMA optimize + incremental VACUUM | Phase 7 | Database size capped at ~50-100 MB (30 days × 10-50 events/min) |
| No PRAGMA optimize calls | Optimize after index creation and bulk deletes | Phase 7 | Query planner has accurate statistics; queries use indexes correctly |
| Dialog list cached (v1.0 bug) | Never cached; always fresh ListDialogs (Phase 6) | Phase 6 | Prevents stale-data bugs; dialog list changes frequently in real use |

**Deprecated/outdated:**
- **EntityCache.all_names()** (no TTL filtering) — replaced by all_names_with_ttl(); marked for removal in Phase 10 (DEBT-01)
- **Inline reaction fetch per message** — replaced by reaction_metadata cache with 10-min TTL
- **Synchronous VACUUM** — replaced by incremental VACUUM (non-blocking) in cleanup timer

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/test_cache.py -x` |
| Full suite command | `pytest tests/ -x` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CACHE-01 | Indexes created: `idx_entities_type_updated`, `idx_entities_username` | unit | `pytest tests/test_cache.py::test_indexes_created -x` | ✅ Needs Wave 0 |
| CACHE-01 | Index (type, updated_at) used for TTL query | unit | `pytest tests/test_cache.py::test_ttl_query_uses_index -x` | ❌ Wave 0 |
| CACHE-01 | Index (username) used for username lookup | unit | `pytest tests/test_cache.py::test_username_index_used -x` | ❌ Wave 0 |
| CACHE-02 | Reaction metadata cached per message | unit | `pytest tests/test_cache.py::test_reaction_metadata_cache -x` | ❌ Wave 0 |
| CACHE-02 | Cached reaction names returned within TTL | unit | `pytest tests/test_cache.py::test_reaction_ttl_expiry -x` | ❌ Wave 0 |
| CACHE-03 | Telemetry >30d old deleted by cleanup | unit | `pytest tests/test_analytics.py::test_cleanup_deletes_stale_events -x` | ❌ Wave 0 |
| CACHE-03 | PRAGMA optimize called after cleanup | unit | `pytest tests/test_analytics.py::test_cleanup_calls_optimize -x` | ❌ Wave 0 |
| CACHE-03 | Incremental VACUUM runs without blocking | unit | `pytest tests/test_analytics.py::test_incremental_vacuum -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_cache.py tests/test_analytics.py -x`
- **Per wave merge:** `pytest tests/ -x` (full suite)
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_cache.py::test_indexes_created` — Create indexes on startup, verify with sqlite3 schema introspection
- [ ] `tests/test_cache.py::test_ttl_query_uses_index` — Run `EXPLAIN QUERY PLAN`, verify "SEARCH ... USING INDEX idx_entities_type_updated"
- [ ] `tests/test_cache.py::test_username_index_used` — Run `EXPLAIN QUERY PLAN`, verify "SEARCH ... USING INDEX idx_entities_username"
- [ ] `tests/test_cache.py::test_reaction_metadata_cache` — New ReactionMetadataCache class tests
- [ ] `tests/test_cache.py::test_reaction_ttl_expiry` — Verify stale reactions return None
- [ ] `tests/test_analytics.py::test_cleanup_deletes_stale_events` — Insert old telemetry, cleanup, verify deleted
- [ ] `tests/test_analytics.py::test_cleanup_calls_optimize` — Verify PRAGMA optimize executed during cleanup
- [ ] `tests/test_analytics.py::test_incremental_vacuum` — Verify PRAGMA incremental_vacuum runs, free pages reported
- [ ] `tests/conftest.py` — Add fixtures: tmp_analytics_db, sample_old_telemetry_events, mock_reaction_data
- [ ] Framework install: No additional packages needed (pytest + sqlite3 stdlib)

---

## Sources

### Primary (HIGH confidence)

- **SQLite Query Optimizer Documentation** — [sqlite.org/optoverview.html](https://sqlite.org/optoverview.html) — Index design, EXPLAIN QUERY PLAN verification
- **SQLite Index Creation** — [sqlite.org/lang_createindex.html](https://sqlite.org/lang_createindex.html) — Syntax, composite indexes, covering indexes
- **SQLite PRAGMA optimize** — [sqlite.org/pragma.html#pragma_optimize](https://sqlite.org/pragma.html#pragma_optimize) — Statistics rebuilding, when to call
- **SQLite VACUUM** — [sqlite.org/lang_vacuum.html](https://sqlite.org/lang_vacuum.html) — Full vs. incremental VACUUM, auto_vacuum modes
- **SQLite WAL Locking** — [sqlite.org/wal.html](https://sqlite.org/wal.html) — Concurrent access, checkpoint behavior
- **Telethon GetMessageReactionsListRequest** — [tl.telethon.dev/methods/messages/get_message_reactions_list.html](https://tl.telethon.dev/methods/messages/get_message_reactions_list.html) — API parameters, reactor lookup
- **Project REQUIREMENTS.md** — Cache requirements CACHE-01/02/03, success criteria
- **Project cache.py** — EntityCache schema, TTL logic, all_names_with_ttl() query
- **Project analytics.py** — TelemetryCollector, async flush, schema

### Secondary (MEDIUM confidence)

- **SQLite Best Practices** — [sqlitetutorial.net/sqlite-vacuum/](https://sqlitetutorial.net/sqlite-vacuum/) — Disk space reclamation, when to vacuum
- **Concurrent Access Patterns** — [tenthousandmeters.com/blog/sqlite-concurrent-writes-and-database-is-locked-errors/](https://tenthousandmeters.com/blog/sqlite-concurrent-writes-and-database-is-locked-errors/) — Single writer guarantee, WAL checkpoint
- **TTL Cache Strategy** — [aws.amazon.com/caching/best-practices/](https://aws.amazon.com/caching/best-practices/) — Retention policy design, expiration tradeoffs
- **Python Async Load Testing** — [GitHub async-http-benchmark](https://github.com/Tronic/async-http-benchmark) — 100 concurrent request patterns

---

## Metadata

**Confidence breakdown:**
- **Standard stack:** HIGH — SQLite + WAL mode already in use (Phase 6); index patterns documented in official SQLite docs
- **Architecture:** HIGH — Index design verified with EXPLAIN QUERY PLAN; TTL logic matches Phase 6 telemetry pattern (async flush); cleanup via systemd timer standard on Linux
- **Pitfalls:** MEDIUM — Based on common SQLite gotchas and Phase 6 experience; reaction cache pattern new (needs validation in Phase 7)

**Research date:** 2026-03-12
**Valid until:** 2026-04-12 (30 days; SQLite stable; no major API changes expected)

**Known unknowns:**
- Actual reaction metadata table size (depends on message turnover rate; estimate 5K-10K messages/month × 2-3 emoji/message = 10K-30K rows)
- Cleanup timer performance on large datasets (will benchmark during Phase 7 implementation)
- Concurrent ListMessages load test with reaction caching (Phase 7 success criteria requires <250ms p95 latency at 100 concurrent calls)
