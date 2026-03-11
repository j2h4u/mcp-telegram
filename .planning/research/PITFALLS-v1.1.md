# Pitfalls Research: v1.1 Additions (Telemetry, Caching, Topics)

**Domain:** Extending Telegram MCP server with observability, cache improvements, and Telegram forum topics
**Researched:** 2026-03-12
**Scope:** v1.1 milestone features — usage telemetry, SQLite optimizations, Telegram topics support
**Overall Confidence:** HIGH (pitfalls rooted in established systems knowledge, Telegram API behavior, and privacy research)

---

## Critical Pitfalls

Mistakes that cause rewrites, correctness violations, or data corruption.

### Pitfall 1: Timing/Cardinality Side-Channel PII Leakage in Telemetry

**What goes wrong:**
Even when you deliberately exclude user IDs and names from telemetry, observers can infer PII via side channels:
- **Timing attacks**: Dialog/message access patterns reveal structure (response time correlates with user behavior)
- **Cardinality attacks**: Hash-anonymized IDs reduce to low-entropy set; counting unique hashes per time bucket leaks user activity profiles
- **Packet size inference**: Network-level observers detect Telegram API request patterns (message fetch size, reaction fetch size)
- **Behavior inference**: Query timing patterns (fast dialog list + slow SearchMessages on specific query = specific conversation location)

**Why it happens:**
- Assumes encryption/anonymization = privacy, ignoring side channels
- Logs don't include PII but structure/timing does
- Single-user deployments (mcp-telegram is single-session) make per-user cardinality = per-user behavior
- Telemetry design optimized for operator debugging, not privacy

**Consequences:**
- Metadata-only inference reconstructs conversations (Whisper Leak attack: 90%+ precision on sensitive topic detection from traffic analysis alone)
- Someone analyzing logs + network traffic reconstructs which dialogs were accessed when
- Breach of privacy invariant without code change (attacker doesn't need database access, just logs)
- LLM context injection: if telemetry is logged to stderr/syslog, prompt injection attacks in one message could include historical telemetry of other messages

**Prevention:**
1. **Never log entity IDs** (user/group/channel IDs, message IDs, cursor state) — even hashed
2. **Avoid behavioral patterns** — don't record (timestamp, tool_name, dialog_name_hash, sender_name_hash) tuples; this reconstructs message graph
3. **Use event classes, not cardinality**: Log "tool_run=ListMessages|ListDialogs|SearchMessages" only, no counts/metrics per query
4. **Bounds-based telemetry**: "messages_returned: <10|10-100|>100" (ranges hide true counts)
5. **Accept staleness**: Aggregate metrics once per hour (batching destroys timing side channels; hourly batching hides minute-scale access patterns)
6. **Design for deletion**: Telemetry must be purge-able (old analytics should be voidable on privacy request; retention policy ≤30 days)
7. **Separate logs from telemetry**: Tool logging goes to stderr (for debugging); telemetry goes to analytics DB (never to logs that might be persisted)

**Detection:**
- Grep for: `entity_id`, `dialog.*id`, `message.*id`, `sender.*id`, `cursor`, `hash(`, in telemetry module
- Search for time-based metrics per-query: if you log (timestamp, query_string), you're vulnerable
- Check if two correlated dialogs show timing patterns (fast/slow) — indicates behavior leakage
- Test: Run two ListMessages calls on same dialog 5 minutes apart; measure response time variance; <5% variance = suspicious (timing is not being recorded)

**Confidence:** HIGH (Whisper Leak & side-channel attack research 2025–2026 published; industry discussion confirms risks)

---

### Pitfall 2: SQLite Concurrency + Telemetry Write Contention

**What goes wrong:**
Adding a second SQLite table (analytics/telemetry) to existing entity_cache.db causes write contention. WAL mode allows concurrent **reads** with writes, but **write transactions still serialize**:
- Entity cache upsert (already happening) + telemetry write = two write txns in quick succession
- Under load (rapid tool calls), write transactions queue and block each other
- Async task that logs telemetry blocks entity cache updates; entity cache upsert blocks telemetry write
- Response latency degrades unpredictably (writer A blocks waiting for writer B's tx to commit)
- Database lock held during JSON serialization (slow), blocking all other writes

**Why it happens:**
- Assumption: SQLite WAL = concurrent writes. **False** — WAL enables concurrent reads *during* write, not concurrent writes
- Single database file = single write lock, regardless of tables
- Async context switches allow multiple tasks to queue write operations before lock release
- Tests pass (no write contention under low load) but production shows latency spikes under concurrent requests
- Developer doesn't profile baseline tool latency, so impact of telemetry writes goes unnoticed until users complain

**Consequences:**
- Tool responses slow down over time (telemetry accumulates rows, writes get slower, more contention)
- Catalog of locking patterns: first GetMe call locks entity cache, telemetry write waits; second tool call waits for telemetry lock; third call waits for entity cache; cascade effect
- Deadlock under high concurrency (unlikely but possible if one write operation tries to read another's table)
- VACUUM operation blocks all writes, causing cascading tool failures
- Production feature works until load test or real deployment; needs to be disabled/ripped out

**Prevention:**
1. **Separate databases**: Entity cache in `entity_cache.db`, analytics in `analytics.db` (two independent write locks; each task holds lock for <1ms)
2. **Async write queue**: Don't await telemetry writes in tool flow; queue events and flush asynchronously (separate task, batched inserts)
   ```python
   # WRONG: blocks tool execution
   async def list_messages(...):
       result = await fetch_messages()
       await telemetry.record_tool_call(...)  # BLOCKS
       return result

   # RIGHT: async queue, flushed separately
   async def list_messages(...):
       result = await fetch_messages()
       telemetry_queue.append({"tool": "ListMessages", ...})  # O(1), no lock
       return result
   ```
3. **Connection pooling per database**: Use `@functools_cache` or context vars to reuse connections within tool call; avoid reconnect overhead
4. **Explicit transaction boundaries**: Minimize transaction scope — upsert entity quickly, batch telemetry writes outside hot path (flush every 60s or 100 events, whichever is sooner)
5. **Test under load**: pytest-benchmark or locust to simulate concurrent tool calls; measure p95 latency with/without telemetry
   - Baseline: `ListMessages` without telemetry writes
   - With telemetry queue (no flush): should be <0.5ms overhead
   - With telemetry flush (separate task): should be <0% overhead to tool latency (asynchronous)

**Detection:**
- Latency tail: p95/p99 latency grows linearly with telemetry row count (profile with `cProfile` or `py-spy`)
- SQLite `.stat` shows journal growth (accumulating locks, checkpoint delays)
- Logs show "database is locked" errors under load (enable WAL logging to debug)
- Benchmark shows telemetry writes add >10% to tool latency; indicates synchronous writes

**Confidence:** HIGH (SQLite concurrency is well-documented; WAL limitations confirmed in official docs; async context switching is Python standard)

---

### Pitfall 3: Cache Invalidation Race — Dialogs Appear/Disappear Unexpectedly

**What goes wrong:**
Dialog list cache (new in v1.1) becomes stale faster than expected:
- User archives a dialog → `ListDialogs` still returns it (cached result, TTL not expired)
- User receives message in new dialog → `ListDialogs` doesn't include it (cache miss, cache not refreshed)
- Reaction count changes → cached reaction data shows old count (no invalidation on reaction updates)
- Sender blocks user → `ListMessages` still lists old sender name (entity cache has stale name, TTL=30d)
- Sender changes name → `ListMessages` shows old name, resolver can't find new name (two staleness sources)

Race condition: Tool A lists dialogs (fills cache), Tool B's Telegram client receives background update (archived dialog), Tool A returns cached list before Tool B's cache invalidation completes.

**Why it happens:**
- TTL-based cache (30d for users, 7d for groups) is designed for metadata, not real-time state
- Dialog list not persisted with timestamp; no way to know if list is stale vs. fresh
- Background updates from Telegram (new messages, deletions, reactions, archiving) don't trigger cache invalidation
- Single-connection Telethon client means updates process sequentially, but cache operations are independent
- Assumption: "Dialog list changes rarely, can cache for days" — false for active users (new dialogs arrive hourly)

**Consequences:**
- LLM acts on outdated dialog list; creates confusion ("I don't see that dialog anymore"; "That dialog shouldn't be here")
- Reaction counts mislead about conversation sentiment; user trusts LLM analysis of old metrics
- Archived dialogs clutter ListDialogs output (privacy regression; should be hidden)
- Cross-tool inconsistency: ListDialogs shows dialog, ListMessages fails (not found after cache refresh)
- Compounding: If resolver caches dialog matches, old cached ID points to wrong entity (e.g., dialog renamed, new dialog created with same name)

**Prevention:**
1. **Never cache dialog list** — fetch fresh on every ListDialogs call (1 RPC, <100ms latency, acceptable)
2. **Cache only entity metadata** (names, types, usernames) — exclude state (archived, unread, reaction count)
3. **Separate caches by change frequency**:
   - **Metadata cache** (TTL=30d): name, type, username — slow to change, safe to cache long
   - **State cache** (TTL=1h or on-demand): unread count, last message, archived flag — always fetch fresh or use short TTL
4. **Reaction cache TTL ≤ message age delta**: If message is <1min old, cache reactions for 10s; messages >1h old can cache longer (reactions stabilize)
5. **Invalidate on write-side**: When you upsert entity cache, set short TTL (5min) for that entity's metadata to force re-fetch on next resolver call
6. **Use `cache.all_names_with_ttl()`**: Current code already does this; don't introduce separate "dialog cache" that bypasses TTL filtering
7. **Document staleness trade-off**: In tool docstring, mention "ListDialogs may lag real-time by <1s if new dialogs arrived; archived dialogs hidden with 1h delay"

**Detection:**
- Test: Archive a dialog, call ListDialogs twice in rapid succession, verify second call shows fresh list (should be cached miss, not hit)
- Compare ListDialogs output to Telegram client UI side-by-side
- Check cache hit rates: if >90% of ListDialogs cache hits, dialog list is cached (wrong); should be <50% (metadata cache hits only)
- Measure response time: if ListDialogs is very fast (<50ms), likely cached; if inconsistent (sometimes 50ms, sometimes 200ms), cache staleness

**Confidence:** HIGH (cache invalidation is classic systems problem; TTL-based caches notoriously unreliable for state; confirmed in distributed systems literature)

---

### Pitfall 4: Telegram Topics API Edge Cases Not Handled

**What goes wrong:**
Forum topics (v1.1 Phase 4 feature) have edge cases that will cause crashes or incorrect output:

1. **Topic 0 (General)**: exists in all forums, non-deletable, but `reply_to.forum_topic_id == 0` in messages — often omitted/None
2. **Deleted topics**: `forumTopicDeleted` flag set; topic still appears in pagination but name is gone; resolving by name fails; should be filtered or labeled "[deleted]"
3. **Topic pagination**: Not covered by `iter_messages()`; requires separate `channels.getForumTopics` call; cursor pagination not compatible with topic iteration
4. **Mixed message types**: Some messages in topic have `reply_to.forum_topic_id`, others don't (off-topic replies, older messages); filtering by topic breaks
5. **Partial topic loading**: Mobile clients (Telegram bot API 7.5, 2025) cache only 50 most recent topics; pagination is required for complete list; analytics will be blind to inactive topics
6. **Topic access control**: User may not see all topics in a group (private topics exist); API returns `permission_denied` on some topic IDs
7. **Topic ID ≠ topic name**: Multiple topics with same name (within same group, unusual but possible); topic names can collide if not namespace-scoped

**Why it happens:**
- Telegram API is complex; topics are relatively new (2024–2025 rollout, still evolving)
- Telethon `iter_messages()` doesn't natively support topic filtering (custom logic required)
- Testing only with non-forum groups or forums without deleted topics misses edge cases
- Assumption: message attributes are always present (forum_topic_id, reply_to) — often None or missing
- Expectation: Telegram API is stable; actually still has rough edges (topic 0 handling varies)

**Consequences:**
- ListMessages crashes if topic name lookup fails (topic deleted after list but before fetch)
- Filtering by topic returns messages from wrong topic (topic 0 bypasses filter, mixing General topic with filtered topic)
- Pagination broken (showing 50 topics when 200 exist; next page doesn't load because ID calculation assumes topic list is complete)
- "Topic not found" errors when user provides topic name (case-sensitive matching fails, or topic name collides)
- Cross-posting detection broken (messages with topic 0 counted as topic-scoped)
- Resolver ambiguity: two topics named "Support"; resolver returns Candidates instead of Resolved

**Prevention:**
1. **Wrap topic iteration in try-except**: `getForumTopics` may fail (deleted topic, permission denied, user removed); fall back to fetch without topic filter
2. **Handle topic 0 explicitly**: In topic filter, if `forum_topic_id == 0` or `None`, decide: include with label "[general]" or exclude with label "[off-topic]"
   - Document behavior in ListMessages docstring
3. **Add topic metadata to output**: Instead of silent filtering, label each message: `[topic: "Support" #42]`
   - Makes it clear when filtering by topic vs. showing all
   - User can verify correct topic in output
4. **Pagination for topics**: Don't assume 50 topics is all; implement topic pagination in GetUsageStats if tool exposes topic stats
5. **Test with real group**: Use forum with 100+ topics, some deleted, some private; verify pagination and filtering
   - Create test group in Telegram; add 5+ topics; delete one; run ListMessages with topic_id=0, 3, 5; verify output
6. **Resolver enhancement**: Topic names scoped to dialog; resolver must resolve (dialog_name + topic_name) tuples, not just topic_name
   - Add `topic` parameter to ListMessages: `topic="Support"` implies "Support topic within resolved dialog"
   - Document that multiple topics can have same name; numeric topic_id is fallback
7. **Cache topic names per-dialog**: Don't reuse topic cache across dialogs; key = (dialog_id, topic_id)

**Detection:**
- Test: Create forum group with 5 topics, delete topic 3, run ListMessages with topic_id=0, 3, 5; verify output doesn't crash and shows "[deleted]" or skips correctly
- Check Telethon logs for failed RPC calls (getForumTopics permission denied)
- Search for hardcoded topic IDs in code (brittle, won't work across deployments)
- Run ListMessages on forum with mixed on-topic and off-topic messages; verify topic 0 handling is correct

**Confidence:** HIGH (Telegram forums documentation 2024–2025; topic edge cases documented in Bot API changelog; confirmed in Telethon issues)

---

### Pitfall 5: GetUsageStats Output Too Noisy or Too Sparse for LLM Consumption

**What goes wrong:**
Telemetry module collects rich statistics, but output format isn't useful for LLM:
- **Too sparse**: Return `{total_tool_calls: 42}` → LLM can't reason about which tools are used or when
- **Too noisy**: Return per-dialog metrics, per-query metrics, reaction distribution, sender statistics → LLM gets overwhelmed, can't extract signal; context window bloated
- **Wrong granularity**: Hour-by-hour breakdown shows trends but hides daily patterns; daily breakdown misses minute-scale issues; 24-hour aggregate hides morning/evening patterns
- **Missing context**: Stats without units or baselines (is "avg latency 50ms" good or bad?)
- **PII trap**: Including top-N dialogs by tool calls reveals usage patterns; anonymized hashes don't help (small cardinality reveals identity)

**Why it happens:**
- Confusion between **monitoring (operator-facing)** and **LLM tool output (Claude-facing)**
- Assumption: more data = more useful (opposite is true for LLMs; context window matters, token efficiency matters)
- Tools designed for humans (dashboards, alerts) don't translate to LLM input (natural language, structured but concise, actionable)
- No feedback loop: nobody asks Claude "What does GetUsageStats output mean?" so poor output ships undetected

**Consequences:**
- LLM ignores GetUsageStats output (too noisy, can't extract actionable info)
- LLM hallucinates about usage patterns (invents correlations from incomplete data)
- Stats are collected but never used (telemetry module becomes pure overhead)
- Next operator tries to improve stats, adds more fields, compounding the problem
- On-disk storage grows but value decreases (more data, less insight)

**Prevention:**
1. **Design for LLM, not humans**: Output should answer one clear question per call (not 10)
2. **Separate concerns**:
   - GetUsageStats (for Claude): High-level patterns only — "tools ranked by calls", "avg response time", "cache hit rate"
   - Logs/metrics (for operator): Detailed breakdown — per-dialog, per-query, time-series (not exposed via tool)
3. **Use natural language units**: Instead of `latency_ms: [50, 120, 75]`, output `"Typical response: <100ms. Occasionally >500ms (5% of calls)"`
4. **Provide context**: "Cache hit rate: 73% (normal for repeated queries; <50% indicates stale TTL or cache disabled)"
5. **Bound privacy risk**: Never include top-N entities by any metric; use aggregate counts instead ("8 dialogs accessed in last hour" not "Dialogs: [foo, bar, baz]")
6. **Make output actionable**: Stats should help Claude decide next action — "Consider caching this dialog (100+ messages)" not "dialog_entropy: 0.83"
7. **Format as natural text**: Avoid JSON dumps; use prose ("Since midnight: 42 tool calls; SearchMessages 20 times, ListMessages 15 times, GetUserInfo 7 times")

**Detection:**
- Parse GetUsageStats output: Is it >500 tokens? Likely too noisy.
- Test with Claude: Ask "What are my most-used tools?" — should answer directly from output
- Review output: Each line should be independently useful; if you need to correlate 3 fields, you've created noise
- Test: Disable some telemetry fields; verify LLM usage doesn't change (those fields weren't used)

**Confidence:** MEDIUM-HIGH (LLM tool design is active research; emerging best practices from Claude, ChatGPT tool design patterns; no established industry standard yet)

---

## Moderate Pitfalls

Mistakes that cause degraded behavior or require recovery.

### Pitfall 6: Telemetry Writes Block Tool Response Latency

**What goes wrong:**
Tool implementations now need to log telemetry events. If telemetry writes are **synchronous** (within the tool execution path):
```python
async def list_messages(...):
    result = await fetch_messages()
    # New: synchronous telemetry write (BAD)
    telemetry.record_tool_call("ListMessages", len(result), time.time() - t0)  # <-- BLOCKS
    return result
```

Under concurrent requests:
- Request A: tool completes, writes telemetry, SQLite lock acquired
- Request B: tool completes, blocks waiting for telemetry lock
- Requests C–Z: queue up, timeout or slow-response degradation

**Why it happens:**
- Simplicity bias: write telemetry immediately after tool execution (easier to reason about causality)
- Assumption: SQLite writes are fast (<1ms) and won't add measurable latency
- No load testing with concurrent tool calls

**Consequences:**
- Response latency increases unpredictably (depends on write order, other requests)
- Users see "slow tool" even though actual work is fast; telemetry write causes delay
- Feature needs to be disabled in production because latency is unacceptable
- P95/P99 latency degrades as database grows (older databases = slower writes, more contention)

**Prevention:**
1. **Asynchronous telemetry**: Queue events in memory, flush in background task (every 60s or 100 events)
2. **Fire-and-forget**: Use `asyncio.create_task()` to start telemetry write without awaiting
   ```python
   async def list_messages(...):
       result = await fetch_messages()
       asyncio.create_task(telemetry.record_tool_call(...))  # Fire and forget
       return result
   ```
3. **Profile baseline**: Measure tool latency without telemetry; measure with sync/async telemetry; verify <10% overhead
4. **Test concurrent load**: `pytest-asyncio` with concurrent tool calls; measure p99 latency
   - Baseline: 100 concurrent calls, no telemetry
   - With async telemetry: should show <5% latency increase
   - With sync telemetry: should show >50% latency increase (detects blocker)

**Detection:**
- Latency increases linearly with telemetry row count (older databases = slower writes)
- Response time variance increases (some requests fast, some slow)
- Database file grows but p95 latency grows faster than row count
- Benchmark: run 100 concurrent tool calls; measure p99 latency; disable telemetry flushes; re-run; compare

---

### Pitfall 7: Entity Cache Stale Name in ListMessages Output

**What goes wrong:**
Entity cache upserts entity names on API fetch. If entity changes name between tool calls:
- First call: fetch_messages() gets sender entity, upsert name="Alice" to cache
- User renames on Telegram: name → "Alice2"
- Second call: ListMessages uses cached name "Alice" (TTL not expired)
- Output shows old name; confuses user

Worse: Resolver uses cache for name matching:
- Sender changes name, old cache entry TTL=30d
- User tries to filter by new name: `sender="Alice2"` → not found in cache → ambiguous/not found
- User confused (just saw "Alice2" in UI, but tool doesn't find them)

**Why it happens:**
- Name changes are infrequent; TTL-based expiry seems reasonable
- Assumption: 30d TTL = acceptable staleness for user names (often false if users are active)
- Cache doesn't track "last name change" — uses generic "updated_at"; staleness is invisible

**Consequences:**
- Output shows wrong names; LLM confused about identity
- Filter by new name doesn't work (not in cache, not in choices dict for resolver)
- Name confusion across tools (ListMessages shows old name, GetUserInfo shows new name)
- Resolver behaves inconsistently (exact match on old name, fuzzy match on new name)

**Prevention:**
1. **Reduce TTL for frequently-accessed entities**: Instead of 30d for all users, use 7d for users who appear in recent messages, 30d for inactive users
2. **Fetch fresh on each tool call**: Don't rely solely on cache for names in output; call get_entity() once per message to get fresh name (cheap, cached at Telethon level)
3. **Resolver enhancement**: If cache miss on name, try fuzzy match on cache + fresh fetch side-by-side; return "maybe updated name" warning
4. **Document limitation**: "Names may lag real-time by <7 days if user changes name and doesn't appear in new messages"
5. **Invalidate on collision**: If fuzzy match returns candidates but exact match fails, invalidate old cache entry (name changed)

**Detection:**
- Test: Change your own username on Telegram, call GetUserInfo twice (5 min apart); verify second call shows new username
- Compare output name to Telegram client UI for same user
- Search for cached entries that haven't been updated recently (>7 days); validate they still exist

---

### Pitfall 8: Reaction Count Cache vs. Actual Reaction Updates

**What goes wrong:**
If reactions are cached per-message, cache becomes stale:
- Message has 3 reactions initially; cached
- User adds 4th reaction → message now has 4 reactions
- ListMessages returns cached reaction count = 3
- User confused (saw 4 in UI, LLM reports 3)

**Why it happens:**
- Reaction fetch is expensive (GetMessageReactionsListRequest per message)
- Caching reactions saves API calls
- But reactions change frequently; cache TTL hard to get right

**Consequences:**
- Stale reaction counts in output
- LLM misses important signal (many reactions = important message)
- Inconsistency between tool output and Telegram UI
- User acts on wrong emotion indicator (e.g., "message is unpopular" when actually popular)

**Prevention:**
1. **Don't cache reactions**: Fetch on every ListMessages call (1 extra RPC per message with reactions)
   - Cost: 1–2 extra API calls per tool call (acceptable, well within Telegram limits)
   - Benefit: Always fresh, no staleness issues
2. **If you must cache**: TTL = 10s (reactions change frequently); even better, 5s
3. **Cache key includes message_id**: Reactions of different messages are independent; don't share cache
4. **Invalidate on mutation**: If user reacts in same session, invalidate cache for that message

**Detection:**
- Test: Add reaction to message, call ListMessages immediately; verify reaction count matches UI
- Add reaction again, call ListMessages; verify count incremented
- Measure time to stale: note reaction count at time T, run tool at T+5s, T+30s; find where staleness occurs

---

## Minor Pitfalls

Mistakes that cause small inconveniences or maintenance burden.

### Pitfall 9: GetUsageStats Requires Manual Cache Lookup

**What goes wrong:**
GetUsageStats queries telemetry database but doesn't cache results. Each call does a full table scan:
```sql
SELECT COUNT(*) as total, COUNT(DISTINCT dialog_id) as unique_dialogs, ...
FROM telemetry WHERE ts >= now() - interval '24 hours'
```
Under repeated calls, database grows and queries slow down.

**Why it happens:**
- First pass: logic is correct, function works
- Production: telemetry table has 100k+ rows; query takes 100ms
- Next call: query still slow (no index, no cache)
- Nobody profiles GetUsageStats because it's rarely called during testing

**Consequences:**
- GetUsageStats becomes slower as telemetry accumulates
- Operator calls it to debug performance, ironically slow
- VACUUM operation blocks all writes while indexing (if no incremental vacuum)

**Prevention:**
1. **Add SQLite indexes**: `CREATE INDEX idx_telemetry_ts ON telemetry(ts)`
   - Allows range query to skip non-matching rows
2. **Cache results in memory**: Last computed result + timestamp; return cached if <60s old
   - In-process cache acceptable (single deployment, single server)
3. **Aggregate on-insert**: Maintain separate `telemetry_summary` table with pre-computed totals (updated on each insert)
   - Trades insert latency for query latency (insert now slower, query now fast)
4. **Analyze query plan**: `EXPLAIN QUERY PLAN` to verify index is used

**Detection:**
- Benchmark: measure GetUsageStats latency over time (should be <50ms; >100ms indicates slow query)
- Check SQLite explain plan: should show `SEARCH telemetry USING idx_telemetry_ts` not `SCAN TABLE telemetry`

---

### Pitfall 10: Analytics Tables Grow Without Cleanup

**What goes wrong:**
Telemetry and topic cache tables grow unbounded. After months, database file is 500MB+ and queries slow down.

**Why it happens:**
- INSERT operations are fast; nobody thinks about cleanup
- DELETE/VACUUM requires exclusive lock (blocks all other operations)
- Assumption: "disk is cheap, queries are indexed"
- No retention policy defined

**Consequences:**
- Database file grows; SQLite file bloat (500MB+ after 6 months)
- Queries slow down (larger dataset, more cache misses)
- VACUUM pauses all operations for minutes (unacceptable during active use)
- Backup/restore times increase
- Old telemetry becomes stale/useless (6-month-old tool call stats irrelevant)

**Prevention:**
1. **Retention policy**: Delete telemetry older than 30 days; delete topic cache older than 7 days
   - Acceptable tradeoff: no long-term telemetry (operator can't query "usage last quarter"), but manageable database size
2. **Automated cleanup**: Separate systemd timer that runs daily at low-activity time (e.g., 3am)
   ```sql
   DELETE FROM telemetry WHERE ts < datetime('now', '-30 days')
   ```
3. **Incremental VACUUM**: Use `PRAGMA incremental_vacuum(1000)` to cleanup 1000 pages at a time (doesn't block)
4. **Monitor file size**: Alert if database > 100MB
5. **Log rotation**: Keep telemetry logs separate from application logs; rotate independently

**Detection:**
- Monitor database file size: should be <50MB; if >100MB, cleanup is overdue
- Check last modified timestamp on analytics.db; if >7 days old without changes, cleanup didn't run

---

### Pitfall 11: Resolver Ambiguity on Topic Name Conflicts

**What goes wrong:**
Two dialogs have topics with the same name:
- Dialog A: topic "Support"
- Dialog B: topic "Support"

User runs `ListMessages(dialog="A", topic="Support")`. Resolver returns "ambiguous support topics" or resolves to wrong one.

**Why it happens:**
- Topic resolution assumes topic names are unique (they're not, across dialogs)
- Resolver doesn't scope topic search to current dialog
- Topic name matching happens at global level, not within-dialog level

**Consequences:**
- Topic filter fails even though topic exists
- LLM gets ambiguous error; can't recover
- Workaround: use topic ID (numeric, not user-friendly)
- User confused (topic clearly exists, why can't you find it?)

**Prevention:**
1. **Scope topic names to dialog**: Resolver accepts (dialog_name, topic_name) tuple; searches only within selected dialog
2. **Document in tool docstring**: "topic= parameter is scoped to the selected dialog"
3. **Numeric topic ID fallback**: "If ambiguous, use topic_id=<number>"
4. **Test with real group**: Create forum with multiple dialogs having same topic names; verify filtering works

**Detection:**
- Test: Create two groups, both with topic "Support"; run ListMessages(dialog="GroupA", topic="Support"); verify returns messages from GroupA's Support topic only
- Verify error message when topic is ambiguous (shouldn't happen if scoped correctly)

---

## Phase-Specific Warnings

| Phase | Topic | Likely Pitfall | Mitigation |
|-------|-------|---|---|
| Phase 1: Telemetry Foundation | PII/side-channel privacy | Pitfall 1, 5 | Design for deletion; no behavioral cardinality; bounds-based metrics; purge-able logs |
| Phase 1: Telemetry Foundation | Performance overhead | Pitfall 6 | Async queue + background flush; benchmark baseline; test concurrent load |
| Phase 2: Cache Improvements | SQLite concurrency | Pitfall 2, 6 | Separate analytics DB; async telemetry queue; load testing with concurrent calls |
| Phase 2: Cache Improvements | Dialog list staleness | Pitfall 3 | Never cache dialog list; cache only entity metadata; separate state from metadata |
| Phase 2: Cache Improvements | Database cleanup | Pitfall 10 | Retention policy ≤30 days; incremental VACUUM; automated daily cleanup |
| Phase 3: Navigation (from_beginning) | Not applicable | None identified | Straightforward parameter addition; no new caching or telemetry |
| Phase 4: Forum Topics | Topic edge cases | Pitfall 4 | Test with real forum groups; handle topic 0, deleted topics, pagination; test permission_denied |
| Phase 4: Forum Topics | Topic resolution ambiguity | Pitfall 11 | Scope topic names to dialog; document numeric ID fallback |
| All phases | Performance regression | Pitfall 5, 6 | Profile before/after; test concurrent load; measure p95 latency; keep telemetry async |
| All phases | Cache key design | Pitfall 7, 8 | TTL strategy depends on change frequency; fetch fresh for frequently-changing state; add indexes |

---

## Summary: Prevention Strategy by Feature

### Telemetry (v1.1 Phase 1)
1. **Privacy-first design**: No IDs, no names, no behavioral cardinality — log event types only (tool_name, outcome class)
2. **Async by default**: Telemetry queue flushed in background; tool responses unblocked
3. **Deletion-first storage**: Telemetry purged after 30 days; on-demand purge for privacy requests
4. **Bounds-based metrics**: Aggregate counts by bins (messages: <10, 10-100, >100) not exact counts
5. **Separate database**: analytics.db independent of entity_cache.db (no write contention)

### Cache (v1.1 Phase 2)
1. **Separate databases**: analytics.db independent of entity_cache.db (no write contention)
2. **TTL strategy**: Metadata (names, types) = 7-30d; state (unread, archived, reactions) = fetch fresh or <1h TTL
3. **No dialog list cache**: ListDialogs always fresh (1 RPC, acceptable latency)
4. **Indexes on hot queries**: Add index on (ts) for telemetry range queries; (entity_id) for entity_cache lookups
5. **Cleanup automation**: Daily delete of telemetry >30d old; incremental VACUUM during off-hours

### Topics (v1.1 Phase 4)
1. **Edge case testing**: Use real forum groups with deleted/private topics; test pagination
2. **Topic 0 handling**: Explicit label/filter for General topic; document behavior
3. **Scoped resolution**: Topics resolved within dialog scope, not globally
4. **Error handling**: Wrap topic API calls in try-except; fall back to unfiltered messages on permission_denied
5. **Topic name collision handling**: Multiple topics with same name are allowed; fallback to numeric ID

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|-----------|-------|
| **Privacy/side-channels** | HIGH | Whisper Leak (2025) and side-channel attacks well-documented; cardinality risks confirmed in industry literature; timing attacks against traffic analysis standard in security research |
| **SQLite concurrency** | HIGH | WAL mode limitations are well-established; concurrent write blocking is documented behavior; confirmed in production systems |
| **Cache invalidation** | HIGH | Classic systems problem; TTL-based caches notorious for staleness; cache invalidation cited as "hardest problem in CS"; confirmed patterns in literature |
| **Telegram topics API** | HIGH | Telegram Bot API documentation detailed (2024–2025); edge cases confirmed in forum implementations; topic 0 behavior documented |
| **LLM telemetry design** | MEDIUM | Emerging area; some guidance from OpenTelemetry; no established best practices for single-agent tools; requires feedback loop with Claude |
| **Performance overhead** | MEDIUM-HIGH | General async instrumentation overhead (1–3%) is documented; specific impact to mcp-telegram needs load testing; SQLite overhead well-understood |

---

## Sources

- [Whisper Leak: Timing Side-Channel Attack on LLM Services](https://arxiv.org/html/2511.03675v1) — LLM traffic analysis, 90%+ precision topic detection
- [Memory Under Siege: Side-Channel Attacks Against LLMs](https://arxiv.org/html/2505.04896v1) — Comprehensive survey of memory/timing attacks
- [Schneier on Security: Side-Channel Attacks Against LLMs](https://www.schneier.com/blog/archives/2026/02/side-channel-attacks-against-llms.html)
- [SQLite Concurrent Writes Documentation](https://www.sqlite.org/lockingv3.html) — Locking and WAL details
- [SQLite Concurrent Writes: Database is Locked Errors](https://tenthousandmeters.com/blog/sqlite-concurrent-writes-and-database-is-locked-errors/)
- [Going Fast with SQLite and Python](https://charlesleifer.com/blog/going-fast-with-sqlite-and-python/) — Best practices for production use
- [Cache Invalidation Nightmare](https://triotechsystems.com/the-cache-invalidation-nightmare-what-youre-likely-doing-wrong/) — Distributed cache problems
- [Three Ways to Maintain Cache Consistency](https://redis.io/blog/three-ways-to-maintain-cache-consistency/) — Event-driven invalidation patterns
- [Telegram Forums API](https://core.telegram.org/api/forum) — Topics, pagination, edge cases, topic 0
- [Telegram Bot API 7.5 (2025)](https://core.telegram.org/bots/api-changelog#march-31-2025) — Topics as first-class citizens, partial topic loading
- [OpenTelemetry Python Instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/) — Async overhead (1–3%)
- [High-Cardinality Observability](https://www.datable.io/post/observability-metrics-cardinality) — Cardinality explosion risks
