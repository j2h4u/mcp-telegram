# Architecture: v1.1 Feature Integration

**Project:** mcp-telegram
**Researched:** 2026-03-12
**Scope:** How new v1.1 components integrate with existing v1.0 architecture; module boundaries; build order

## Executive Summary

v1.1 adds observability (usage telemetry), completeness (forum topics, backward pagination), and cache efficiency improvements. The existing modular architecture (tools.py, cache.py, resolver.py, formatter.py, pagination.py) accommodates these additions without fundamental restructuring.

**Key decisions:**
- **analytics.py** — New SQLite event store (separate database from entity_cache.db, parallel to cache.py's pattern)
- **GetUsageStats** — New MCP tool using singledispatch pattern (follows existing ToolArgs/tool_runner convention)
- **Topic support** — Extend ListMessages with topics parameter; formatter already handles reply_to inspection
- **from_beginning** — Cursor pagination variation; logic already supports reverse iteration

**No breaking changes** to v1.0 tools or APIs. All new parameters optional with sensible defaults.

## Current Architecture (v1.0)

### Module Map
```
server.py (MCP entry point)
├─ tools.py (5 tools: ListDialogs, ListMessages, SearchMessages, GetMyAccount, GetUserInfo)
│  ├─ cache.py (EntityCache SQLite, entity_id→name/type/username, TTL-enforced)
│  ├─ resolver.py (fuzzy name→id, @username lookup, transliteration)
│  ├─ formatter.py (messages→human text, replies, reactions, media)
│  ├─ pagination.py (cursor encode/decode, base64 JSON)
│  └─ telegram.py (Telethon client factory, session persistence)
└─ (prompts, resources, templates empty)
```

### Existing Module Responsibilities

| Module | Responsibility | Persistence | Dependencies |
|--------|----------------|-------------|--------------|
| server.py | MCP protocol dispatch (list_tools, call_tool) | None | tools |
| tools.py | ToolArgs classes + @tool_runner.register handlers (5 tools) | L1: @functools_cache | cache, resolver, formatter, pagination, telegram |
| cache.py | Entity metadata (users/groups/channels) with TTL enforcement | L2: SQLite (WAL, TTL) | None |
| resolver.py | Name→entity_id via fuzzy match (WRatio), @username lookup | None (reads cache) | cache (optional for @username) |
| formatter.py | Message→readable text (headers, gaps, replies, reactions) | None | None |
| pagination.py | Cursor encode/decode (base64 JSON) | None | None |
| telegram.py | TelegramClient factory, session dir, auth loop | Session file on disk | None |

### Message Fetch Strategy
- **Always fresh** from Telegram API (no message caching)
- Entity metadata cached 30d (users), 7d (groups/channels)
- Cache hits only on entity_id→name lookups during formatting/resolution

## v1.1 Additions

### 1. New Module: analytics.py

**Purpose:** Privacy-safe usage telemetry (tool call counts, error rates, performance metrics)

**Location:** `src/mcp_telegram/analytics.py` (parallel to cache.py)

**Design:**
```python
# SQLite schema: events table
# Columns: id (PK), tool_name, query (hash or truncated),
#          duration_ms, error_msg, timestamp

class AnalyticsStore:
    def __init__(self, db_path: Path) -> None:
        """Open analytics SQLite database."""

    def record_event(
        self,
        tool_name: str,
        query: str | None,
        duration_ms: float,
        error: str | None,
    ) -> None:
        """Record a tool call event."""

    def get_stats(
        self,
        tool_name: str | None = None,
        hours: int = 24,
    ) -> dict:
        """Aggregate stats: call count, error count, avg/min/max duration."""
        # Returns dict with keys: calls, errors, avg_ms, min_ms, max_ms

    def close(self) -> None:
        """Close database connection."""
```

**Why separate database (analytics.db ≠ entity_cache.db)?**
- EntityCache: read-heavy (cache hits on every resolve), entity_id→name lookups
- AnalyticsStore: write-only (one INSERT per tool call)
- Mixing tables risks WAL contention (reader lock conflicts with writer lock)
- Different retention policies (entities: 30d users/7d groups; events: 90d for future)
- Separate databases = independent VACUUM schedules

**Integration points:**
- tools.py: Import AnalyticsStore via get_analytics_store() factory (pattern: @functools_cache)
- server.py: Wrap tool_runner call with timing, record_event after response (success or error)

### 2. New Tool: GetUsageStats

**Location:** tools.py, new ToolArgs subclass + dispatch

**Pattern:** Follows existing ToolArgs convention (no special registration needed, auto-discovered by reflect)

```python
class GetUsageStats(ToolArgs):
    """
    Fetch usage telemetry: tool call counts, error rates, performance metrics.
    No arguments required; returns stats for all tools over the last 24 hours.
    """
    tool: str | None = None  # Optional: filter by tool name
    hours: int = 24         # Time window in hours


@tool_runner.register
async def get_usage_stats(args: GetUsageStats) -> t.Sequence[TextContent | ...]:
    """Read analytics store, aggregate and format stats."""
    store = get_analytics_store()
    stats = store.get_stats(tool_name=args.tool, hours=args.hours)
    # Format stats dict as readable text
    return [TextContent(type="text", text=formatted_stats)]
```

**No dialog resolution** (unlike ListMessages/SearchMessages) — pure telemetry read.

### 3. Extended Tool: ListMessages with Topics and from_beginning

**Changes to ListMessages parameters:**

```python
class ListMessages(ToolArgs):
    """
    List messages in a dialog by name. Returns messages newest-first in
    human-readable format (HH:mm FirstName: text) with date headers and
    session breaks.

    Use cursor= with the next_cursor token from a previous response to page
    back in time. Use sender= to filter messages from a specific person.
    Use unread=True to show only messages you haven't read yet.

    New in v1.1:
    - topics=: Filter by forum topic name (supergroup topics enabled)
    - from_beginning=True: Jump to oldest messages instead of newest
    """

    dialog: str
    limit: int = 100
    cursor: str | None = None
    sender: str | None = None
    unread: bool = False
    # NEW:
    topics: str | None = None      # Topic name filter
    from_beginning: bool = False   # Iterate from oldest (False=default: newest)
```

#### Implementation: from_beginning

**Current behavior (v1.0):**
- `reverse=False` in iter_messages (returns newest first)
- cursor pagination goes backward (older messages)

**New behavior (v1.1):**
```python
if args.from_beginning:
    # Start from oldest (smallest message_id)
    iter_kwargs["reverse"] = True
    # No cursor → starts at message_id=1
    # With cursor → starts at specified message_id, goes older
else:
    # Current behavior (default)
    iter_kwargs["reverse"] = False
```

**Why it works:**
- Cursor is opaque (base64 JSON with message_id + dialog_id)
- encode_cursor() doesn't care about iteration direction
- formatter.format_messages() reverses order for display (oldest→newest calendar order)

**No format changes:** Cursor encode/decode unchanged; pagination.py needs no modifications.

#### Implementation: topics

**Telegram architecture:**
- Forum-enabled supergroups have topics (permanent threads)
- Each message has `reply_to.forum_topic = True` if in a topic
- `reply_to.reply_to_top_id = topic_id` (integer)
- Topic name requires separate API call: `GetForumTopicRequest()` or `GetMessages()` with topic ID

**v1.1a (MVP):** Stub only
```python
if args.topics:
    return [TextContent(type="text",
        text=f"Topic filtering not yet implemented (topic='{args.topics}')")]
```

**v1.1b (Full):** Topic filtering
```python
# New cache addition: topics table (or extend entities table with type="topic")
# Resolve topic name → topic_id via fuzzy match
# Filter messages: keep only those with msg.reply_to.reply_to_top_id == topic_id
# Format: prepend topic name to message if in topic
```

**Formatter changes (minor):**
```python
# In format_messages(), check if message has topic:
reply_to = getattr(msg, "reply_to", None)
topic_id = getattr(reply_to, "reply_to_top_id", None) if reply_to else None
if topic_id:
    # Fetch topic name from cache or include in output
    topic_prefix = f"[Topic #{topic_id}] "  # v1.1a
    # topic_prefix = f"[Topic: {topic_name}] " (v1.1b)
```

### 4. Optional Cache Extensions (Deferred to post-v1.1)

**Current schema (entity_cache.db):**
```sql
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,     -- user, group, channel, topic (v1.1b)
    name TEXT NOT NULL,
    username TEXT,
    updated_at INTEGER NOT NULL
);
```

**Optional additions (NOT for v1.1a, consider for v1.1b):**
1. **Indexes:** `(type, updated_at)` for efficient all_names_with_ttl() filtering
2. **Topics table:** `(id, dialog_id, name, updated_at)` for topic resolution
3. **Reactions cache:** emoji→user_name mappings (only if GetMessageReactionsListRequest becomes expensive)

**For v1.1a:** No schema changes. Topics handled inline during message filtering (deferred).

## Integration Points

### Dependency Graph (v1.1)

```
server.py
├─ tools.py (modified)
│  ├─ cache.py (unchanged)
│  ├─ resolver.py (unchanged)
│  ├─ formatter.py (unchanged or minimal changes for topics)
│  ├─ pagination.py (unchanged)
│  ├─ telegram.py (unchanged)
│  └─ analytics.py (NEW, one-way import)
└─ analytics.py (NEW, imported by server.py to wrap call_tool)
```

### Tight Coupling (v1.0, still required)

| From | To | Purpose |
|------|-----|---------|
| tools.py | cache.py | All tools read entity cache for name resolution |
| tools.py | resolver.py | List/Search/GetUserInfo use resolve() |
| tools.py | formatter.py | ListMessages/SearchMessages format output |
| tools.py | pagination.py | ListMessages uses cursor encode/decode |
| tools.py | telegram.py | All tools use Telethon client factory |

### New Loose Coupling (v1.1)

| From | To | Direction | Nature |
|------|-----|-----------|--------|
| server.py | analytics.py | → | Call analytics.record_event() after tool_runner |
| tools.py | analytics.py | → | (Optional future: record from tool runner for custom metrics) |
| analytics.py | tools.py | None | Analytics never imports tools (no circular dep) |

### Zero Coupling (Correct)

- formatter.py, pagination.py remain standalone
- resolver.py doesn't call formatter or pagination
- cache.py doesn't import from tools.py
- analytics.py is write-only leaf (reads nothing, returns dicts)

## Circular Dependency Check

```
✓ server.py → tools.py
✓ server.py → analytics.py (NEW: record events after tool calls)
✓ tools.py → cache.py
✓ tools.py → resolver.py
✓ tools.py → formatter.py
✓ tools.py → pagination.py
✓ tools.py → telegram.py
✓ tools.py → analytics.py (NEW: optional for custom metrics in runner)
✓ analytics.py → (nothing)

✗ No circular imports
✗ No bidirectional dependencies
```

## Recommended Build Order

### Phase 1: Analytics Foundation (Days 1–2)

**Goal:** Silent event recording, ready for GetUsageStats in Phase 2

1. **Create analytics.py**
   - SQLite schema: `events (id, tool_name, query_hash, duration_ms, error_msg, timestamp)`
   - Class: AnalyticsStore with record_event(), get_stats()
   - Database location: `~/.local/share/mcp-telegram/analytics.db`
   - Dependencies: sqlite3 (stdlib), pathlib
   - Tests: Unit tests for schema, aggregation logic

2. **Modify tools.py**
   - Add get_analytics_store() with @functools_cache (pattern: get_entity_cache())
   - Import: from .analytics import AnalyticsStore

3. **Modify server.py**
   - Wrap call_tool() with timing and analytics.record_event()
   - Capture duration_ms, error message if exception
   - Pattern:
     ```python
     t0 = time.monotonic()
     try:
         result = await tools.tool_runner(args)
         elapsed_ms = (time.monotonic() - t0) * 1000
         analytics.record_event(name, str(args), elapsed_ms, None)
         return result
     except Exception as e:
         elapsed_ms = (time.monotonic() - t0) * 1000
         analytics.record_event(name, str(args), elapsed_ms, str(e))
         raise
     ```

**Why this phase first:** Analytics is isolated. No impact on tool behavior. Can ship as "silent observer" before GetUsageStats.

### Phase 2: GetUsageStats Tool (Day 2)

**Goal:** Observable telemetry queryable by LLMs

1. **Modify tools.py — add GetUsageStats class**
   ```python
   class GetUsageStats(ToolArgs):
       """..."""
       tool: str | None = None
       hours: int = 24

   @tool_runner.register
   async def get_usage_stats(args: GetUsageStats) -> ...:
       store = get_analytics_store()
       stats = store.get_stats(tool_name=args.tool, hours=args.hours)
       # Format and return
   ```

2. **Tests:**
   - Mock AnalyticsStore
   - Verify aggregation logic (call counts, error rates, duration stats)
   - Verify filtering by tool name and time window

**Why after Phase 1:** Requires analytics infrastructure to read from.

### Phase 3: ListMessages Extensions (Days 3–4)

**Goal:** Complete navigation (from_beginning) and v1.1a topic stub

1. **Modify tools.py — ListMessages class**
   - Add `topics: str | None = None`
   - Add `from_beginning: bool = False`
   - Update docstring

2. **Modify tools.py — list_messages() runner**
   - Handle from_beginning: `iter_kwargs["reverse"] = True if args.from_beginning else False`
   - Handle topics: (v1.1a) Check if args.topics and return "not yet implemented" message
   - No change to cursor logic

3. **Optional: Minor formatter.py updates**
   - If topics implemented (v1.1b): add topic annotation to message line
   - v1.1a: skip formatter changes

4. **Tests:**
   - Test from_beginning=False (regression: same behavior as v1.0)
   - Test from_beginning=True (reverse=True propagates)
   - Test topics=None (regression: same behavior as v1.0)
   - Test topics="foo" (returns stub message in v1.1a)

**Why last:** Depends on stable analytics + tools.py modifications. Minimal risk. Topics can be phased (v1.1a stub → v1.1b full implementation).

### Phase 4: Optional Post-v1.1 (Not in v1.1a)

**Defer to later milestone:**
- Add indexes to entity_cache.db for all_names_with_ttl() optimization
- Implement full topic filtering and resolution (v1.1b)
- Add VACUUM strategy for analytics.db (monthly cleanup of old events)

## Build Order Dependency Graph

```
Phase 1 (analytics.py, tools.get_analytics_store, server.record_event)
    ↓
Phase 2 (GetUsageStats tool) — reads from Phase 1
    ↓
Phase 3 (ListMessages extensions) — reads from server.py changes (optional analytics integration)
```

**Critical path:** Phase 1 → Phase 2 must be strictly sequential (Phase 2 can't run before Phase 1 schema exists).

**Phase 3 independent:** Can start after Phase 2 completes, doesn't depend on analytics; just adds parameters.

## Data Flow Examples

### Example 1: GetUsageStats (New Tool)

```
LLM: "call GetUsageStats with tool=ListMessages, hours=24"
  ↓
server.py:call_tool("GetUsageStats", {"tool": "ListMessages", "hours": 24})
  ↓
Time: t0 = monotonic()
  ↓
tools.tool_args() → GetUsageStats(tool="ListMessages", hours=24)
tools.get_analytics_store() → singleton AnalyticsStore
tools.tool_runner(args) dispatch → get_usage_stats()
  ↓
analytics.get_stats(tool_name="ListMessages", hours=24)
  ↓ (read only, no event record for GetUsageStats itself to avoid recursion)
  ↓
Result: "ListMessages: 42 calls, avg 1200ms, 2 errors (last 24h)"
  ↓
server.py:analytics.record_event("GetUsageStats", "tool=ListMessages", elapsed_ms=10, error=None)
  ↓
Return to LLM
```

### Example 2: ListMessages from_beginning (Modified)

```
LLM: "list messages in MyDM starting from oldest"
  ↓
tools.list_messages(ListMessages(dialog="MyDM", from_beginning=True, limit=50))
  ↓
Time: t0 = monotonic()
  ↓
Step 1: Resolve "MyDM" → entity_id=987654
Step 2: iter_messages(entity_id=987654, limit=50, reverse=True) [v1.0: reverse=False]
Step 3: Messages come back oldest-first (opposite of v1.0)
Step 4: format_messages() reverses order → calendar-ordered (oldest at top)
Step 5: encode_cursor(messages[-1].id, entity_id) if len==limit
  ↓
Result: 50 oldest messages + next_cursor
  ↓
server.py:analytics.record_event("ListMessages", "dialog=MyDM", elapsed_ms=150, error=None)
  ↓
Return to LLM
```

### Example 3: ListMessages with topics (v1.1a stub)

```
LLM: "list messages in MyGroup with topics=announcements"
  ↓
tools.list_messages(ListMessages(dialog="MyGroup", topics="announcements", ...))
  ↓
Step 1: Resolve "MyGroup" → entity_id=123456
Step 2: Check args.topics:
        if args.topics:
            return [TextContent("Topic filtering not yet implemented")]
  ↓
Return stub message
```

## Code Locations (v1.1)

**Existing v1.0 files (no changes):**
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/cache.py`
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/resolver.py`
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py`
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py`
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/telegram.py`

**Files with modifications:**
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py` — wrap call_tool with analytics
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py` — add GetUsageStats, extend ListMessages

**New files:**
- `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/analytics.py` — AnalyticsStore class

## File Modification Summary

| File | Changes | Lines | Risk |
|------|---------|-------|------|
| server.py | Wrap call_tool() with timing + record_event() | +15–20 | LOW |
| tools.py | Add GetUsageStats class + runner, extend ListMessages, add get_analytics_store() | +80–120 | LOW |
| analytics.py | **NEW** — AnalyticsStore class, SQLite schema | ~150–200 | LOW |
| cache.py | No changes | — | — |
| resolver.py | No changes | — | — |
| formatter.py | No changes (v1.1a), optional topic annotation (v1.1b) | 0 or ~10 | — |
| pagination.py | No changes | — | — |

**Total new code:** ~250–350 lines (analytics.py + tools.py + server.py changes)
**Complexity:** LOW — no algorithmic changes, extensions only
**Breaking changes:** NONE — all new parameters optional, defaults maintain v1.0 behavior
**Backward compatible:** YES — all tools v1.0-compatible; new tools discoverable via reflection

## Integration Risks & Mitigations

### Risk 1: Analytics DB Contention with Entity Cache
**Scenario:** Many concurrent ListMessages calls → AnalyticsStore writes block entity cache reads
**Mitigation:** Separate SQLite databases (analytics.db ≠ entity_cache.db)
**Confidence:** HIGH — WAL mode handles concurrent reads; separation eliminates contention

### Risk 2: Topics Parameter Not Yet Implemented (v1.1a)
**Scenario:** User passes topics= parameter, expects filtering, gets stub message
**Mitigation:** Clear stub message; phased delivery (v1.1a stub → v1.1b full)
**Confidence:** HIGH — documented in ListMessages docstring as "v1.1b feature"

### Risk 3: from_beginning Cursor Pagination Confusion
**Scenario:** Cursor from oldest-first iteration used with newest-first iteration
**Mitigation:** Cursor is opaque + dialog guard; direction is client concern
**Confidence:** HIGH — pagination.py unchanged; cursor format identical

### Risk 4: Analytics Event Recursion (GetUsageStats calling record_event)
**Scenario:** GetUsageStats call creates event → stored in analytics → creates noise
**Mitigation:** Pattern: don't record GetUsageStats calls themselves (read-only telemetry)
**Confidence:** MEDIUM — requires discipline in implementation; document in code comment

## Performance Impact

### Negligible Overhead (v1.0 vs v1.1)

| Metric | v1.0 | v1.1 | Delta |
|--------|------|------|-------|
| ListMessages latency | ~500ms (API) | ~500ms (API) | +0% (analytics write is async/batch) |
| Entity cache hits | ~1ms | ~1ms | 0% (unchanged) |
| Resolver latency | ~2ms (fuzzy) | ~2ms (fuzzy) | 0% (unchanged) |
| Analytics per-call overhead | — | ~1ms INSERT | +0.2% (negligible) |

### Cache Efficiency Gains (Post-v1.1)
- Indexes on entity_cache.db: all_names_with_ttl() could improve from O(n) scan to O(log n) seek
- Deferred to v1.1b or later (no impact on v1.1a schedule)

## Compatibility Matrix

| Feature | v1.0 | v1.1a | v1.1b |
|---------|------|-------|-------|
| ListDialogs | ✓ | ✓ | ✓ |
| ListMessages | ✓ | ✓ (from_beginning new param) | ✓ (topics filtering) |
| SearchMessages | ✓ | ✓ | ✓ |
| GetMyAccount | ✓ | ✓ | ✓ |
| GetUserInfo | ✓ | ✓ | ✓ |
| GetUsageStats | — | ✓ | ✓ |
| Topic filtering | — | (stub) | ✓ |
| from_beginning param | — | ✓ | ✓ |
| Analytics telemetry | — | ✓ | ✓ |

**v1.0 → v1.1a:** Drop-in upgrade, new tools auto-discovered, all v1.0 calls work unchanged
**v1.1a → v1.1b:** Implement topics table + filtering, fully backward compatible

---

## Summary Table: What Changes for v1.1

| Area | Change | Scope | Impact | Build Phase |
|------|--------|-------|--------|-------------|
| New module | analytics.py | Isolated event store | Low (observer pattern) | Phase 1 |
| New tool | GetUsageStats | tools.py + ToolArgs dispatch | Low (read-only) | Phase 2 |
| Extended tool | ListMessages from_beginning | Pagination variation | Low (cursor unchanged) | Phase 3 |
| Extended tool | ListMessages topics | Filter parameter (stub v1.1a) | Low (deferrable) | Phase 3 |
| Server integration | Wrap call_tool + record_event | Timing capture | Low (5–20 lines) | Phase 1 |
| Cache (optional) | Indexes + topics table | Schema optimization | Deferred | Post-v1.1 |

**Overall v1.1 scope:** Extension architecture, no refactoring. All changes additive. Backward compatible.

---

## Appendix: Proof of Concept Flow

v1.1 integration with existing code works because:

1. **ToolArgs dispatch is unchanged** — server.py reflect logic (inspect.getmembers) auto-discovers new GetUsageStats class
2. **Cursor pagination is direction-agnostic** — base64 JSON format doesn't encode iteration direction
3. **Analytics is one-way dependency** — tools → analytics (never reverse)
4. **Cache has clear boundaries** — EntityCache and AnalyticsStore are separate concerns, separate databases
5. **Formatter is pure** — no state mutations; topic annotation is optional output decoration

This is why v1.1 can be built incrementally without touching resolver.py or formatter.py (until v1.1b for topics).
