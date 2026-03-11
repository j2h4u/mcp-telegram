# Architecture: v1.1 System Design

**Domain:** Telegram MCP server with observability, caching, and topics
**Researched:** 2026-03-12

## Recommended Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      MCP Client (Claude)                         │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                    MCP Server (stdio)
                           │
     ┌─────────────────────┼─────────────────────┐
     │                     │                     │
  [Tools]            [Telemetry]          [Cache]
     │                     │                     │
ListMessages          Event Queue          Entity Cache
ListDialogs           │                      (SQLite)
SearchMessages        ├─→ [Background Task]  │
GetUserInfo               │                   │
GetMe                     │                   │
GetUsageStats ←───────────┘                   │
     │                                         │
     └─────────────────┬───────────────────────┘
                       │
            ┌──────────┴──────────┐
            │                     │
       entity_cache.db        analytics.db
       (SQLite WAL)           (SQLite WAL)
            │                     │
       ┌────┴────┐           ┌────┴────┐
       │ entities │           │ telemetry
       │ (TTL)    │           │ (30d)
       └──────────┘           └──────────┘
```

## Component Boundaries

| Component | Responsibility | Communicates With |
|-----------|---------------|-------------------|
| **tools.py** | Tool implementations (ListMessages, ListDialogs, etc.) | EntityCache, TelegramClient, Resolver, Formatter, Telemetry |
| **telemetry.py** (new) | Event collection, async queue, async flush | AsyncIO, analytics.db, GetUsageStats tool |
| **cache.py** | Entity metadata caching (v1.0) + indexes (v1.1) | tools.py (lookup), telemetry.py (record metadata) |
| **resolver.py** | Name → ID fuzzy matching (v1.0) + topic scoping (v1.1) | EntityCache, rapidfuzz |
| **formatter.py** | Message formatting (v1.0, unchanged for v1.1) | Message objects from Telethon |
| **server.py** | MCP server setup, tool registration | tools.py, tool_runner dispatcher |
| **Background task** (new) | Periodic telemetry flush to analytics.db | Telemetry queue, analytics.db |

## Data Flow

### Tool Execution (v1.1)
```
User: ListMessages(dialog="Alice", limit=50)
  ↓
[tools.list_messages()]
  ├─ Resolve dialog name → entity_id (Resolver + EntityCache)
  ├─ Fetch messages (Telethon + TelegramClient)
  ├─ Format output (Formatter)
  ├─ Queue telemetry event (Telemetry.append)  ← O(1), non-blocking
  └─ Return TextContent

[Background task] (runs every 60s in parallel)
  ├─ Pop N events from queue
  ├─ Write to analytics.db
  └─ Sleep until next flush
```

### Cache Lookup
```
Resolver.resolve(query="Alice")
  ├─ Get choices from EntityCache.all_names_with_ttl()
  │   └─ Query entity_cache.db WHERE (type='user' AND updated_at >= now-30d)
  │       using index idx_entity_type_name for fast lookup
  ├─ Fuzzy match (rapidfuzz.WRatio)
  └─ Return Resolved | Candidates | NotFound
```

### Telemetry Flush
```
[Every 60s or 100 events]
Background task:
  ├─ Acquire lock on analytics.db
  ├─ INSERT telemetry rows (batch)
  ├─ Release lock
  └─ Mark rows as flushed, clear queue
```

## Patterns to Follow

### Pattern 1: Fire-and-Forget Telemetry (No Blocking)
**What:** Queue telemetry events in memory; background task flushes asynchronously

**When:** All tool calls; every ListMessages, ListDialogs, SearchMessages, GetUserInfo, GetMe records event

**Example:**
```python
async def list_messages(...):
    t0 = time.monotonic()
    result = await fetch_messages()  # Real work
    duration_ms = (time.monotonic() - t0) * 1000

    # Queue event (O(1), never blocks tool response)
    telemetry_queue.append({
        "ts": time.time(),
        "tool_name": "ListMessages",
        "duration_ms": int(duration_ms),
        "success": True,
        "message_count_bucket": bucket_count(len(result))
    })

    return result  # Return immediately; queue will flush async
```

**Why:** Tool responses unaffected by telemetry writes; telemetry survives crash (queue drained when process restarts)

---

### Pattern 2: Separate Metadata from State in Cache
**What:** TTL-based cache for slow-changing metadata; fetch fresh for fast-changing state

**When:**
- **Metadata cache (30d TTL)**: user names, types, usernames (slow to change)
- **State fetch fresh**: unread count, archived flag, reaction count (change frequently or on every call)

**Example:**
```python
# GOOD: metadata cached
def get_entity_name(entity_id):
    cached = cache.get(entity_id, ttl_seconds=30*24*3600)
    return cached["name"] if cached else None  # Name stable; safe to cache

# GOOD: state fresh
async def list_messages(...):
    # Dialog list always fresh (don't cache)
    dialogs = await client.iter_dialogs()

    # Entity names can be cached; resolve happens once
    entity = resolve(dialog_name, cache.all_names_with_ttl(...))

    # Reactions always fresh (change frequently)
    for msg in messages:
        if msg.reactions:
            reactions = await client(GetMessageReactionsListRequest(...))
            # Display fresh count
```

**Why:** Balances API call efficiency (metadata) with correctness (state)

---

### Pattern 3: Topic Names Scoped to Dialog
**What:** Resolver accepts (dialog_name, topic_name) tuple; searches only within dialog's topics

**When:** ListMessages `topic` parameter; any topic resolution

**Example:**
```python
class ListMessages(ToolArgs):
    dialog: str       # Resolved to dialog_id
    topic: str | None = None  # Scoped to resolved dialog
    # ...

async def list_messages(args):
    # Step 1: Resolve dialog
    dialog_id = resolve(args.dialog, ...)

    # Step 2: Resolve topic (scoped to dialog)
    if args.topic:
        topics = await client(channels.GetForumTopicsRequest(channel=dialog_id))
        topic_id = resolve(args.topic, {t.id: t.title for t in topics})

    # Step 3: Filter messages by topic_id
    messages = [m for m in messages if m.reply_to.forum_topic_id == topic_id]
```

**Why:** Prevents ambiguity; multiple dialogs can have topics with same name

---

### Pattern 4: Explicit Error Handling for Telegram API Edge Cases
**What:** Wrap topic/permission-related calls in try-except; fall back gracefully

**When:** GetForumTopicsRequest, permission_denied on deleted topics, private topics

**Example:**
```python
try:
    topics = await client(channels.GetForumTopicsRequest(
        channel=dialog_id,
        limit=100,  # Pagination
        offset_id=0
    ))
except errors.ChannelPrivateError:
    # User removed from channel
    return [TextContent(type="text", text="No access to forum")]
except errors.NotAllowedError:
    # Topics disabled or permissions issue
    return [TextContent(type="text", text="Topics not available in this dialog")]

if args.topic:
    # Topic might be deleted
    topic_ids = [t.id for t in topics if not t.deleted]
    if args.topic not in {t.title for t in topics}:
        return [TextContent(type="text", text=f"Topic '{args.topic}' not found")]
```

**Why:** Telegram API returns errors for deleted/private topics; graceful fallback better than crash

---

## Scalability Considerations

| Concern | At 100 users | At 10K users | At 1M users |
|---------|--------------|--------------|-------------|
| **Entity cache size** | <1KB metadata | <100KB metadata | <10MB metadata (per user) |
| **Telemetry retention** | <1MB (30d at 10 calls/day) | <100MB (30d at 1K calls/day) | Not applicable (single deployment) |
| **Tool latency** | 100-200ms | 150-300ms (more entities to cache) | Not applicable |
| **Dialog list fetch** | 1 RPC (1ms) | 5 RPCs (50ms, 100+ dialogs) | Not applicable |
| **Topic pagination** | 1 RPC (50 topics) | Multiple RPCs (pagination to 500 topics) | Not applicable |

**Notes:**
- mcp-telegram is single-user deployment (Telegram session is single account)
- "N users" here means user's N contacts; scales sublinearly (cached after first fetch)
- Telemetry rotation at 30d prevents unbounded database growth
- No sharding needed; SQLite WAL sufficient for single async client

---

## API Contracts

### New in v1.1

#### GetUsageStats Tool
**Input:**
```
(no parameters)
```

**Output:**
```
TextContent:
"Since midnight: 42 tool calls
ListMessages: 20 times (avg 180ms)
SearchMessages: 15 times (avg 320ms)
ListDialogs: 5 times (avg 80ms)
GetUserInfo: 2 times (avg 150ms)

Cache hit rate: 73% (entity metadata)
Average message batch: 10-100 messages per ListMessages call"
```

**Privacy constraints:**
- ✗ No entity names, IDs, dialog names
- ✗ No per-query metrics or cardinality
- ✓ Event counts only
- ✓ Latency aggregates (average, not per-call)

#### ListMessages Enhancement
**New input parameters (v1.1):**
```python
class ListMessages(ToolArgs):
    dialog: str                    # Existing
    limit: int = 100               # Existing
    cursor: str | None = None      # Existing
    sender: str | None = None      # Existing
    unread: bool = False            # Existing

    from_beginning: bool = False    # NEW: fetch oldest messages first
    topic: str | None = None        # NEW: filter by forum topic name
```

**Output: unchanged** — same TextContent format as v1.0

**Error cases:**
- `from_beginning=true` on non-forum group: works (ignores, oldest messages returned)
- `topic="Support"` on non-forum group: error "This dialog doesn't support topics"
- `topic="Support"` on forum but topic deleted: error "Topic not found"

---

## Database Schemas

### entity_cache.db (v1.0 + v1.1 indexes)

```sql
-- Existing (unchanged)
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,
    name       TEXT NOT NULL,
    username   TEXT,
    updated_at INTEGER NOT NULL
);

-- NEW in v1.1 (indexes for fast lookup)
CREATE INDEX IF NOT EXISTS idx_entity_type_name
    ON entities(type, name);  -- Fast fuzzy match by type + name
CREATE INDEX IF NOT EXISTS idx_entity_username
    ON entities(username);    -- Fast @username lookups
```

### analytics.db (NEW in v1.1)

```sql
CREATE TABLE IF NOT EXISTS telemetry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,              -- time.time()
    tool_name       TEXT NOT NULL,              -- "ListMessages", "SearchMessages", etc.
    duration_ms     INTEGER NOT NULL,           -- Wall-clock time (include network)
    success         INTEGER NOT NULL,           -- 1 (success) or 0 (error)
    message_count_bucket TEXT,                  -- "<10", "10-100", ">100" (nullable)
    created_at      REAL DEFAULT (unixepoch()) -- Auto-set on insert
);

-- Index for range queries (GetUsageStats)
CREATE INDEX IF NOT EXISTS idx_telemetry_ts
    ON telemetry(ts);

-- Index for tool-specific queries ("which tool most used?")
CREATE INDEX IF NOT EXISTS idx_telemetry_tool
    ON telemetry(tool_name);
```

---

## Deployment Considerations

### Single Deployment (mcp-telegram via Docker)
```
├── entity_cache.db      (existing, v1.0)
│   └─ 30KB typical size; <1MB after months
├── analytics.db         (new, v1.1)
│   └─ 1MB typical size (30d retention); grows ~30KB/day
└── mcp-telegram process
    ├─ AsyncIO event loop (tools + background flush task)
    ├─ Telemetry queue (in-memory, <100KB)
    └─ Telethon connection (persistent session)
```

### Telemetry Retention Cleanup
**Mechanism:** Systemd timer + SQL script

```bash
# /etc/systemd/system/mcp-telegram-cleanup.timer
[Timer]
OnBootSec=1h
OnUnitActiveSec=1d
Unit=mcp-telegram-cleanup.service

[Install]
WantedBy=timers.target

---

# /etc/systemd/system/mcp-telegram-cleanup.service
[Service]
Type=oneshot
ExecStart=/usr/bin/sqlite3 \
    /home/j2h4u/.local/state/mcp-telegram/analytics.db \
    "DELETE FROM telemetry WHERE ts < unixepoch() - 30*24*3600; \
     PRAGMA incremental_vacuum(1000);"
```

---

## Sources

- v1.0 ARCHITECTURE.md (base design)
- v1.1 PITFALLS.md (edge cases, concurrency constraints)
- SQLite WAL & concurrency documentation
- Python asyncio patterns (fire-and-forget with create_task)
- Telegram Bot API 7.5 (topics, pagination)
