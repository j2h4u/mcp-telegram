# Phase 23: Prefetch & Lazy Refresh - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

Background prefetch fills the cache ahead of navigation; lazy refresh pulls new messages into cache on access. No user-facing API changes — all work is behind the scenes in capability_history and cache layers.

Requirements: PRE-01 through PRE-05 (prefetch), REF-01 through REF-03 (lazy refresh).

</domain>

<decisions>
## Implementation Decisions

### Claude's Discretion

All three gray areas delegated — requirements are prescriptive enough.

**Background task error handling:**
- Fire-and-forget with structured logging (logger.debug for success, logger.warning for failure)
- No retry — prefetch is opportunistic, next user read will re-trigger
- RPCError (rate limit, flood wait) should log but never propagate to the user's response

**Prefetch cascading depth:**
- Single level only — only user-initiated reads (via execute_history_read_capability) trigger prefetch
- Background prefetch results do NOT trigger further prefetch (avoids runaway API calls)
- This keeps prefetch bounded and predictable

**Delta refresh transparency:**
- Fully invisible to the LLM — no response hints about background activity
- Delta results land in cache silently for next read to pick up

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — PRE-01 through PRE-05, REF-01 through REF-03 definitions

### Cache infrastructure (integration points)
- `src/mcp_telegram/cache.py` — MessageCache class (store_messages, try_read_page), _should_try_cache bypass logic, CachedMessage proxy
- `src/mcp_telegram/capability_history.py` — execute_history_read_capability() where cache-first reads happen (lines 157-242 are the integration zone)

### Navigation model
- `src/mcp_telegram/pagination.py` — HistoryDirection enum, encode/decode navigation tokens

### Prior phase decisions
- `.planning/STATE.md` — accumulated decisions section (prefetch triggers, dual prefetch, same write path)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `MessageCache.store_messages()` — prefetch results use exact same write path (PRE-04)
- `MessageCache.try_read_page()` — coverage detection already handles direction + anchor_id + topic_id
- `_should_try_cache()` — bypass logic already correct, no changes needed for prefetch
- `HistoryDirection` enum — NEWEST/OLDEST already used for cache direction in capability_history

### Established Patterns
- `asyncio` async/await throughout — background tasks via `asyncio.create_task()` is natural
- Single TelegramClient shared across all calls — Telethon handles concurrent iter_messages safely (MTProto layer serializes)
- Cache population at line 242: `msg_cache.store_messages(entity_id, raw_messages)` — prefetch hooks into this flow

### Integration Points
- `execute_history_read_capability()` — after returning the response, fire prefetch tasks
- `MessageCache` constructor takes `sqlite3.Connection` — prefetch tasks need their own connection or serialize through the existing one (SQLite WAL allows concurrent reads + one writer)
- Navigation token generation (lines 293-309) — provides the anchor_id for prefetch direction

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

### Reviewed Todos (not folded)
- "Refactor MCP tool surface around capability-oriented best practices" — unrelated to prefetch/cache; belongs in a future tool-surface milestone

</deferred>

---

*Phase: 23-prefetch-lazy-refresh*
*Context gathered: 2026-03-20*
