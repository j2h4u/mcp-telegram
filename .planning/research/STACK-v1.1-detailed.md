# Technology Stack: v1.1 Additions

**Project:** mcp-telegram (Telegram MCP server)
**Researched:** 2026-03-12
**Base stack:** Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz, SQLite

## v1.1 Stack Additions

### Telemetry & Observability

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| **sqlite3** (stdlib) | 3.41+ (Debian) | Telemetry event storage (separate analytics.db) | Lightweight, single-file, ACID compliant; avoid heavyweight observability stack (Prometheus, etc.) that adds monitoring burden; keep mcp-telegram self-contained |
| **asyncio** (stdlib) | Python 3.13 built-in | Async telemetry queue (fire-and-forget events) | Avoid blocking tool responses; standard pattern for production async systems |
| **collections.deque** | stdlib | FIFO event queue (thread-safe append/popleft) | O(1) operations; simple, no external dependency |
| **datetime** (stdlib) | built-in | Timestamp generation, TTL calculation | Built-in, sufficient for telemetry timestamps |

**Not included:**
- OpenTelemetry SDK — too heavyweight for single-deployment scenario; adds observability burden without benefit
- Prometheus/Grafana — operator already has Grafana Alloy + Grafana Cloud; telemetry should expose via simple SQL queries, not full instrumentation framework
- structlog/python-json-logger — events stored in SQLite, not logged to stderr; structured logging not needed

### Cache Improvements

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| **sqlite3** WAL + indexes | 3.41+ | Entity cache optimization (existing entity_cache.db) | Add `CREATE INDEX idx_entity_type_name ON entities(type, name)` and `idx_entity_username ON entities(username)` for fast resolver lookups |
| **time** (stdlib) | built-in | TTL enforcement (timestamp comparisons) | Already used in v1.0; sufficient for cache expiry |

**Not included:**
- Redis — adds deployment complexity (separate service); SQLite sufficient for single deployment
- Memcached — unnecessary; entity metadata cache <50KB for typical users
- functools.lru_cache — doesn't support TTL; SQLite with TTL semantics clearer

### Forum Topics Support

| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| **telethon.tl.functions.channels.GetForumTopicsRequest** | (part of Telethon) | Fetch forum topic list and metadata | Already available in Telethon; no external dependency |
| **telethon.types.ForumTopicDeleted** | (part of Telethon) | Detect deleted topics | Built into Telethon type system; no external API needed |

**Not included:**
- Special-purpose topic library — Telegram topics are sufficiently simple; Telethon provides needed RPC methods
- Caching library for topics — SQLite can cache topic metadata (name, ID, parent_id, deleted_flag) if needed

## Complete v1.1 Stack

### Core Framework
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| Python | 3.13 (pinned) | Runtime | pydantic-core PyO3 0.22.6 compatibility; immutable (locked) |
| Telethon | 1.34+ | Telegram MTProto client | v1.0 choice; stable for v1.1 additions |
| MCP SDK | 1.0+ | MCP server scaffolding | v1.0 choice; no changes needed |
| Pydantic | 2.x | Data validation (tool args) | v1.0 choice; handles union types correctly |
| rapidfuzz | 3.x | Fuzzy name resolution | v1.0 choice; WRatio scorer sufficient for Cyrillic |

### Database
| Technology | Version | Purpose | Why |
|------------|---------|---------|-----|
| SQLite | 3.41+ (Debian 13) | Entity metadata cache + telemetry events | ACID, WAL mode for concurrent reads, lightweight |

### Supporting Libraries
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| aiofiles | 23.x+ | Async file I/O (if telemetry persistence needed) | **Not used in v1.1** — telemetry persisted directly to SQLite |
| pytest-asyncio | 0.23+ | Async test fixtures | Testing v1.1 features (existing; add concurrent load tests) |
| pytest-benchmark | 4.0+ | Performance benchmarking | **New for v1.1** — measure telemetry overhead, cache performance |

## Alternatives Considered

| Category | Recommended | Alternative | Why Not |
|----------|-------------|-------------|---------|
| Telemetry backend | SQLite (separate DB) | PostgreSQL/ClickHouse | Overkill; operator doesn't run observability DB; single deployment doesn't warrant separate DB server |
| Telemetry backend | SQLite (separate DB) | JSON files on disk | No query capability; inefficient for time-range searches |
| Telemetry backend | SQLite (separate DB) | OpenTelemetry + Grafana Cloud | Too heavyweight; operator already has Grafana Cloud; telemetry in mcp-telegram should be self-contained |
| Cache implementation | SQLite + TTL | Redis | Adds deployment complexity; SQLite sufficient for single deployment with <1MB metadata |
| Cache implementation | SQLite + TTL | in-memory dict + asyncio.sleep | No persistence across restarts; TTL implementation error-prone |
| Topic implementation | Telethon native GetForumTopicsRequest | Manual RPC with channels.pb2 | Telethon abstracts away TL complexity; use built-in method |
| Async pattern | fire-and-forget (create_task) | explicit Background task / Queue | simpler; fire-and-forget idiomatic in Python async; no need for explicit task management |
| Async pattern | fire-and-forget (create_task) | Celery / Dramatiq | Overkill; single deployment, low event volume |

## Installation

```bash
# New in v1.1 (v1.0 already installed)
# No new packages required; all dependencies are stdlib or already present

# Optional: for benchmarking only
pip install pytest-benchmark  # Concurrent load testing

# For local development
pip install -e ".[dev]"  # Includes pytest-asyncio, pytest-benchmark
```

## Configuration

### v1.1-specific environment variables
```bash
# .env (or /opt/docker/mcp-telegram/.env)
TELEMETRY_ENABLED=true           # Enable telemetry collection (default: true)
TELEMETRY_RETENTION_DAYS=30      # Delete telemetry older than N days (default: 30)
TELEMETRY_FLUSH_INTERVAL=60      # Flush queue every N seconds (default: 60)
TELEMETRY_FLUSH_SIZE=100         # Flush queue when N events accumulated (default: 100)
```

### SQLite pragmas (analytics.db)
```python
# Set during analytics database initialization:
PRAGMA journal_mode=WAL;         # Enable concurrent reads with writes
PRAGMA synchronous=NORMAL;       # Faster writes, acceptable for telemetry (not critical data)
PRAGMA cache_size=2000;          # 2000 pages = ~8MB cache (default 2000)
PRAGMA busy_timeout=5000;        # Wait 5s on locked database before error

# Create indexes (Phase 2):
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts);
CREATE INDEX IF NOT EXISTS idx_telemetry_tool ON telemetry(tool_name);
CREATE INDEX IF NOT EXISTS idx_entity_type_name ON entities(type, name);
CREATE INDEX IF NOT EXISTS idx_entity_username ON entities(username);
```

## Backward Compatibility

**v1.0 → v1.1 migration:**
- ✓ Existing entity_cache.db unchanged (new indexes added, non-breaking)
- ✓ New analytics.db created on first v1.1 startup (separate file)
- ✓ All v1.0 tools (ListDialogs, ListMessages, SearchMessages, GetMe, GetUserInfo) unchanged
- ✓ New GetUsageStats tool added (new, no removal)
- ✓ New `from_beginning` parameter in ListMessages (optional, default=False, backward-compatible)
- ✓ New `topic` parameter in ListMessages (optional, default=None, backward-compatible)

## Performance Expectations

| Operation | v1.0 | v1.1 | Overhead | Notes |
|-----------|------|------|----------|-------|
| ListMessages | 150ms avg | 151ms avg | <1% | Telemetry queue append is O(1), negligible |
| ListMessages (concurrent 10 calls) | 1.5s total | 1.6s total | ~7% | Telemetry flush runs async; slight contention if flush timing coincides |
| GetUsageStats | — | 50ms | — | Cached for 60s; subsequent calls <1ms |
| Database size (1 month, 100 tools/day) | 50KB | 55KB | +10% | analytics.db ~5KB (telemetry), entity_cache.db +5KB (indexes) |

**Profile assumptions:**
- Baseline measured on v1.0 (Phase 5 completed)
- v1.1 overhead measured under concurrent load (pytest-benchmark, 100 concurrent calls)
- Telemetry flush every 60s or 100 events (batching amortizes lock acquisition)

## Sources

- [SQLite WAL Documentation](https://www.sqlite.org/wal.html) — Concurrent reads during writes
- [SQLite PRAGMA Options](https://www.sqlite.org/pragma.html) — Performance tuning
- [Python asyncio Best Practices](https://docs.python.org/3.13/library/asyncio.html) — Async patterns
- [Telethon GetForumTopics Method](https://docs.telethon.dev/en/stable/reference/telethon.client.messages.MessageMethods.html#telethon.client.messages.MessageMethods.get_forum_topics) — Topic API
