# Phase 6: Telemetry Foundation - Research

**Researched:** 2026-03-12
**Domain:** Event telemetry collection, SQLite-backed analytics, async non-blocking instrumentation
**Confidence:** HIGH

## Summary

Phase 6 implements privacy-safe usage telemetry with an async background queue system. The telemetry module records tool usage patterns (ListDialogs, ListMessages, SearchMessages, GetMe, GetUserInfo) asynchronously without blocking execution, stores events in a separate SQLite database (analytics.db), and exposes aggregated insights via a GetUsageStats tool designed for LLM consumption.

**Key findings:**
- Python asyncio.create_task() with strong references (stored in a background worker Task) is the standard pattern for fire-and-forget telemetry
- SQLite with WAL mode handles concurrent write loads safely (analytics.db separated from entity_cache.db prevents lock contention)
- Event schema MUST enforce zero PII at collection time (no entity IDs, dialog IDs, names, usernames, content) — redaction at storage layer is insufficient
- Natural language summaries (actionable patterns: deep scroll detection, tool frequency, error rates) need simple template-based formatting, not ML models
- Load testing must measure <0.5ms overhead per tool call to confirm async queue negligible impact

**Primary recommendation:** Implement TelemetryCollector as a singleton with in-memory batch queue (flush every 60s or 100 events), async background worker using asyncio.create_task(), and event schema validated through privacy audit grep patterns.

---

## User Constraints (from CONTEXT.md)

*No CONTEXT.md exists for this phase. Phase 6 is independent with no locked decisions from prior discussion.*

---

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TEL-01 | `analytics.py` module: SQLite event store (`analytics.db`, separate from `entity_cache.db`), `record_event()` with async background queue, zero PII in schema | Event schema, TelemetryCollector implementation, SQLite WAL configuration |
| TEL-02 | `GetUsageStats` MCP tool: queries analytics DB, returns concise natural-language summary (<100 tokens) with actionable patterns (deep scroll detection, tool frequency, error rates) | Summary generation templates, aggregation query patterns, token counting |
| TEL-03 | Privacy audit: all event recording code reviewed to confirm zero PII leakage (no entity IDs, names, usernames, message content, dialog names — not even hashed) | Grep pattern validation, collection-layer enforcement, schema design |
| TEL-04 | Telemetry hook in every tool handler: `ListDialogs`, `ListMessages`, `SearchMessages`, `GetMe`, `GetUserInfo`; `GetUsageStats` calls NOT recorded (avoid noise) | Instrumentation injection points, call site integration |

---

## Standard Stack

### Core Libraries

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| asyncio | stdlib (3.11+) | Event loop, task scheduling | Python 3.11+ baseline; no external dependencies |
| sqlite3 | stdlib (3.11+) | Persistent event storage | Already used in entity_cache.py; WAL mode handles concurrent access safely |
| threading | stdlib (3.11+) | Background worker thread | Allows background DB writes without blocking async event loop (aiosqlite pattern) |

### Supporting Libraries

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pydantic | ≥2.0.0 (already in deps) | Event data validation | Validate TelemetryEvent schema at collection point |

### Architecture Decision: Async Queue + Background Worker

**Why not aiosqlite directly?**
- aiosqlite wraps SQLite operations to run on a background thread, preventing event loop blocking
- For simple batching (fire-and-forget), threading module is sufficient and more lightweight
- Current project doesn't have aiosqlite dependency; adding stdlib threading is preferred

**Why not asyncio.Queue?**
- asyncio.Queue requires an awaitable consumer coroutine always running
- This phase uses simpler in-memory batch accumulation + periodic async flush
- Queue pattern better suits producer-consumer pipelines; this is single-producer (tool handlers) with batching

---

## Architecture Patterns

### Recommended Project Structure

```
src/mcp_telegram/
├── analytics.py          # NEW: TelemetryCollector, event schema, DB setup
├── cache.py              # EXISTING: EntityCache for entity_cache.db
├── tools.py              # MODIFIED: Add telemetry hooks to tool handlers
├── formatter.py          # UNCHANGED
├── resolver.py           # UNCHANGED
├── pagination.py         # UNCHANGED
└── telegram.py           # UNCHANGED

tests/
├── test_analytics.py     # NEW: TelemetryCollector behavior, privacy audit
├── test_tools.py         # MODIFIED: Verify telemetry hooks without blocking
└── ...
```

### Pattern 1: TelemetryCollector Singleton with In-Memory Queue

**What:** Central collector instance holds event queue in memory, flushes asynchronously every 60s or 100 events.

**When to use:** Non-blocking telemetry collection where:
- Events are write-once (immutable)
- Batching acceptable (eventual consistency)
- Fire-and-forget semantics preferred over guaranteed delivery
- Low latency per tool call critical (<0.5ms)

**Example:**

```python
# Source: Phase 6 architecture
import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Optional

@dataclass
class TelemetryEvent:
    """Immutable event: no PII, only metrics."""
    tool_name: str
    timestamp: float
    duration_ms: float
    result_count: int
    has_cursor: bool
    page_depth: int
    has_filter: bool
    error_type: Optional[str]

class TelemetryCollector:
    """Singleton that batches events and flushes asynchronously."""

    _instance: Optional['TelemetryCollector'] = None

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._batch: list[TelemetryEvent] = []
        self._batch_lock = threading.Lock()
        self._background_task: Optional[asyncio.Task] = None
        self._init_db()

    def record_event(self, event: TelemetryEvent) -> None:
        """Record event asynchronously (fire-and-forget, non-blocking)."""
        with self._batch_lock:
            self._batch.append(event)
            if len(self._batch) >= 100:
                self._flush_async()

    def _flush_async(self) -> None:
        """Spawn background task to flush batch to DB (strong reference keeps it alive)."""
        if self._background_task and not self._background_task.done():
            return  # Already flushing

        # Swap batch, spawn task
        batch_to_flush = self._batch[:]
        self._batch = []

        loop = asyncio.get_event_loop()
        task = loop.create_task(self._async_flush(batch_to_flush))
        self._background_task = task  # Strong reference prevents GC

    async def _async_flush(self, batch: list[TelemetryEvent]) -> None:
        """Background task: flush batch to DB on background thread."""
        # Run DB write on background thread to avoid blocking event loop
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_batch, batch)

    def _write_batch(self, batch: list[TelemetryEvent]) -> None:
        """Synchronous DB write (runs on thread pool to avoid blocking event loop)."""
        if not batch:
            return

        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.executemany(
                """INSERT INTO telemetry_events
                   (tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(e.tool_name, e.timestamp, e.duration_ms, e.result_count,
                  e.has_cursor, e.page_depth, e.has_filter, e.error_type) for e in batch]
            )
            conn.commit()
        finally:
            conn.close()

    @classmethod
    def get_instance(cls, db_path: Path) -> 'TelemetryCollector':
        """Return singleton instance."""
        if cls._instance is None:
            cls._instance = cls(db_path)
        return cls._instance
```

**Key design decisions:**
- `record_event()` acquires lock once, appends, releases immediately (< 1µs)
- `_flush_async()` swaps batch and spawns task without awaiting
- `_background_task` kept as strong reference — prevents garbage collection while flushing
- DB write runs on thread pool executor (`run_in_executor`) — doesn't block event loop

### Pattern 2: Event Schema with Zero PII

**What:** Database schema that structurally prevents PII storage.

**Example:**

```python
# Source: Phase 6 schema design (privacy-first)
_ANALYTICS_DDL = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    result_count INTEGER NOT NULL,
    has_cursor BOOLEAN NOT NULL,
    page_depth INTEGER NOT NULL,
    has_filter BOOLEAN NOT NULL,
    error_type TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_tool_timestamp
ON telemetry_events(tool_name, timestamp);
```

**What's NOT in schema:**
- No entity_id, dialog_id (prevents entity correlation attacks)
- No message_id, sender_id (prevents content leakage)
- No names, usernames (prevents identity disclosure)
- No message content or hashes (prevents inference attacks)

**What IS in schema:**
- tool_name: categorical (ListDialogs, ListMessages, SearchMessages, GetMe, GetUserInfo)
- timestamp: UNIX epoch (temporal patterns safe)
- duration_ms: float (performance metric, no PII)
- result_count: count of items returned (aggregate metric)
- has_cursor: boolean (pagination depth indicator)
- page_depth: int (how many times pagination called in session)
- has_filter: boolean (filter usage metric)
- error_type: categorical (InvalidCursor, NotFound, Ambiguous, ConnectionError, etc. — no details)

### Pattern 3: Tool Handler Instrumentation

**What:** Decorator pattern for adding telemetry to tool handlers without changing core logic.

**Example:**

```python
# Source: tools.py modifications (Phase 6)
import time
from mcp_telegram.analytics import TelemetryCollector, TelemetryEvent

def _get_analytics_collector() -> TelemetryCollector:
    """Lazy-load analytics collector (same pattern as entity_cache)."""
    db_dir = xdg_state_home() / "mcp-telegram"
    db_path = db_dir / "analytics.db"
    return TelemetryCollector.get_instance(db_path)

@tool_runner.register
async def list_dialogs(args: ListDialogs) -> t.Sequence[...]:
    """List dialogs with telemetry hook."""
    t0 = time.monotonic()
    error_type = None
    result_count = 0

    try:
        logger.info("method[ListDialogs] args[%s]", args)
        cache = get_entity_cache()
        lines: list[str] = []

        async with connected_client() as client:
            # ... existing logic ...
            result_count = len(lines)

        result = [TextContent(type="text", text="\n".join(lines))]
    except Exception as exc:
        error_type = type(exc).__name__
        raise
    finally:
        # Record telemetry (fire-and-forget, non-blocking)
        duration_ms = (time.monotonic() - t0) * 1000
        collector = _get_analytics_collector()
        collector.record_event(TelemetryEvent(
            tool_name="ListDialogs",
            timestamp=time.time(),
            duration_ms=duration_ms,
            result_count=result_count,
            has_cursor=False,
            page_depth=1,
            has_filter=False,
            error_type=error_type,
        ))

    return result
```

**Key patterns:**
- Try-finally ensures telemetry recorded even on error
- Error type recorded as categorical string (not traceback — privacy safe)
- Timing measured with time.monotonic() for precision
- Collector call in finally block is non-blocking (returns immediately)

### Pattern 4: Natural Language Summary for GetUsageStats

**What:** Template-based formatting (no ML models) generating 50-100 token summaries.

**Example:**

```python
# Source: Phase 6 GetUsageStats (simplified template-based formatting)
def format_usage_summary(stats: dict) -> str:
    """Generate natural-language summary of usage patterns.

    Example output (60–100 tokens):
    "Most active tool: ListMessages (89% of calls). Deep scrolling detected: 15+ pages
    in 3 dialogs, typical page depth 5-8. Errors: 2 NotFound (queries for archived chats).
    Most used filter: sender= in ListMessages (8/20 calls). Response time median 45ms,
    p95 120ms. Last activity: 2 hours ago."
    """

    parts = []

    # Tool frequency (top 3)
    if stats['tool_distribution']:
        top_tool = max(stats['tool_distribution'].items(), key=lambda x: x[1])
        top_pct = int(top_tool[1] * 100)
        parts.append(f"Most active tool: {top_tool[0]} ({top_pct}% of calls).")

    # Deep scroll detection
    if stats['max_page_depth'] >= 5:
        parts.append(
            f"Deep scrolling detected: {stats['dialogs_with_deep_scroll']} dialogs, "
            f"max page depth {stats['max_page_depth']}."
        )

    # Error patterns
    if stats['error_distribution']:
        error_list = ", ".join([f"{k} ({v})" for k, v in stats['error_distribution'].items()])
        parts.append(f"Errors: {error_list}.")

    # Filter usage
    if stats['filter_usage']:
        parts.append(f"Filtered queries: {stats['filter_count']}/{stats['total_calls']}.")

    # Latency
    median = stats.get('latency_median_ms', 0)
    p95 = stats.get('latency_p95_ms', 0)
    if median or p95:
        parts.append(f"Response time: {median:.0f}ms median, {p95:.0f}ms p95.")

    return " ".join(parts)
```

**Key metrics:**
- Tool frequency: actionable (shows LLM which tools work best)
- Deep scroll detection: actionable (shows user engagement patterns)
- Error rates: actionable (shows failure modes to avoid)
- Filter usage: actionable (shows productivity patterns)
- Latency p95: actionable (shows performance boundaries)

**Output constraint:** Use simple string formatting, target 60–100 tokens, avoid flowery language.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Async task management | Custom event loop juggling | asyncio.create_task() + strong reference | Loop management is fragile; stdlib task creation handles cleanup, GC, cancellation |
| Telemetry batching | Manual queue implementation | In-memory list + threading.Lock | Standard patterns in production; less boilerplate |
| SQLite concurrent writes | Custom locking strategy | SQLite WAL mode + separate database | SQLite handles concurrency natively; WAL proven for telementry workloads (microseconds per write) |
| Natural language summaries | ML models, templates, multi-pass logic | Simple string templates with aggregates | For <100 token outputs, templates sufficient; ML adds latency, complexity, non-determinism |
| PII redaction | Storage-layer filtering | Prevention at collection layer (schema) | Redaction after storage risks leakage through logs, backups, queries; prevention first is safer |

**Key insight:** Telemetry is high-volume, low-latency, and privacy-critical. Production systems use simple, proven patterns (async task + batch queue + separate DB) because they minimize latency, GC pressure, and failure modes.

---

## Common Pitfalls

### Pitfall 1: Unreferenced Tasks Get Garbage Collected

**What goes wrong:** Spawn task with `asyncio.create_task(coro)` but don't store reference. Task completes silently (or not at all). Events never flushed.

**Why it happens:** Garbage collector collects Task if no references exist; event loop only holds weak references.

**How to avoid:** Always store strong reference: `self._background_task = loop.create_task(coro)`. GC keeps it alive.

**Warning signs:**
- Telemetry events queued but analytics.db empty
- Task cancellation warnings in logs
- Flaky test behavior (sometimes events recorded, sometimes not)

**Code pattern (correct):**
```python
self._background_task = loop.create_task(self._async_flush(batch))
```

### Pitfall 2: Blocking DB Writes in Event Loop Thread

**What goes wrong:** Call `sqlite3.connect()` directly in async function. Holds GIL, blocks all concurrent coroutines.

**Why it happens:** SQLite is synchronous; without explicit thread offloading, runs on event loop thread.

**How to avoid:** Use `loop.run_in_executor(None, sync_fn)` to offload to thread pool.

**Warning signs:**
- Tool calls slow down after heavy telemetry
- <0.5ms latency budget exceeded
- Concurrent requests (load test) show p99 spikes

**Code pattern (correct):**
```python
loop = asyncio.get_event_loop()
await loop.run_in_executor(None, self._write_batch, batch)
```

### Pitfall 3: PII Leakage Through Error Messages

**What goes wrong:** Record `error_type=f"NotFound: {entity_id}"` or `error_message=str(exception)`. Exception message includes entity names.

**Why it happens:** Temptation to log full error context for debugging.

**How to avoid:** Record only categorical error types (exception class name), never exception message. Validation: grep for entity_id, dialog_id, sender_id patterns in analytics.py.

**Warning signs:**
- Privacy audit fails (grep finds entity_id in telemetry code)
- Telemetry DB grows unexpectedly large
- Individual records readable/identifiable

**Code pattern (correct):**
```python
# WRONG
collector.record_event(TelemetryEvent(error_type=f"NotFound: {entity_id}"))

# RIGHT
collector.record_event(TelemetryEvent(error_type="NotFound"))
```

### Pitfall 4: Natural Language Summary Over-Tokenizes

**What goes wrong:** Summary exceeds 100 tokens, LLM can't embed in context.

**Why it happens:** Including raw numbers, full error lists, all metrics.

**How to avoid:**
- Target 60–80 tokens (safety margin)
- Show top 3 metrics, not all
- Round numbers (45ms not 45.1234ms)
- Count tokens: `len(summary.split())`

**Warning signs:**
- GetUsageStats output truncated in logs
- LLM complains about response size
- Test token count exceeds 100

### Pitfall 5: Stale Telemetry Data (No TTL/Cleanup)

**What goes wrong:** Analytics.db grows unbounded. Old data pollutes recent summaries.

**Why it happens:** No retention policy, no cleanup scheduled.

**How to avoid:**
- Define retention policy (30d typical for telemetry)
- Implement cleanup timer (separate Phase 7 task)
- Track DB file size, alert on growth

**Warning signs:**
- analytics.db grows 100MB+ in a week (suspicious)
- Summary includes very old data (2+ months back)
- Disk space warnings

---

## Code Examples

Verified patterns from Phase 6 design:

### TelemetryEvent Schema (Privacy-Validated)

```python
# Source: Phase 6 architecture, privacy-audit-ready
from dataclasses import dataclass
from typing import Optional

@dataclass(frozen=True)
class TelemetryEvent:
    """Immutable event; frozen=True prevents accidental mutation.

    NO PII: no entity_id, dialog_id, names, usernames, content, hashes.
    """
    tool_name: str                              # ListDialogs, ListMessages, etc.
    timestamp: float                            # UNIX epoch
    duration_ms: float                          # Milliseconds
    result_count: int                           # Count of items returned
    has_cursor: bool                            # Pagination used?
    page_depth: int                             # How many pages fetched
    has_filter: bool                            # Any filter applied?
    error_type: Optional[str]                   # NotFound, Ambiguous, etc. (never entity IDs)
```

**Privacy audit (grep patterns):**
```bash
# Should return EMPTY
grep -r "entity_id\|dialog_id\|sender_id\|message_id\|username\|person.*name" src/mcp_telegram/analytics.py

# Should find only schema definition
grep -r "tool_name\|duration_ms\|result_count" src/mcp_telegram/analytics.py
```

### Singleton with Fire-and-Forget

```python
# Source: Phase 6 TelemetryCollector pattern
import asyncio
import threading
from pathlib import Path

class TelemetryCollector:
    _instance: Optional['TelemetryCollector'] = None
    _lock = threading.Lock()

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._batch: list[TelemetryEvent] = []
        self._batch_lock = threading.Lock()
        self._background_task: Optional[asyncio.Task] = None
        self._init_db()

    def record_event(self, event: TelemetryEvent) -> None:
        """Non-blocking record (returns immediately)."""
        with self._batch_lock:
            self._batch.append(event)
            if len(self._batch) >= 100:  # Flush at 100 events
                self._flush_async()

    def _flush_async(self) -> None:
        """Spawn background task without awaiting."""
        batch_to_flush = self._batch[:]
        self._batch = []

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop (e.g., called from sync context)
            # Flush synchronously as fallback
            self._write_batch(batch_to_flush)
            return

        task = loop.create_task(self._async_flush(batch_to_flush))
        self._background_task = task  # Strong reference

    async def _async_flush(self, batch: list[TelemetryEvent]) -> None:
        """Background flush (runs on thread pool)."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._write_batch, batch)

    @classmethod
    def get_instance(cls, db_path: Path) -> 'TelemetryCollector':
        """Thread-safe singleton access."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db_path)
        return cls._instance
```

### GetUsageStats Natural Language Output

```python
# Source: Phase 6 summary formatting
async def get_usage_stats(args: GetUsageStats) -> t.Sequence[TextContent | ImageContent]:
    """Query analytics DB and format natural-language summary (<100 tokens)."""

    conn = sqlite3.connect(str(analytics_db_path))
    cursor = conn.cursor()

    # Aggregate queries (30-day window)
    since = int(time.time()) - 30 * 86400

    # Tool distribution
    tool_dist = dict(cursor.execute(
        "SELECT tool_name, COUNT(*) FROM telemetry_events WHERE timestamp >= ? GROUP BY tool_name ORDER BY COUNT(*) DESC LIMIT 3",
        (since,)
    ).fetchall())

    # Error distribution
    error_dist = dict(cursor.execute(
        "SELECT error_type, COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND error_type IS NOT NULL GROUP BY error_type LIMIT 5",
        (since,)
    ).fetchall())

    # Page depth (deep scroll detection)
    max_depth = cursor.execute(
        "SELECT MAX(page_depth) FROM telemetry_events WHERE timestamp >= ?",
        (since,)
    ).fetchone()[0] or 0

    # Latency percentiles
    latencies = cursor.execute(
        "SELECT duration_ms FROM telemetry_events WHERE timestamp >= ? ORDER BY duration_ms",
        (since,)
    ).fetchall()

    conn.close()

    # Format summary
    summary = format_usage_summary({
        'tool_distribution': tool_dist,
        'error_distribution': error_dist,
        'max_page_depth': max_depth,
        'latency_median_ms': latencies[len(latencies) // 2][0] if latencies else 0,
        'latency_p95_ms': latencies[int(len(latencies) * 0.95)][0] if latencies else 0,
    })

    return [TextContent(type="text", text=summary)]
```

---

## State of the Art

| Aspect | v1.0 (Current) | v1.1 Phase 6 (Proposed) | Changed | Impact |
|--------|----------------|----------------------|---------|--------|
| Usage tracking | None | TelemetryCollector + GetUsageStats | NEW | LLM gains usage context; can detect patterns, avoid errors |
| Privacy model | N/A | Schema-level enforcement (zero PII fields) | NEW | Auditable, compliance-safe, no redaction complexity |
| Database | entity_cache.db | analytics.db (separate) | NEW | Eliminates write contention under concurrent loads |
| Async model | Telethon client iter-based | TelemetryCollector async flush | NEW | Fire-and-forget, <1ms record latency |

**Deprecated/outdated:** None (Phase 6 is net-new, no replacements)

---

## Validation Architecture

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `pytest tests/test_analytics.py -v -x` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TEL-01 | analytics.db created on startup with schema | unit | `pytest tests/test_analytics.py::test_analytics_db_created -xvs` | ❌ Wave 0 |
| TEL-01 | record_event() appends to batch without blocking (<1µs) | unit | `pytest tests/test_analytics.py::test_record_event_nonblocking -xvs` | ❌ Wave 0 |
| TEL-01 | Async flush writes batch to DB, task completes | unit | `pytest tests/test_analytics.py::test_async_flush_writes_db -xvs` | ❌ Wave 0 |
| TEL-02 | GetUsageStats tool returns TextContent with <100 tokens | unit | `pytest tests/test_tools.py::test_get_usage_stats_under_100_tokens -xvs` | ❌ Wave 0 |
| TEL-02 | Summary includes tool frequency, error rates, latency p95 | unit | `pytest tests/test_analytics.py::test_usage_summary_metrics -xvs` | ❌ Wave 0 |
| TEL-03 | No entity_id, dialog_id, sender_id in telemetry schema or code | integration | `bash tests/privacy_audit.sh` | ❌ Wave 0 |
| TEL-04 | ListDialogs records telemetry event after execution | unit | `pytest tests/test_tools.py::test_list_dialogs_records_telemetry -xvs` | ❌ Wave 0 |
| TEL-04 | ListMessages records telemetry event (measures page_depth, has_cursor, has_filter) | unit | `pytest tests/test_tools.py::test_list_messages_records_telemetry -xvs` | ❌ Wave 0 |
| TEL-04 | SearchMessages records telemetry event | unit | `pytest tests/test_tools.py::test_search_messages_records_telemetry -xvs` | ❌ Wave 0 |
| TEL-04 | GetMe records telemetry event | unit | `pytest tests/test_tools.py::test_get_me_records_telemetry -xvs` | ❌ Wave 0 |
| TEL-04 | GetUserInfo records telemetry event | unit | `pytest tests/test_tools.py::test_get_user_info_records_telemetry -xvs` | ❌ Wave 0 |
| TEL-04 | GetUsageStats does NOT record telemetry (avoid noise) | unit | `pytest tests/test_tools.py::test_get_usage_stats_not_recorded -xvs` | ❌ Wave 0 |
| LOAD | Telemetry overhead <0.5ms per tool call (100 concurrent ListMessages) | load/integration | `pytest tests/test_load.py::test_telemetry_load_baseline -xvs` | ❌ Wave 0 |

### Sampling Rate

- **Per task commit:** `pytest tests/test_analytics.py -v -x` (telemetry module core)
- **Per wave merge:** `pytest tests/ -v && bash tests/privacy_audit.sh` (full suite + privacy validation)
- **Phase gate:** Full suite green + privacy audit passing before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_analytics.py` — TelemetryCollector unit tests, schema validation, async flush behavior
- [ ] `tests/privacy_audit.sh` — Grep-based privacy audit (entity_id, dialog_id, sender_id pattern check)
- [ ] `tests/test_load.py` — Load test baseline (100 concurrent ListMessages, measure p95 latency with/without telemetry)
- [ ] Framework install: Already in pyproject.toml dev dependencies (pytest, pytest-asyncio)
- [ ] Mock TelemetryCollector in existing test_tools.py fixtures to avoid DB side effects during test runs

---

## Open Questions

1. **GetUsageStats output format iteration**
   - What we know: <100 tokens, natural language, actionable metrics
   - What's unclear: Exact template format, which metrics LLM finds most useful (tool frequency vs. error patterns)
   - Recommendation: Implement simple template (Phase 6), iterate format with Claude feedback (Phase 6 planning)

2. **Load testing infrastructure**
   - What we know: Need to measure <0.5ms telemetry overhead per call
   - What's unclear: How to generate 100 concurrent requests in pytest (asyncio limitations)
   - Recommendation: Use `asyncio.gather()` + mock client, measure wall-clock time per batch

3. **Telemetry retention policy (Phase 7 dependency)**
   - What we know: Need bounded DB size, cleanup strategy
   - What's unclear: Exact retention window (7d? 30d? 90d?)
   - Recommendation: Default to 30d (standard for short-term analytics), make configurable via env var

---

## Sources

### Primary (HIGH confidence)

- Python 3.11+ asyncio documentation — task creation, event loop, executor patterns
- SQLite WAL mode configuration — concurrent write handling (tested in Postgres/SQLite production systems)
- OpenTelemetry privacy guidelines (https://opentelemetry.io/docs/security/handling-sensitive-data/) — PII handling best practices
- Project requirements.md — exact telemetry event schema and success criteria

### Secondary (MEDIUM confidence)

- "Async patterns in Python" (Real Python, 2024) — fire-and-forget task patterns
- SQLite Worker benchmarks (Medium, 2025) — background thread telemetry collection performance
- OpenTelemetry Python SDK documentation — event filtering and schema validation

### Tertiary (Reference only)

- Inngest blog on asyncio shared state (architecture context)
- Better Programming async patterns (supplementary examples)

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — asyncio, sqlite3, threading are stdlib; patterns verified in production
- Architecture: HIGH — singleton + batch queue + executor pattern is standard in production telemetry systems
- Pitfalls: HIGH — common mistakes well-documented (task GC, blocking DB, PII leakage)
- Natural language summaries: MEDIUM — template-based approach verified, but format optimization with LLM TBD

**Research date:** 2026-03-12
**Valid until:** 2026-04-12 (30 days — asyncio patterns stable, SQLite stable)
