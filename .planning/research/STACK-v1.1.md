# Stack Research: v1.1 Features (Telemetry, Forum Topics, Cache Indexes)

**Project:** mcp-telegram
**Milestone:** v1.1 Observability & Completeness
**Researched:** 2026-03-12
**Confidence:** HIGH (Telethon APIs verified against official docs, telemetry patterns from industry standards, SQLite optimizations documented)

---

## Executive Summary

v1.1 adds three capability layers to the existing Python+Telethon+SQLite stack:

1. **Privacy-safe telemetry** — SQLite event logging (no PII, behavioral metrics only)
2. **Forum topics support** — Telegram supergroup topics navigation via `MessageReplyHeader.forum_topic` + `GetForumTopicsRequest`
3. **Cache efficiency** — SQLite indexes on frequently-queried columns (`type`, `updated_at`, `username`)

**No heavy new dependencies required.** All three layers use Python stdlib (logging, sqlite3) + existing Telethon APIs. No new external packages needed beyond current stack.

---

## Validated Stack (v1.0 — Keep Unchanged)

| Component | Version | Purpose |
|-----------|---------|---------|
| Python | 3.13 | Runtime (pinned in .python-version) |
| Telethon | >=1.23.0 (current: 1.42.0) | Telegram MTProto client |
| MCP SDK | >=1.1.0 (current: 1.26.0) | Protocol + Server class |
| Pydantic v2 | >=2.0.0 | Tool schemas + validation |
| rapidfuzz | >=3.14.3 | Name fuzzy matching (WRatio) |
| SQLite | stdlib (sqlite3) | Entity cache, telemetry storage |

**No changes needed.** These remain the foundation.

---

## New Features: Stack Requirements

### 1. Telemetry Module (Privacy-Safe Event Logging)

**What:** SQLite table for behavioral events (tool calls, resolver actions, API latency) — zero PII stored.

**Stack additions:** None. Use Python stdlib `logging` + `sqlite3`.

#### Schema Design

```sql
CREATE TABLE IF NOT EXISTS telemetry_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp INTEGER NOT NULL,                    -- Unix timestamp
    event_type TEXT NOT NULL,                      -- 'tool_call', 'resolver_action', 'cache_hit', 'api_latency'
    tool_name TEXT,                                -- 'ListMessages', 'SearchMessages', etc. (NULL for resolver events)
    action TEXT,                                   -- 'resolve_success', 'resolve_ambiguous', 'cache_miss', etc.
    duration_ms REAL,                              -- API/operation latency (NULL if not applicable)
    cache_hit INTEGER,                             -- 0=miss, 1=hit (NULL if not applicable)
    resolver_score REAL,                           -- WRatio score if resolve event (NULL otherwise)
    metadata TEXT                                  -- JSON string for structured fields (see below)
);

CREATE INDEX idx_telemetry_timestamp ON telemetry_events(timestamp);
CREATE INDEX idx_telemetry_event_type ON telemetry_events(event_type);
CREATE INDEX idx_telemetry_tool_name ON telemetry_events(tool_name);
```

#### Metadata Field (JSON)

Store additional structured data as JSON in `metadata` column to avoid schema bloat:

```json
{
    "dialog_count": 42,
    "messages_per_page": 10,
    "query_length": 15,
    "candidate_count": 3,
    "threshold_band": "AMBIGUOUS",
    "feature": "forum_topic",
    "error_type": null
}
```

#### Event Types

**Tool Call Events:**
- Event: `tool_call`, Action: `ListMessages`, Duration: actual API time

**Resolver Events:**
- Action: `resolve_single` (auto-resolved, score ≥90)
- Action: `resolve_ambiguous` (3-5 candidates, 60-89 band)
- Action: `resolve_failed` (no match <60)
- Includes: `resolver_score`, `candidate_count`

**Cache Events:**
- Event: `cache_access`, Action: `hit` or `miss`, Includes: `cache_hit` boolean

**API Latency Events:**
- Event: `api_latency`, Tool: `ListMessages`, Duration: milliseconds

#### Why This Design

- **Privacy:** No message content, sender IDs, contact names — only behavior metrics
- **Queryable:** SQL allows aggregation: "avg latency per tool", "resolver success rate", etc.
- **Extensible:** `metadata` JSON avoids schema changes for one-off fields
- **WAL-safe:** Lightweight writes don't block reads (important for concurrent access)
- **Retention:** Can prune events older than N days independently

#### Integration Point

New `GetUsageStats` tool reads telemetry_events:

```python
class GetUsageStats(ToolArgs):
    """Return usage statistics over the last N days."""
    days: int = 7
```

Returns:
```json
{
    "period_days": 7,
    "tool_call_count": 42,
    "avg_resolver_success_rate": 0.91,
    "avg_api_latency_ms": 234.5,
    "cache_hit_rate": 0.78,
    "top_tools": ["ListMessages", "SearchMessages"],
    "timestamp_generated": "2026-03-12T10:30:00Z"
}
```

**No external dependency.** Logging via stdlib `logging` module, storage via sqlite3.

---

### 2. Forum Topics Support

**What:** Enhanced `ListMessages` to filter/display messages from specific forum topics in supergroups.

**Stack additions:** None. Telethon 1.42 already provides `MessageReplyHeader.forum_topic` flag + `GetForumTopicsRequest` RPC.

#### Telethon API Surface

**Message reply structure in v1.42:**

```python
# From Telethon message.reply_to (MessageReplyHeader)
message.reply_to.forum_topic          # bool: True if this message is a reply in a topic
message.reply_to.reply_to_top_id      # int: the topic ID (when forum_topic=True)
```

**GetForumTopicsRequest parameters:**

```python
from telethon import functions

result = await client(functions.channels.GetForumTopicsRequest(
    channel=supergroup_entity,
    q="",                              # search query (optional)
    offset_date=0,                     # pagination start time
    offset_id=0,                       # pagination start message ID
    offset_topic=0,                    # pagination start topic ID
    limit=100,                         # max topics to return
))

# result: messages.ForumTopics
# result.topics: list of ForumTopic objects
# Each ForumTopic has: id, date, title, icon_color, top_message, etc.
```

**ForumTopic fields (subset relevant to display):**

- `id` — topic ID (matches `message.reply_to.reply_to_top_id`)
- `title` — topic name ("General", "Off-topic", etc.)
- `top_message` — most recent message in topic
- `closed` — whether topic is archived
- `pinned` — whether topic is pinned in list

#### ListMessages Enhancement

**New optional parameters:**

```python
class ListMessages(ToolArgs):
    dialog: str
    limit: int = 10
    from_beginning: bool = False       # [NEW] jump to oldest messages
    topic_id: int | None = None        # [NEW] filter by forum topic
```

**Implementation strategy:**

1. When `topic_id` is provided:
   - Validate that the dialog is a supergroup with topics enabled (verify via GetForumTopicsRequest)
   - Fetch the topic metadata to display topic name in output
   - Filter messages: `message.reply_to.forum_topic == True AND message.reply_to.reply_to_top_id == topic_id`

2. When `topic_id` is None:
   - Include messages from all topics (existing behavior for non-topic chats)

3. Display format:
   ```
   [Topic: "General"]
   From: Alice
   Date: 2026-03-12 10:30:00
   Text: Hello everyone
   ---
   From: Bob
   Date: 2026-03-12 10:35:00
   Text: Hi Alice
   ```

#### Why No New Dependency

- Telethon 1.42 already includes `GetForumTopicsRequest` RPC
- `MessageReplyHeader.forum_topic` and `reply_to_top_id` already available
- Topic filtering is a simple boolean check in Python
- Cache can store topic names (entity_type='topic') using existing EntityCache with a new `topic_id` column

#### Cache Schema Update

Add to existing `entities` table:

```sql
ALTER TABLE entities ADD COLUMN IF NOT EXISTS topic_id INTEGER;
-- Allows caching topics by numeric ID (e.g., topic_id=5 → title="General")
```

Or create separate table:

```sql
CREATE TABLE IF NOT EXISTS forum_topics (
    topic_id INTEGER PRIMARY KEY,
    supergroup_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    closed INTEGER DEFAULT 0,          -- 0=open, 1=closed
    pinned INTEGER DEFAULT 0,          -- 0=not pinned, 1=pinned
    updated_at INTEGER NOT NULL
);

CREATE INDEX idx_forum_topics_supergroup ON forum_topics(supergroup_id);
```

**Recommendation:** Separate table. Topics are scoped to supergroups; mixing with user/group/channel entities complicates queries.

---

### 3. Cache Indexes (Entity Cache Performance)

**What:** SQLite indexes on high-cardinality, frequently-filtered columns in EntityCache.

**Stack additions:** None. Standard SQLite DDL.

#### Current Schema (v1.0)

```sql
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT,
    updated_at INTEGER NOT NULL
);
```

**Usage patterns:**
- Lookup by ID (PRIMARY KEY already indexed) ✓
- Lookup by username (resolver.py line ~120)
- Filter by type (entity_type='user' vs 'group' vs 'channel')
- TTL check on updated_at (cache.py line 81)

#### Recommended Indexes

```sql
-- Existing: PRIMARY KEY on id (automatic)

-- NEW: Username lookup
CREATE INDEX IF NOT EXISTS idx_entities_username ON entities(username);
-- Speeds: get_by_username() method

-- NEW: Type-based queries + TTL
CREATE INDEX IF NOT EXISTS idx_entities_type_updated ON entities(type, updated_at);
-- Speeds: all_names_with_ttl() filtering by (type='user' AND updated_at >= ?)

-- OPTIONAL: Name-based search (if added in future)
-- CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
-- For now: defer, not currently used in resolvers
```

#### Implementation

```python
# In cache.py __init__
_DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT,
    updated_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_entities_username ON entities(username);
CREATE INDEX IF NOT EXISTS idx_entities_type_updated ON entities(type, updated_at);
"""
```

#### Maintenance

**PRAGMA optimize** before close (SQLite 3.18+):

```python
# In cache.py close()
def close(self) -> None:
    """Close the database connection."""
    self._conn.execute("PRAGMA optimize")  # Gather statistics for future query planning
    self._conn.close()
```

**Periodic VACUUM** (optional):

```bash
# Defragment database every few days
sqlite3 ~/.local/share/mcp-telegram/entity_cache.db "VACUUM;"
```

#### Performance Impact

- **Lookup by username:** O(log N) instead of O(N)
- **TTL filtering:** Index scan instead of full table scan
- **Storage overhead:** ~5-10% per index (acceptable for entity metadata tables)
- **Write overhead:** Minimal (updates are same O(1) + O(log N) index update)

**Benchmark targets:**
- Entity cache size: ~100-1000 entities (users + groups + channels)
- Index overhead: <1MB disk
- Query latency: <1ms (with or without index, but consistency improves)

---

## API Reference: Telethon Forum Topics

### GetForumTopicsRequest

```python
from telethon import functions, types

# Fetch topics from a supergroup
result = await client(functions.channels.GetForumTopicsRequest(
    channel=supergroup_entity,       # types.Channel with broadcast=False
    q="search query",                # optional: filter by title
    offset_date=0,                   # pagination: timestamp
    offset_id=0,                     # pagination: message ID
    offset_topic=0,                  # pagination: topic ID (use for next page)
    limit=100                        # max topics per page
))

# result: messages.ForumTopics (with topics, count, read_state fields)
# result.topics: list[ForumTopic]
```

### ForumTopic Fields

```python
topic: ForumTopic
topic.id                      # int — unique within supergroup
topic.date                    # datetime — last activity
topic.title                   # str — topic name
topic.icon_color              # int — UI color (RGB)
topic.icon_emoji_id           # int | None — emoji ID if custom icon
topic.top_message             # int — most recent message ID
topic.closed                  # bool — archived state
topic.pinned                  # bool — pinned in topic list
topic.my                      # bool — created by current user
topic.hidden                  # bool — hidden from non-moderators
topic.short                   # bool — abbreviated version (when paginating)
```

### Message.reply_to for Topics

```python
# In ListMessages output
message.reply_to.forum_topic      # bool: True if replying in a topic
message.reply_to.reply_to_top_id  # int: topic ID (matches ForumTopic.id)

# Filter implementation
if message.reply_to and message.reply_to.forum_topic and message.reply_to.reply_to_top_id == topic_id:
    # This message is in the requested topic
    pass
```

**Caveat:** `reply_to_top_id` exists only when `forum_topic=True`. Always check `forum_topic` flag first.

### Why No New Dependency

- `GetForumTopicsRequest` shipped with Telethon 1.23+ (available since v1.0 base)
- `MessageReplyHeader` and `forum_topic` flag available in current Telethon schema
- Filtering is pure Python logic, no external library needed
- Telethon already imports all necessary types (no additional `tl.functions` imports needed)

---

## Installation & Deprecations

### No New Dependencies

v1.1 **does not add** any external Python packages. Existing `pyproject.toml` is sufficient:

```toml
dependencies = [
  "mcp>=1.1.0",
  "telethon>=1.23.0",
  "pydantic>=2.0.0",
  "pydantic-settings>=2.6.0",
  "typer>=0.15.0",
  "xdg-base-dirs>=6.0.0",
  "rapidfuzz>=3.14.3",
]
```

**No changes needed.** All three v1.1 features (telemetry, topics, cache indexes) use stdlib + existing packages.

### Python Version Compatibility

- **Python 3.13:** sqlite3 module unchanged (compatible)
- **logging module:** Unchanged since 3.8 (stable)
- **Telethon 1.42:** Compatible with Python 3.8-3.13

**No version bumps required.**

---

## Architecture: Integration Points

### Telemetry Integration

**Where to instrument:**

1. **tools.py** — Wrap `tool_runner` dispatcher to log tool_call events
2. **resolver.py** — Log resolver_action events (success/ambiguous/failed)
3. **cache.py** — Log cache_hit/cache_miss on `get()` method
4. **connected_client context** — Log api_latency from connect/disconnect timings

**New module:** `telemetry.py`

```python
# telemetry.py
class TelemetryCollector:
    def __init__(self, db_path: Path):
        self._conn = sqlite3.connect(str(db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_DDL)
        self._conn.commit()

    def log_tool_call(self, tool_name: str, duration_ms: float, metadata: dict = None):
        # Write telemetry_events row
        pass

    def log_resolver_action(self, action: str, score: float, candidate_count: int = None, metadata: dict = None):
        pass

    def get_usage_stats(self, days: int = 7) -> dict:
        # Query aggregates for GetUsageStats tool
        pass
```

**Invocation:**

```python
# In tools.py tool_runner
telemetry = get_telemetry_collector()  # Singleton per process
telemetry.log_tool_call("ListMessages", duration_ms, {"messages_per_page": 10})

# In resolver.py
telemetry.log_resolver_action("resolve_single", score=95.5)
```

### Forum Topics Integration

**Where to implement:**

1. **tools.py ListMessages class** — Add `topic_id: int | None = None` parameter
2. **Telethon fetch** — Call GetForumTopicsRequest when `topic_id` is set (validate supergroup)
3. **Cache** — Add `Topic` entity type (or separate `forum_topics` table)
4. **Formatter** — Display topic name in message output header

**New method in tools.py:**

```python
async def _get_topic_title(client: TelegramClient, supergroup_entity, topic_id: int) -> str:
    """Fetch forum topic title from cache or API."""
    cache = get_entity_cache()
    cached = cache.get_forum_topic(supergroup_id=supergroup_entity.id, topic_id=topic_id)
    if cached:
        return cached['title']

    result = await client(functions.channels.GetForumTopicsRequest(
        channel=supergroup_entity,
        limit=1,  # Optimize: could also fetch batch if multiple topics
    ))
    for topic in result.topics:
        if topic.id == topic_id:
            cache.upsert_forum_topic(supergroup_entity.id, topic.id, topic.title)
            return topic.title

    return f"Topic #{topic_id}"  # Fallback
```

### Cache Indexes Integration

**Where to apply:**

1. **cache.py __init__** — Update _DDL with new indexes
2. **cache.py close()** — Add `PRAGMA optimize` before shutdown
3. **No other changes** — Indexes are transparent to read/write logic

---

## Testing Strategy

### Telemetry Tests

- Mock TelemetryCollector, verify log calls with correct params
- Query telemetry_events table, check aggregates (count, avg latency, etc.)
- Verify no PII leaked (no message content, sender IDs, etc.)

### Forum Topics Tests

- Mock GetForumTopicsRequest, verify it's called when `topic_id` is set
- Verify message filtering: only messages with matching `reply_to.reply_to_top_id`
- Verify topic name display in output
- Edge case: supergroup without topics enabled (should skip filtering)

### Cache Index Tests

- Measure query latency with/without indexes (EXPLAIN QUERY PLAN)
- Verify indexes don't break insert/update performance
- Confirm PRAGMA optimize succeeds without errors

---

## What NOT to Add (Deferred or Out of Scope)

| Feature | Rationale |
|---------|-----------|
| **Message content caching** | Messages always fetched fresh; caching content has staleness/consistency risk |
| **Real-time telemetry export** | Polling model only; no background thread for streaming telemetry to external service |
| **Encryption at rest for telemetry** | Privacy is behavioral-only; no user data to encrypt. Filesystem permissions sufficient |
| **OpenTelemetry integration** | Adds dependency (opentelemetry-api); stdlib logging sufficient for this scope |
| **Automatic telemetry pruning** | Manual `VACUUM` command sufficient; don't add background job yet |
| **Topic emoji rendering** | `icon_emoji_id` available but rendering requires external emoji API; skip for v1.1 |
| **Bulk GetForumTopicsRequest** | Current async/await pattern works; no need for batch RPC yet |
| **Transliterate for topic titles** | Topics are always in their native script; no need for transliteration |

---

## Sources

- [Telethon 1.42.0 Client API](https://docs.telethon.dev/en/stable/modules/client.html) — HIGH confidence
- [Telethon TL Schema: GetForumTopicsRequest](https://tl.telethon.dev/methods/channels/get_forum_topics.html) — HIGH confidence
- [Telethon TL Schema: MessageReplyHeader](https://tl.telethon.dev/constructors/message_reply_header.html) — HIGH confidence
- [Telethon TL Schema: ForumTopic](https://tl.telethon.dev/constructors/forum_topic.html) — HIGH confidence
- [SQLite Index Best Practices](https://sqlite.org/optoverview.html) — HIGH confidence
- [Python 3.13 sqlite3 docs](https://docs.python.org/3/library/sqlite3.html) — HIGH confidence
- [Python logging best practices](https://docs.python.org/3/howto/logging-cookbook.html) — HIGH confidence
- Existing codebase: cache.py, tools.py, telegram.py (direct verification) — HIGH confidence

---

**Stack Research for:** mcp-telegram v1.1 — Telemetry, Forum Topics, Cache Optimization
**Researched:** 2026-03-12
**Next Steps:** Phase 6 creates REQUIREMENTS.md detailing schema, tool args, and integration points.
