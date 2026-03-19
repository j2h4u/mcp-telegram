---
gsd_state_version: 1.0
milestone: v1.4
milestone_name: Message Cache
status: unknown
stopped_at: Completed 22-01-PLAN.md
last_updated: "2026-03-19T21:32:10.870Z"
progress:
  total_phases: 5
  completed_phases: 4
  total_plans: 6
  completed_plans: 6
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-20)

**Core value:** LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup boilerplate before every real task
**Current focus:** Phase 22 — edit-detection

## Current Position

Phase: 22 (edit-detection) — EXECUTING
Plan: 1 of 1

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
- [Phase 21]: min_id=1 sentinel (OLDEST first page) treated as cache anchor_id=None to include message ID 1 in coverage
- [Phase 21]: cast('MessageLike', CachedMessage) in reply map — frozen dataclass conflicts with Protocol settable-attribute assumption in mypy
- [Phase 22-edit-detection]: Version write and cache INSERT OR REPLACE share a single transaction in store_messages()
- [Phase 22-edit-detection]: Only text change triggers versioning — edit_date-only changes do not produce version rows
- [Phase 22-edit-detection]: Batch SELECT IN for version detection — O(1) round trips per store_messages call

### Pending Todos

- 2 pending in `.planning/todos/pending`
- `2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices.md`
- `2026-03-13-carry-deferred-v1-1-cleanup-and-forum-validation.md`

### Blockers/Concerns

None

## Session Continuity

Last session: 2026-03-19T21:32:10.867Z
Stopped at: Completed 22-01-PLAN.md
Resume file: None
