# Feature Landscape: v1.1 (Observability & Completeness)

**Domain:** Telegram MCP server observability and feature completeness
**Researched:** 2026-03-12

## Table Stakes

Features users expect. Missing = product feels incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| **GetUsageStats** | LLM needs visibility into own behavior; "how many messages have I read?" | Medium | Must be low-noise, actionable; privacy-first (no PII) |
| **ListMessages pagination (from_beginning)** | Users need bidirectional navigation (backward through history); "oldest messages in dialog" | Low | Straightforward parameter; uses existing Telethon iter_messages(reverse=True) |
| **Forum topics support** | Modern Telegram groups use topics; hiding them = broken UX; "read #support channel in GroupName" | Medium | Edge cases complex (topic 0, deleted topics, pagination); straightforward once enumerated |
| **Cache correctness** | After days of use, tool output shouldn't show stale data (wrong names, old reaction counts) | Medium | Not visible until user reports inconsistency; critical for trust |

## Differentiators

Features that set product apart. Not expected, but valued.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Privacy-by-design telemetry** | LLM observability without side-channel PII leakage | High | Few MCP servers have thought through timing/cardinality attacks; shows rigor |
| **Async-first instrumentation** | Telemetry has zero latency impact on tool calls | Medium | Non-obvious; most services block on observability; pattern differentiates |
| **Topic-aware navigation** | ListMessages understands forum structure; "read 5 messages from #support topic" | Medium | No other Telegram MCP server handles topics; Telegram topics are growing feature |

## Anti-Features

Features to explicitly NOT build.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| **Long-term analytics retention** (>90 days) | Operational burden (database bloat, slow queries); privacy risk (old data = richer inference surface) | Retention policy: 30 days; operator exports summaries if needed (not tool responsibility) |
| **OpenTelemetry SDK integration** | Adds observability "framework" complexity; mcp-telegram is single deployment (no distributed tracing benefit) | Keep telemetry simple: SQLite events + GetUsageStats tool; operator integrates with Grafana Cloud if needed |
| **Real-time webhook notifications** | Out of scope (polling model only); adds complexity without benefit to read-only tool set | Users poll GetUsageStats themselves; MCP server doesn't push updates |
| **Telemetry export to external service** | Couples mcp-telegram to operator's infrastructure; adds privacy/security surface | Telemetry stays local; operator uses SQL to query analytics.db if needed |
| **Message content caching** | v1.0 constraint; messages always fresh (privacy + accuracy) | Keep this constraint; telemetry only tracks metadata (tool name, count, latency) not content |
| **Write-side telemetry** | Out of scope (read-only by design) | Telemetry only tracks read operations (ListDialogs, ListMessages, SearchMessages, GetUserInfo, GetUsageStats) |

## Feature Dependencies

```
GetUsageStats
├─ telemetry collection infrastructure (all tools record basic events)
├─ analytics.db (separate from entity_cache.db)
└─ async flush mechanism

ListMessages(from_beginning=true)
├─ (no dependencies; orthogonal parameter)
└─ existing Telethon iter_messages() with reverse=True

ListMessages(topic="Support")
├─ Telegram forums API (channels.getForumTopics)
├─ topic metadata cache (name, ID, deleted_flag)
└─ resolver enhancement (scope topic names to dialog)

Cache improvements
├─ SQLite indexes (entity_type, username, ts)
├─ analytics.db (separate database, no contention)
└─ retention policy / automated cleanup

Privacy-safe telemetry
├─ no behavioral cardinality (no per-dialog counts)
├─ async queue (fire-and-forget, no blocking)
└─ bounds-based metrics ("10-100 messages", not exact count)
```

## MVP Recommendation

Prioritize:
1. **GetUsageStats + telemetry infrastructure** (Phase 1) — Foundation for observability; enables all downstream features
2. **Cache improvements** (Phase 2) — Correctness fix; prevents silent data staleness bugs
3. **ListMessages(from_beginning)** (Phase 3) — Quick win; straightforward feature addition
4. **ListMessages(topic=)** (Phase 4) — Feature completeness; Telegram topics becoming standard

Defer:
- ~~Long-term retention~~ (anti-feature; 30d is sufficient)
- ~~OpenTelemetry integration~~ (anti-feature; too heavyweight)
- ~~Webhook notifications~~ (out of scope; polling model)

## Feature Phasing

### Phase 1: Telemetry Foundation
**Requirement:** "Usage telemetry module (SQLite, behavioral events only, zero PII) + GetUsageStats tool"

**Definition of Done:**
- [ ] analytics.db created on first startup
- [ ] Telemetry events: tool_name (required), duration_ms (required), success (required), message_count_bucket (optional for ListMessages/SearchMessages)
- [ ] Async flush mechanism: queue events, flush every 60s or 100 events
- [ ] GetUsageStats tool returns: "Since midnight: 42 tool calls; ListMessages 20 times, SearchMessages 15 times, GetUserInfo 7 times" (natural language, <100 tokens)
- [ ] No PII in telemetry (no IDs, no names, no behavioral cardinality)
- [ ] Retention policy: delete telemetry >30 days old
- [ ] 57 tests green (add telemetry tests, existing tests still pass)

**Privacy constraints verified:**
- [ ] Grep: no entity IDs, dialog IDs, message IDs, hashed IDs in telemetry module
- [ ] No per-query cardinality (no "unique dialogs accessed" metrics)
- [ ] Timing metrics only at high level (total duration, not step-by-step breakdown)

### Phase 2: Cache Improvements
**Requirement:** "Cache improvements: SQLite indexes, dialog list cache, reaction cache, VACUUM strategy"

**Definition of Done:**
- [ ] analytics.db has index on (ts) for range queries
- [ ] entity_cache.db has index on (type, name) for resolver fuzzy match
- [ ] entity_cache.db has index on (username) for @username lookups
- [ ] Dialog list always fetched fresh (never cached in-memory)
- [ ] Reaction count fetched fresh on every ListMessages call (not cached)
- [ ] Automated cleanup: systemd timer runs daily, deletes telemetry >30d old, runs incremental VACUUM
- [ ] Database size monitoring: alert if >100MB
- [ ] Load test: 100 concurrent ListMessages calls, p99 latency <500ms with telemetry enabled
- [ ] 57+ tests green (add cache + concurrency tests)

### Phase 3: Navigation (from_beginning)
**Requirement:** "ListMessages navigation: from_beginning=true parameter (jump to oldest messages)"

**Definition of Done:**
- [ ] ListMessages accepts optional `from_beginning: bool` (default=False)
- [ ] When from_beginning=True, passes reverse=True to Telethon (oldest messages first)
- [ ] Cursor pagination works with from_beginning (encode max_id correctly for reverse iteration)
- [ ] Docstring updated: "from_beginning=True returns messages oldest-first; pagination cursor stable"
- [ ] 57+ tests green (add from_beginning edge cases)

### Phase 4: Forum Topics
**Requirement:** "Forum topics support in ListMessages (filter by topic, show topic name)"

**Definition of Done:**
- [ ] ListMessages accepts optional `topic: str | None` (default=None, scoped to selected dialog)
- [ ] Resolver: (dialog_name, topic_name) → topic ID; handles ambiguity with fallback to numeric ID
- [ ] When topic specified, filter messages by reply_to.forum_topic_id
- [ ] Handle topic 0 (General): explicit label "[general]" in output or exclude (documented)
- [ ] Handle deleted topics: catch permission_denied, fall back to unfiltered messages with warning
- [ ] Test with real forum group: 100+ topics, some deleted, pagination works
- [ ] Docstring updated: "topic= parameter scoped to dialog; topic names can collide, use topic_id=<number> if ambiguous"
- [ ] 57+ tests green (add topic edge case tests)

## Success Metrics

| Metric | v1.0 Baseline | v1.1 Target | How Measured |
|--------|---------------|-------------|--------------|
| P95 tool latency | 200ms | <250ms | pytest-benchmark, 100 concurrent calls |
| Cache hit rate (entity metadata) | 60% | 70% | Telemetry analysis (Phase 1) |
| Stale data incidents | (unknown) | <1/month | User reports + GetUsageStats consistency checks |
| Time to debug performance issue | >30min | <10min | GetUsageStats gives visibility into tool call patterns |
| Topics support | 0% | 100% (forums tested) | Test coverage + manual forum testing |

## Sources

- v1.0 PROJECT.md (feature baseline)
- v1.1 PITFALLS.md (anti-features, edge cases)
- Telegram Bot API 7.5 (2025) — Topics feature completeness
