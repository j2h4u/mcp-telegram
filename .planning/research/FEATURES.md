# Feature Landscape: mcp-telegram v1.1

**Domain:** Telegram MCP bridge (read-only, stdio, Telethon-based)
**Researched:** 2026-03-12
**Overall Confidence:** MEDIUM (Telethon topic support unconfirmed in testing; telemetry patterns from industry standards)

## Table Stakes

Features users expect. Missing = product feels incomplete.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Dialog/message read access | Core value proposition | ✓ Existing | ListDialogs, ListMessages ship in v1.0 |
| Name-based resolution (no IDs) | LLM usability — zero cold-start friction | ✓ Existing | Fuzzy match + cache in v1.0 |
| Pagination without duplicates | Fetch large conversation histories without repeating messages | Medium | Cursor pagination shipped; from_beginning variant needed |
| Reply chain context | Understand message context via parent message lookup | ✓ Existing | reply_map built in ListMessages (v1.0) |
| Reaction metadata | Understand user engagement (emoji, count) | ✓ Existing | Reaction names cached in ListMessages when total ≤ 15 (v1.0) |
| Private contact info access | Understand communication graph (e.g., "who do these users talk to together?") | ✓ Existing | GetUserInfo + GetCommonChats in v1.0 |

## Differentiators

Features that set product apart. Not expected, but valued.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| **Privacy-safe usage telemetry** | LLMs understand tool consumption patterns (what dialogs are accessed, how often, search volume) without exposing user data; enables product iteration | Medium | Behavioral events only (no names, IDs, message content); SQLite event log; `GetUsageStats` tool queryable by LLM |
| **Forum topic filtering** | Search/filter messages within supergroup topics (not just whole group); complete Telegram feature parity | Medium | Telegram exposes `reply_to.forum_topic_id` + topic name via `channels.getForumTopics()` RPC; Telethon support unvalidated |
| **from_beginning navigation** | Jump to oldest messages without cursor iteration (skip 1000 messages in 10 pages vs 100) | Low | Add `from_beginning=true` param to ListMessages; Telethon's `reverse=True` + `min_id=1` handles this |
| **Dialog list caching** | Reduce ListDialogs latency on repeated calls (hot path in LLM loops) | Low | Cache dialogs in SQLite with 5-min TTL; invalidate on new message |
| **Reaction cache improvements** | Avoid re-fetching reaction names across paginated message requests | Low | Extend entity cache schema to store reaction_emoji → reactor_list; TTL strategy TBD |

## Anti-Features

Features to explicitly NOT build.

| Anti-Feature | Why Avoid | What to Do Instead |
|--------------|-----------|-------------------|
| Write operations (send/edit/delete) | Prompt injection from message content can trigger write actions (security invariant) | Accept as permanent scope boundary; document in PROJECT.md |
| Media download/streaming | Telegram client library constraint; format already describes media | Include `media_type`, `file_size` in message format; LLM uses external Telegram client to fetch |
| Real-time notifications (webhooks) | Polling model simpler; stateless HTTP/SSE transport via mcp-proxy sufficient | Poll via ListMessages + cursor state management on caller side |
| Native HTTP/SSE transport | mcp-proxy already handles this; no value in reimplementing | Keep proxy in Docker compose; stdio to proxy works cleanly |
| Multi-account support | Stateful session management complexity; single deployment per account is orthogonal problem | Deploy separate container per account; scale horizontally not vertically |
| Message content caching | Telegram messages frequently edited; staleness risk high | Always fetch fresh from API; accept latency; offset cached for search pagination only |

## Feature Dependencies

```
ListMessages (v1.0)
├── Fuzzy name resolution (v1.0)
├── Entity cache (v1.0)
│   └── Dialog/sender metadata (name, type, username)
└── Cursor pagination (v1.0)

GetUsageStats (v1.1) [NEW]
└── Telemetry events table (v1.1) [NEW]

ListMessages + topic filter (v1.1) [ENHANCEMENT]
├── channels.getForumTopics() call (new RPC call)
└── Telethon topic support validation (research needed)

ListMessages + from_beginning (v1.1) [ENHANCEMENT]
└── reverse=True pagination (Telethon existing feature)

Dialog list cache (v1.1) [ENHANCEMENT]
└── SQLite dialogs table + TTL (new table in entity_cache.db)
```

## LLM Consumption Patterns

### Pattern 1: Dialog Discovery (Cold Start)
**How LLM uses it:** Call `ListDialogs` once at session start; cache result locally; use dialog names in subsequent calls.
**Current API:** `ListDialogs(archived=False, ignore_pinned=False)` returns newline-delimited lines: `name='...' id=... type=user|group|channel last_message_at=YYYY-MM-DD HH:MM unread=N`
**LLM consumption:** Parse lines; map name → id; filter by type if needed (e.g., "only groups"). **No change needed in v1.1** unless dialog count > 100 and latency becomes bottleneck (rare).

### Pattern 2: Message Retrieval (Hot Path)
**How LLM uses it:** Call `ListMessages` → get first page → parse messages → extract answer-relevant facts → call again with cursor if more context needed.
**Current API:** `ListMessages(dialog=name, limit=100, cursor=None, sender=filter, unread=False)` returns newest-first messages with \n-joined format.
**LLM consumption:** Parse message format; extract timestamps, sender names, text; build conversation map; use reply chain for context.
**v1.1 enhancements:**
- Add `from_beginning=true` → jump to oldest messages (useful when asking "what was the first discussion of X?" or "summarize conversation from start")
- Add `topic=name|id` → filter by supergroup topic (useful when asking "what's been discussed in #announcements topic?")
**Rationale:** Reduces pagination latency for historical questions; completes Telegram feature parity for group organization use cases.

### Pattern 3: Search with Context (Discovery Path)
**How LLM uses it:** Call `SearchMessages(query)` → get results with surrounding 3 messages → extract context → done (no pagination typically).
**Current API:** `SearchMessages(dialog=name, query=text, limit=100, offset=0)` returns offset-paginated results with ±3 context.
**LLM consumption:** Parse query results; use surrounding context to understand snippet meaning.
**v1.1 status:** No change needed; search has different pagination model (offset, not cursor) due to Telegram API constraint.

### Pattern 4: Usage Telemetry (Observability)
**How LLM uses it:** Call `GetUsageStats(period=day|week|month, metric=tool_calls|dialogs_accessed|search_volume)` → parse response → answer "which parts of my Telegram are being queried most?"
**Proposed API (NEW in v1.1):**
```python
class GetUsageStats(ToolArgs):
    """Retrieve privacy-safe usage statistics: dialog access frequency, search volume, tool call patterns.

    No PII: returns aggregated counts only, no names or message content.
    """
    period: Literal["day", "week", "month"] = "week"
    breakdown_by: Literal["dialog", "tool"] = "dialog"
```

**Response format:** Newline-delimited CSV-like output:
```
datetime,entity,metric_name,count
2026-03-12T00:00:00Z,dialog_123,list_messages_calls,15
2026-03-12T00:00:00Z,dialog_123,search_volume,2
2026-03-12T00:00:00Z,tool,list_dialogs_calls,1
```

**LLM consumption:** Parse response; answer questions like:
- "Which chat have I been asking about most in the last week?" (max count for metric=list_messages_calls, group by dialog)
- "How many times have I searched vs browsed?" (sum search_volume vs list_messages_calls)
- "What's my usage trend?" (compare day to week to month)

**Rationale:**
- **Privacy:** No user IDs, names, message content — only behavioral counts
- **Product insight:** Understand LLM tool adoption (e.g., "are users asking about topics much?" if topic filter is used)
- **Observability:** Enables debugging ("why is latency high?" → check search_volume spike)

### Pattern 5: Contact Map Building (Analysis Path)
**How LLM uses it:** Call `GetUserInfo(name)` → get profile + common chats → understand "who is this person and where do they appear?"
**Current API:** `GetUserInfo(name)` returns name, id, username, verified status, common_chats list.
**LLM consumption:** Parse common chats; answer "which of my groups do I share with this person?" or "is this account verified?"
**v1.1 status:** No change needed; feature complete in v1.0.

## MVP Recommendation

**Prioritize (v1.1 Phase):**
1. **Privacy-safe telemetry module** (`GetUsageStats` tool + event logging) — Enables product understanding, unblocks analytics roadmap
2. **Forum topic support** (`topic=` parameter in ListMessages + topic metadata in output) — Completes Telegram feature parity; medium complexity but high value for organized groups
3. **from_beginning navigation** (`from_beginning=true` in ListMessages) — Low complexity, reduces pagination latency for historical queries

**Defer (post-v1.1):**
- Dialog list caching (only needed if latency becomes issue; ListDialogs typically ≤ 1 sec)
- Reaction cache improvements (already good enough; expensive only for very high-engagement groups)
- Reaction name mapping by reactor (nice-to-have, not blocking any use case)

## Complexity Breakdown

| Feature | Implementation | Testing | Risk |
|---------|----------------|---------|------|
| **GetUsageStats + telemetry** | New telemetry.py module (60 LOC): event schema, insert_event(), query_by_period(). SQLite new table `events(timestamp, entity_id, metric, count)` with indexes. Tool handler (20 LOC) | Unit tests for event insert/query; integration test with mocked events | **MEDIUM**: PII redaction correctness (important to verify no names leak); SQLite schema versioning (what if we need to add columns later?) |
| **Forum topics** | Modify ListMessages to: (1) accept `topic` param, (2) call `channels.getForumTopics()` to get topic_id → name mapping, (3) filter messages by `reply_to.forum_topic_id`, (4) append topic name to output lines. ~40 LOC | Unit test: mock getForumTopics, test filtering; integration test: actual supergroup with topics (requires test account) | **MEDIUM-HIGH**: Telethon topic support unvalidated in testing; `reply_to.forum_topic_id` may not exist in older message versions; topic_id mapping may be flaky if topics renamed frequently |
| **from_beginning** | Add boolean param to ListMessages; set `reverse=True` + `min_id=1` when enabled. ~5 LOC | Trivial: test reverse=True produces oldest-first order; compare with reverse=False | **LOW**: Telethon's reverse parameter is well-tested; no edge cases expected |
| **Dialog list cache** | SQLite new table `dialogs(id, name, type, cached_at)`. Cache warm-up on ListDialogs; invalidation on any iter_messages call (triggers new_message event). ~30 LOC | Unit test: cache hit/miss; integration test: verify invalidation on new message | **LOW**: Standard caching pattern; 5-min TTL keeps data fresh |

## Sources

- **Telethon:** [Client.iter_messages documentation](https://docs.telethon.dev/en/stable/modules/client.html), [Issue #4453 (reverse parameter)](https://github.com/LonamiWebs/Telethon/issues/4453), [Issue #3837 (reply_to filtering)](https://github.com/LonamiWebs/Telethon/issues/3837)
- **Telegram API:** [Forums API](https://core.telegram.org/api/forum), [channels.getForumTopics RPC](https://core.telegram.org/method/channels.getForumTopics), [ForumTopic constructor](https://core.telegram.org/constructor/forum_topic), [messages.getHistory method](https://core.telegram.org/method/messages.getHistory)
- **LLM Observability:** [OpenTelemetry LLM Observability](https://opentelemetry.io/blog/2024/llm-observability/), [Langfuse tracing + masking](https://langfuse.com/docs/tracing-features/masking), [PII Redaction patterns](https://portkey.ai/blog/the-complete-guide-to-llm-observability/)
- **SQLite Patterns:** [Android SQLite Best Practices](https://developer.android.com/topic/performance/sqlite-performance-best-practices), [FTS5 (Full-Text Search)](https://www.sqlite.org/fts5.html)
- **Pagination:** [Slack API pagination evolution](https://slack.engineering/evolving-api-pagination-at-slack/), [GraphQL cursor specification](https://graphql.org/learn/pagination/)

---

## Quality Gate Checklist

- ✓ **Categories clear**: Table stakes (read access, resolution, pagination) vs differentiators (telemetry, topics, from_beginning) vs anti-features clearly separated
- ✓ **Complexity noted**: Low/Medium/High marked; implementation LOC estimates provided
- ✓ **LLM consumption patterns addressed**: 5 patterns documented with current API, proposed changes, and LLM parsing requirements
- ✓ **Telegram API specifics covered**: MTProto methods (channels.getForumTopics), message object fields (reply_to.forum_topic_id), constraints (offset pagination for search)
- ✓ **Research gaps flagged**: Telethon topic support unvalidated in testing; requires integration test with real supergroup
