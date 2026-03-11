# Roadmap: mcp-telegram

## Milestones

- ✅ **v1.0 Core API** — Phases 1–5 (shipped 2026-03-11)
- 🔄 **v1.1 Observability & Completeness** — Phases 6–10 (in planning)

## Phases

<details>
<summary>✅ v1.0 Core API (Phases 1–5) — SHIPPED 2026-03-11</summary>

- [x] Phase 1: Support Modules (4/4 plans) — completed 2026-03-10
- [x] Phase 2: Tool Updates (4/4 plans) — completed 2026-03-10
- [x] Phase 3: New Tools (2/2 plans) — completed 2026-03-10
- [x] Phase 4: SearchMessages Context Window (2/2 plans) — completed 2026-03-11
- [x] Phase 5: Cache & Error Hardening (2/2 plans) — completed 2026-03-11

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>🔄 v1.1 Observability & Completeness (Phases 6–10) — IN PROGRESS</summary>

- 🔄 Phase 6: Telemetry Foundation (3/4 plans) — in progress
- ▶️ Phase 7: Cache Improvements & Optimization (3/3 plans planned)
- [ ] Phase 8: Navigation Features (TBD plans)
- [ ] Phase 9: Forum Topics Support (TBD plans)
- [ ] Phase 10: Tech Debt Cleanup (TBD plans)

</details>

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Support Modules | v1.0 | 4/4 | Complete | 2026-03-10 |
| 2. Tool Updates | v1.0 | 4/4 | Complete | 2026-03-10 |
| 3. New Tools | v1.0 | 2/2 | Complete | 2026-03-10 |
| 4. SearchMessages Context Window | v1.0 | 2/2 | Complete | 2026-03-11 |
| 5. Cache & Error Hardening | v1.0 | 2/2 | Complete | 2026-03-11 |
| 6. Telemetry Foundation | v1.1 | 3/4 | In progress | 2026-03-12 (06-03) |
| 7. Cache Improvements & Optimization | v1.1 | 0/3 | Planned | — |
| 8. Navigation Features | v1.1 | 0/TBD | Not started | — |
| 9. Forum Topics Support | v1.1 | 0/TBD | Not started | — |
| 10. Tech Debt Cleanup | v1.1 | 0/TBD | Not started | — |

---

## Phase Details

### Phase 6: Telemetry Foundation

**Goal:** Implement privacy-safe usage telemetry with async background queue and GetUsageStats tool for LLM consumption.

**Depends on:** Nothing (Phase 1 foundation)

**Requirements:** TEL-01, TEL-02, TEL-03, TEL-04

**Success Criteria** (what must be TRUE):
1. analytics.db created on first startup with proper schema (separate from entity_cache.db)
2. All tool handlers (ListDialogs, ListMessages, SearchMessages, GetMe, GetUserInfo) emit telemetry events asynchronously (never blocking)
3. GetUsageStats tool returns natural-language summary <100 tokens with actionable patterns (deep scroll, tool frequency, error rates)
4. Privacy audit confirms zero PII in telemetry module (no entity IDs, dialog IDs, names, usernames, message content, hashes)
5. Load test baseline confirms telemetry overhead <0.5ms per tool call (async queue has negligible impact)

**Plans:**
- [x] 06-01-PLAN.md — TelemetryCollector singleton with in-memory queue and async flush (Wave 0) — completed 2026-03-11
- [x] 06-02-PLAN.md — Telemetry hooks in 5 tool handlers + GetUsageStats stub (Wave 1) — completed 2026-03-12
- [x] 06-03-PLAN.md — GetUsageStats tool with natural-language summary formatting (Wave 1) — completed 2026-03-12
- [ ] 06-04-PLAN.md — Privacy audit script + load test baseline (Wave 2)

---

### Phase 7: Cache Improvements & Optimization

**Goal:** Add SQLite indexes, establish cache invalidation policy, implement retention/cleanup strategy for bounded database size.

**Depends on:** Phase 6 (analytics.db established, separation confirmed)

**Requirements:** CACHE-01, CACHE-02, CACHE-03

**Success Criteria** (what must be TRUE):
1. SQLite indexes created on entity_cache.db: `idx_entities_type_updated(type, updated_at)` and `idx_entities_username(username)` — EXPLAIN QUERY PLAN shows index use
2. Dialog list never cached (always fresh on ListDialogs call); reaction counts always fetched fresh on ListMessages call
3. Entity metadata cached with documented TTL policy: users 30d, groups/channels 7d; `PRAGMA optimize` called after bulk writes
4. Daily cleanup timer configured: deletes telemetry >30d old, runs incremental VACUUM on analytics.db
5. Load test with 100 concurrent ListMessages calls confirms p95 latency <250ms (separating analytics.db from entity_cache.db prevents write contention)

**Plans:**
- [ ] 07-01-PLAN.md — SQLite indexes on entity_cache.db (Wave 1)
- [ ] 07-02-PLAN.md — Reaction metadata cache with 10-min TTL (Wave 1)
- [ ] 07-03-PLAN.md — Cleanup strategy: retention/VACUUM/PRAGMA optimize + load test (Wave 2)

---

### Phase 8: Navigation Features

**Goal:** Enable bidirectional message navigation and archived dialog discovery.

**Depends on:** Phase 7 (cache policy established)

**Requirements:** NAV-01, NAV-02

**Success Criteria** (what must be TRUE):
1. ListMessages accepts `from_beginning: bool` parameter; when true, fetches oldest messages first (reverse=True, min_id=1)
2. Cursor pagination works correctly with reverse iteration (both forward and backward from_beginning modes tested)
3. ListDialogs returns both archived and non-archived dialogs by default; `exclude_archived: bool = False` parameter allows filtering
4. Archived chats visible in entity cache and ListDialogs output enables "contact not found" avoidance
5. All existing tests remain green; new pagination boundary cases (first page, last page, cursor at mid-list) pass

**Plans:** TBD

---

### Phase 9: Forum Topics Support

**Goal:** Enable ListMessages to filter by forum topic with comprehensive edge-case handling (topic 0, deleted topics, pagination).

**Depends on:** Phase 7 (cache indexes), Phase 8 (navigation)

**Requirements:** TOPIC-01, TOPIC-02, TOPIC-03

**Success Criteria** (what must be TRUE):
1. ListMessages accepts `topic: str | None` parameter; fuzzy-resolves topic name scoped to given dialog
2. Topic metadata cached with short TTL (5-10 min); handles edge cases: topic 0 (General), deleted topics (permission_denied caught, fallback to unfiltered), >50 topics (pagination implemented)
3. Messages filtered correctly by `reply_to.forum_topic_id == topic_id`; topic name shown in output header when filter active
4. Resolver handles (dialog_name, topic_name) tuple correctly; prevents ambiguity when multiple dialogs have identically-named topics
5. Real forum group testing confirms filtering works with 100+ topics, some deleted, some private; pagination passes with no off-by-one errors

**Plans:** TBD

---

### Phase 10: Tech Debt Cleanup

**Goal:** Remove orphaned code, dead imports, and fix incomplete timezone parameter handling.

**Depends on:** Phase 9 (all features complete)

**Requirements:** DEBT-01, DEBT-02, DEBT-03

**Success Criteria** (what must be TRUE):
1. EntityCache.all_names() method removed (replaced by all_names_with_ttl() in Phase 5, no call sites remain)
2. Dead imports removed from tools.py:18 (TelegramClient, custom, functions, types); no unused imports in any module
3. Timezone parameter either passed at all format_messages() call sites OR removed from signature (consistent implementation)
4. Code coverage maintained; all 57+ existing tests remain green
5. No technical debt items remain open for v1.1

**Plans:** TBD

---
