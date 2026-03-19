# Phase 23: Prefetch & Lazy Refresh - Research

**Researched:** 2026-03-20
**Domain:** asyncio background tasks, SQLite WAL concurrency, cache-first pagination
**Confidence:** HIGH

## Summary

Phase 23 adds two invisible performance layers on top of the Phase 21 cache-first reads: prefetch (fire background fetches ahead of where the user is navigating) and lazy refresh (pull new messages into a cached page on access). Both operate entirely behind the `execute_history_read_capability()` call boundary — no MCP tool surface changes.

The codebase is already well-positioned for this. `asyncio.create_task()` is the natural tool since the entire server is async. The existing `MessageCache.store_messages()` write path is reusable as-is for prefetch writes. SQLite WAL mode is already enabled (Phase 20), so background write tasks can share the same connection without blocking the response path, as long as commit ordering is managed.

The main design challenges are: (1) precisely determining when a read is "first" vs "subsequent" for the PRE-01/PRE-02 split, (2) implementing the in-memory dedup set (PRE-05) so background prefetch tasks don't pile up for the same page anchor, and (3) for lazy refresh (REF-01/REF-02), computing `last_cached_id` from the page that was just returned without a second DB round-trip.

**Primary recommendation:** Introduce a `PrefetchCoordinator` class that owns the dedup set and exposes a `schedule(dialog_id, direction, anchor_id, topic_id)` method. Wire it into `execute_history_read_capability()` after the response is assembled, fire tasks with `asyncio.create_task()`, and log errors without propagating.

---

<user_constraints>
## User Constraints (from CONTEXT.md)

### Locked Decisions

All implementation decisions delegated to Claude's discretion — requirements are prescriptive.

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

### Claude's Discretion

All three gray areas delegated above.

### Deferred Ideas (OUT OF SCOPE)

None — discussion stayed within phase scope. Refactor of MCP tool surface is out of scope.
</user_constraints>

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| PRE-01 | On first ListMessages for a dialog: prefetch next page (current direction) + oldest page in background via asyncio.create_task | "First" = navigation=None or "newest"; detect by checking `navigation` parameter before cache-first path |
| PRE-02 | On any subsequent page read: prefetch next page in current direction | "Subsequent" = navigation is a base64 token (page 2+); anchor_id from navigation token drives next prefetch anchor |
| PRE-03 | When reading oldest page: prefetch next page forward (old→new direction) | Oldest page = navigation="oldest" with no cursor; prefetch next in OLDEST direction using last message id as min_id anchor |
| PRE-04 | Prefetch results stored in MessageCache via same write path as regular cache population | `msg_cache.store_messages(entity_id, raw_messages)` — already correct, no changes to write path |
| PRE-05 | In-memory dedup set of (dialog_id, direction, anchor_id) prevents duplicate API calls | Set[tuple[int, HistoryDirection, int | None]] — cleared on PrefetchCoordinator reset or process restart |
| REF-01 | On cache hit for paginated pages, background delta refresh pulls messages newer than last_cached_message_id | Trigger only when `cached_page is not None`; last_id = max(m.id for m in cached_page) |
| REF-02 | Delta fetch uses iter_messages(min_id=last_cached_id) to pull only new messages | Telethon iter_messages min_id param: returns messages with id > min_id |
| REF-03 | No timer-based refresh — refresh only on access (zero API calls for inactive dialogs) | Confirmed by design: only triggered inside execute_history_read_capability on cache hit path |
</phase_requirements>

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| asyncio (stdlib) | 3.11+ | Background task scheduling | `create_task()` fires coroutines without blocking caller; already used throughout |
| telethon | existing | `iter_messages(min_id=N)` for delta fetch | PRE-01/REF-02 use existing client interface |
| sqlite3 (stdlib) | existing | MessageCache write path for prefetch results | Same WAL-mode connection already shared |

### No new dependencies required.

**Test run command:** `uv run pytest tests/ -x -q`
**Targeted tests:** `uv run pytest tests/test_capability_history.py tests/test_cache.py -x -q`

---

## Architecture Patterns

### Recommended Project Structure (additions only)

```
src/mcp_telegram/
├── prefetch.py          # NEW: PrefetchCoordinator class + background task coroutines
├── capability_history.py  # MODIFIED: wire in PrefetchCoordinator after response assembly
└── cache.py             # UNCHANGED: store_messages() used as-is by prefetch tasks
```

### Pattern 1: PrefetchCoordinator with in-memory dedup set

**What:** A lightweight class instantiated once per server session, holding a `set` of in-flight or already-fired `(dialog_id, direction, anchor_id, topic_id)` tuples. Its `schedule()` method checks the set, adds the key if absent, then calls `asyncio.create_task()`.

**When to use:** Created at server startup (or lazily on first call), passed into `execute_history_read_capability()` as an optional parameter (like `msg_cache` today).

```python
# Source: project pattern (asyncio.create_task + in-memory set)
import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

class PrefetchCoordinator:
    def __init__(self) -> None:
        self._in_flight: set[tuple[int, str, int | None, int | None]] = set()

    def schedule(
        self,
        coro: Coroutine[Any, Any, None],
        *,
        key: tuple[int, str, int | None, int | None],
    ) -> bool:
        """Fire coro as a background task if key not already scheduled. Returns True if fired."""
        if key in self._in_flight:
            return False
        self._in_flight.add(key)
        task = asyncio.create_task(self._run(coro, key=key))
        task.add_done_callback(lambda t: None)  # suppress "Task exception was never retrieved"
        return True

    async def _run(
        self,
        coro: Coroutine[Any, Any, None],
        *,
        key: tuple[int, str, int | None, int | None],
    ) -> None:
        try:
            await coro
            logger.debug("prefetch_done key=%r", key)
        except Exception:
            logger.warning("prefetch_failed key=%r", key, exc_info=True)
        finally:
            self._in_flight.discard(key)
```

**Key detail:** Remove key from set in `finally` so the same page can be re-fetched after a failure (next user navigation re-triggers).

### Pattern 2: Prefetch trigger logic in execute_history_read_capability

**What:** After assembling the `HistoryReadExecution` response (post line 310 in capability_history.py), inspect `navigation` and `cached_page` to decide what background tasks to fire.

**Integration zone:** Lines 292–321 of `capability_history.py` — after `navigation_result` is built, before `return HistoryReadExecution(...)`.

```python
# Fire prefetch tasks — after response assembled, non-blocking
if prefetch_coordinator is not None:
    _schedule_prefetch_tasks(
        prefetch_coordinator,
        client=client,
        msg_cache=msg_cache,
        entity_id=entity_id,
        topic_id=topic_id_for_cache,
        navigation=navigation,
        cache_direction=cache_direction,
        cache_anchor_id=cache_anchor_id,
        messages=cursor_source_messages,
        limit=limit,
        cached_page=cached_page,
    )
```

### Pattern 3: Detecting "first" vs "subsequent" page (PRE-01 vs PRE-02)

**What:** The `navigation` parameter arriving at `execute_history_read_capability()` already encodes this:

| `navigation` value | Meaning | PRE trigger |
|--------------------|---------|-------------|
| `None` or `"newest"` | First page (newest) | PRE-01: next page NEWEST + oldest page |
| `"oldest"` | First page (oldest) | PRE-01 + PRE-03: next page OLDEST + oldest page (same) |
| base64 token | Subsequent page | PRE-02: next page in token's direction |

"First ListMessages for a dialog" (PRE-01) means `navigation` is `None`, `"newest"`, or `"oldest"` — these are the non-token values. A base64 token means page 2+.

**How to compute next anchor for prefetch:**
- After a NEWEST read (newest-first, descending IDs): next anchor = `min(m.id for m in messages)` — prefetch wants messages with id < that
- After an OLDEST read (oldest-first, ascending IDs): next anchor = `max(m.id for m in messages)` — prefetch wants messages with id > that
- Oldest-page prefetch (PRE-01 dual): anchor_id=None, direction=OLDEST

### Pattern 4: Delta refresh (REF-01/REF-02)

**What:** When `cached_page is not None`, fire a background task that fetches `iter_messages(entity=entity_id, min_id=last_id, limit=page_limit)` and writes results to cache.

```python
async def _delta_refresh_task(
    client: TelegramClient,
    msg_cache: MessageCache,
    entity_id: int,
    last_id: int,
    limit: int,
    topic_id: int | None,
) -> None:
    iter_kwargs: dict[str, object] = {
        "entity": entity_id,
        "min_id": last_id,
        "limit": limit,
        "reverse": True,  # oldest-first to get new messages in order
    }
    if topic_id is not None:
        iter_kwargs["reply_to"] = topic_id  # topic scoping if needed
    new_msgs = [msg async for msg in client.iter_messages(**iter_kwargs)]
    if new_msgs:
        msg_cache.store_messages(entity_id, new_msgs)
        logger.debug("delta_refresh_done entity_id=%r new_count=%d", entity_id, len(new_msgs))
```

**`last_id` source:** `max(m.id for m in cached_page)` — the highest message ID already in cache for this page. `min_id` in Telethon means "return messages with id > min_id".

### Anti-Patterns to Avoid

- **Blocking on create_task result:** Never `await` the prefetch task from the response path. Fire and forget.
- **Cascading prefetch:** Prefetch task coroutines must NOT call `_schedule_prefetch_tasks()` again. The `PrefetchCoordinator` is not passed into background coroutines — enforced by coroutine signature.
- **Separate SQLite connection per task:** The existing WAL-mode shared connection handles concurrent reads + one writer. Background tasks can reuse `msg_cache` directly (WAL serializes writes). Opening extra connections per task adds overhead and file descriptor pressure.
- **RPCError propagation:** Always wrap background task body in `try/except Exception` — Telethon flood waits must not surface as user-facing errors.
- **Key never released on failure:** Use `finally: self._in_flight.discard(key)` so a failed prefetch can be retried on next navigation.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Background task scheduling | Custom thread pool or queue | `asyncio.create_task()` | Server is already async; create_task is zero-overhead and integrates with the event loop naturally |
| Concurrent write safety | Explicit mutex on SQLite writes | SQLite WAL mode (already active) | WAL allows multiple readers + one writer; busy_timeout=30000ms already set in `_open_connection()` |
| Message ID range for delta | Manual query of message_cache table | `max(m.id for m in cached_page)` computed from the returned page | The cached_page is already in memory — no extra DB round-trip needed |

---

## Common Pitfalls

### Pitfall 1: Task exception silently discarded
**What goes wrong:** `asyncio.create_task()` discards exceptions unless `task.add_done_callback()` or `task.result()` is called. The default Python warning "Task exception was never retrieved" appears in stderr but is easy to miss.
**Why it happens:** Fire-and-forget pattern drops the task reference immediately.
**How to avoid:** In `PrefetchCoordinator._run()`, wrap the coroutine call in try/except and log. The done_callback `lambda t: None` suppresses the Python warning.
**Warning signs:** Silent prefetch failures in production, no cache warming despite navigation.

### Pitfall 2: Dedup key not released after failure
**What goes wrong:** A prefetch task fails (RPCError), key stays in `_in_flight` set forever. Subsequent navigation for same page never re-triggers prefetch.
**Why it happens:** Key added before task fires, never removed on exception.
**How to avoid:** `finally: self._in_flight.discard(key)` in `_run()`.

### Pitfall 3: Wrong min_id semantics in Telethon
**What goes wrong:** `iter_messages(min_id=X)` returns messages with `id > X` (exclusive). Using `max(m.id)` directly gives messages newer than the page's newest — correct for delta refresh. Using it as OLDEST anchor would skip that message.
**Why it happens:** min_id is a lower bound exclusive filter, not an offset.
**How to avoid:** For delta refresh (REF-02): `min_id=max(m.id for m in cached_page)` — correct, fetches newer messages. For OLDEST prefetch next-page: use `min_id=max(m.id for m in messages)` in iter_kwargs similarly.

### Pitfall 4: Topic-scoped delta refresh misses topic filter
**What goes wrong:** Delta refresh for a forum topic fetches all dialog messages, not just the topic's messages, polluting the cache with off-topic messages.
**Why it happens:** `iter_messages(entity=...)` without `reply_to` fetches all topics.
**How to avoid:** When `topic_id is not None`, add appropriate `reply_to` param to delta refresh iter_kwargs — same pattern as `capability_history.py` line 147.

### Pitfall 5: Prefetch fires on bypass paths (BYP-01/BYP-02)
**What goes wrong:** Prefetch triggers even when navigation=newest (BYP-01) or unread=True (BYP-02), creating redundant API calls.
**Why it happens:** Prefetch scheduling logic doesn't check `_should_try_cache()`.
**How to avoid:** Only schedule prefetch tasks when `_should_try_cache(navigation, unread=unread)` would return True for the prefetched page. For PRE-01 (first page), the prefetch target is page 2 (cacheable) — safe to schedule. But if `unread=True`, skip all prefetch.

### Pitfall 6: PrefetchCoordinator lifetime mismatch
**What goes wrong:** Coordinator created per-request (not per-session), so the dedup set is reset on every call — PRE-05 dedup never works.
**Why it happens:** Instantiated inside `execute_history_read_capability()` instead of at server startup.
**How to avoid:** Instantiate once in the MCP server/tools layer, pass as a parameter.

---

## Code Examples

### Determining prefetch anchors from messages list

```python
# Source: derived from existing capability_history.py cursor generation pattern (lines 291-309)

def _next_prefetch_anchor(
    messages: list[MessageLike],
    direction: HistoryDirection,
) -> int | None:
    """Compute anchor_id for the next prefetch page."""
    if not messages:
        return None
    if direction == HistoryDirection.NEWEST:
        # NEWEST is descending (highest ID first); next page anchor = lowest ID seen
        return min(getattr(m, "id", 0) for m in messages)
    else:
        # OLDEST is ascending; next page anchor = highest ID seen
        return max(getattr(m, "id", 0) for m in messages)
```

### Scheduling logic decision tree

```python
# Source: derived from CONTEXT.md decisions + REQUIREMENTS.md

def _schedule_prefetch_tasks(coordinator, *, client, msg_cache, entity_id, topic_id,
                              navigation, cache_direction, messages, limit,
                              cached_page, unread):
    if unread:
        return  # BYP-02: never prefetch for unread reads

    is_first_page = (navigation is None or navigation in ("newest", "oldest"))

    if is_first_page:
        # PRE-01: prefetch next page in current direction + oldest page
        next_anchor = _next_prefetch_anchor(messages, cache_direction)
        if next_anchor is not None:
            key = (entity_id, str(cache_direction), next_anchor, topic_id)
            coordinator.schedule(
                _prefetch_task(client, msg_cache, entity_id, cache_direction, next_anchor, limit, topic_id),
                key=key,
            )
        # Dual prefetch: oldest page (only if we're not already reading oldest)
        if cache_direction != HistoryDirection.OLDEST:
            oldest_key = (entity_id, str(HistoryDirection.OLDEST), None, topic_id)
            coordinator.schedule(
                _prefetch_task(client, msg_cache, entity_id, HistoryDirection.OLDEST, None, limit, topic_id),
                key=oldest_key,
            )
    else:
        # PRE-02: subsequent page — prefetch next page only
        next_anchor = _next_prefetch_anchor(messages, cache_direction)
        if next_anchor is not None:
            key = (entity_id, str(cache_direction), next_anchor, topic_id)
            coordinator.schedule(
                _prefetch_task(client, msg_cache, entity_id, cache_direction, next_anchor, limit, topic_id),
                key=key,
            )

    # REF-01: delta refresh on cache hit
    if cached_page is not None and messages:
        last_id = max(getattr(m, "id", 0) for m in cached_page)
        coordinator.schedule(
            _delta_refresh_task(client, msg_cache, entity_id, last_id, limit, topic_id),
            key=(entity_id, "delta", last_id, topic_id),
        )
```

### PRE-03 edge case: reading oldest page triggers forward prefetch

```python
# PRE-03: when direction=OLDEST and no cursor (first oldest page), prefetch next in OLDEST direction
# This is already covered by PRE-01 dual prefetch: oldest page prefetch IS the next OLDEST page
# when navigation="oldest". The same _prefetch_task with direction=OLDEST and anchor=max(messages.id)
# satisfies PRE-03.
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| No background prefetch | asyncio.create_task fire-and-forget | Phase 23 | First page fetches feel instant because next page is already in cache |
| Cache populated only on miss | Cache populated on every API fetch (CACHE-05, Phase 21) + delta refresh on hit (REF-01) | Phase 23 adds REF path | Cache stays fresh without polling |
| SQLite journal mode | WAL (Phase 20) | Phase 20 | Enables concurrent background writes without blocking readers |

---

## Open Questions

1. **Topic-scoped delta refresh — reply_to param**
   - What we know: `capability_history.py` uses `iter_kwargs["reply_to"] = topic_reply_to_message_id` for non-general topics
   - What's unclear: `topic_reply_to_message_id` is fetched from `topic_metadata` inside the function, but background tasks only receive `topic_id` (int). The `reply_to` message ID may differ from `topic_id`.
   - Recommendation: For the delta refresh task, pass `topic_reply_to_message_id` as a separate parameter when `topic_id is not None`. This requires reading it from `topic_capability` before scheduling. If not available (e.g., general topic), fall back to no reply_to filter.

2. **Prefetch coverage for sender-filtered reads**
   - What we know: `filter_sender_after_fetch` means the API is called without from_user filter but results are filtered in Python. Prefetch would also need to fetch without sender filter.
   - What's unclear: Should prefetch tasks apply the same sender filter or always fetch the full page?
   - Recommendation: Prefetch always fetches the full page (no sender filter) — prefetch is for cache warming, not result delivery. The cache holds all messages regardless of sender.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (asyncio_mode = "auto") |
| Config file | pyproject.toml `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_capability_history.py tests/test_cache.py -x -q` |
| Full suite command | `uv run pytest tests/ -x -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| PRE-01 | First page triggers background prefetch of next page + oldest page | integration | `uv run pytest tests/test_prefetch.py::test_first_page_schedules_dual_prefetch -x` | ❌ Wave 0 |
| PRE-02 | Subsequent page triggers background prefetch of next page | integration | `uv run pytest tests/test_prefetch.py::test_subsequent_page_schedules_next_prefetch -x` | ❌ Wave 0 |
| PRE-03 | Oldest page triggers forward prefetch | integration | `uv run pytest tests/test_prefetch.py::test_oldest_page_triggers_forward_prefetch -x` | ❌ Wave 0 |
| PRE-04 | Prefetch writes via store_messages (same path) | unit | `uv run pytest tests/test_prefetch.py::test_prefetch_task_stores_messages -x` | ❌ Wave 0 |
| PRE-05 | Dedup set prevents duplicate prefetch tasks | unit | `uv run pytest tests/test_prefetch.py::test_dedup_suppresses_duplicate_schedule -x` | ❌ Wave 0 |
| REF-01 | Cache hit triggers background delta refresh | integration | `uv run pytest tests/test_prefetch.py::test_cache_hit_triggers_delta_refresh -x` | ❌ Wave 0 |
| REF-02 | Delta fetch uses min_id=last_cached_id | unit | `uv run pytest tests/test_prefetch.py::test_delta_refresh_uses_min_id -x` | ❌ Wave 0 |
| REF-03 | No timer-based refresh — only on access | unit | `uv run pytest tests/test_prefetch.py::test_no_background_timer_refresh -x` (structural: verify no asyncio.sleep/Timer) | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_prefetch.py tests/test_capability_history.py -x -q`
- **Per wave merge:** `uv run pytest tests/ -x -q`
- **Phase gate:** Full suite green (`uv run pytest tests/ -q`) before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_prefetch.py` — covers PRE-01 through PRE-05, REF-01 through REF-03
- [ ] `src/mcp_telegram/prefetch.py` — PrefetchCoordinator class skeleton (test harness needs it to import)

---

## Sources

### Primary (HIGH confidence)
- `src/mcp_telegram/capability_history.py` — Integration zone at lines 157-321; cache-first path; prefetch hook location
- `src/mcp_telegram/cache.py` — `MessageCache.store_messages()`, `try_read_page()`, `_should_try_cache()`, WAL setup
- `src/mcp_telegram/pagination.py` — `HistoryDirection`, `encode_history_navigation`, token decode
- `src/mcp_telegram/message_ops.py` — `_build_history_iter_kwargs()`, navigation parsing, min_id/max_id semantics
- `.planning/phases/23-prefetch-lazy-refresh/23-CONTEXT.md` — all implementation decisions
- `.planning/REQUIREMENTS.md` — PRE-01 through PRE-05, REF-01 through REF-03 definitions
- `.planning/STATE.md` — accumulated decisions (prefetch triggers, dual prefetch, same write path)

### Secondary (MEDIUM confidence)
- Python asyncio docs (create_task, task lifecycle, done callbacks) — standard library, stable API
- Telethon iter_messages `min_id` semantics — verified from existing usage in `_build_history_iter_kwargs()` (line 134-136) showing exclusive lower-bound behavior

### Tertiary (LOW confidence)
- None — all findings are directly verified from project source code

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new dependencies, all patterns from existing codebase
- Architecture: HIGH — integration points precisely identified from source reading
- Pitfalls: HIGH — derived from concrete code analysis (min_id semantics, task lifecycle, dedup key lifetime)

**Research date:** 2026-03-20
**Valid until:** 2026-04-20 (stable domain — asyncio and sqlite3 stdlib, no external lib churn expected)
