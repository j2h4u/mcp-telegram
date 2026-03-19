---
gsd_state_version: 1.0
milestone: v1.4
milestone_name: Message Cache
status: unknown
stopped_at: Completed 21-01-PLAN.md
last_updated: "2026-03-19T20:50:14.186Z"
progress:
  total_phases: 5
  completed_phases: 2
  total_plans: 5
  completed_plans: 4
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-20)

**Core value:** LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup boilerplate before every real task
**Current focus:** Phase 21 — cache-first-reads-bypass-rules

## Current Position

Phase: 21 (cache-first-reads-bypass-rules) — EXECUTING
Plan: 2 of 2

## Accumulated Context

### Decisions

- Approach 1 (Structured Field Cache + CachedMessage Proxy) selected over JSON Blob and Page-Level cache
- Messages are near-immutable — no TTL expiration, cache grows indefinitely
- Prefetch triggers on first ListMessages per dialog (not on ListDialogs)
- Dual prefetch: next page + oldest page on first access
- Same SQLite DB as entity_cache.db — no separate connection
- META-01/META-02 already implemented — Phase 19 is commit + test coverage
- [Phase 20-cache-foundation]: WITHOUT ROWID for message_cache and message_versions — composite PK always known, eliminates secondary B-tree
- [Phase 20-cache-foundation]: message_versions schema-only in Plan 01 — Phase 22 populates; schema-first keeps bootstrap idempotent
- [Phase 20-cache-foundation]: tuple[object, ...] + cast() for from_row() parameter type — SQLite rows are mixed-type, cast() at use sites satisfies mypy
- [Phase 20-cache-foundation]: CachedMessage.reactions/media always None — media_description folds into .message; reactions not stored in cache
- [Phase 21]: HistoryDirection imported via TYPE_CHECKING + runtime import in try_read_page to avoid circular import
- [Phase 21]: try_read_page returns None when len(rows) < limit — strict partial coverage detection
- [Phase 21]: forum_topic_id=1 sentinel for General topic (reply_to_top_id=None but forum_topic=True)

### Pending Todos

- 2 pending in `.planning/todos/pending`
- `2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices.md`
- `2026-03-13-carry-deferred-v1-1-cleanup-and-forum-validation.md`

### Blockers/Concerns

None

## Session Continuity

Last session: 2026-03-19T20:50:14.183Z
Stopped at: Completed 21-01-PLAN.md
Resume file: None
