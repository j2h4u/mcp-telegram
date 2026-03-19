# Roadmap: mcp-telegram

## Milestones

- ✅ **v1.0 Core API** — Phases 1-5 (shipped 2026-03-11)
- ✅ **v1.1 Observability & Completeness** — Phases 6-9 (shipped 2026-03-13)
- ✅ **v1.2 MCP Surface Research** — Phases 10-13 (shipped 2026-03-13)
- ✅ **v1.3 Medium Implementation** — Phases 14-18 (shipped 2026-03-14)
- 🚧 **v1.4 Message Cache** — Phases 19-23 (in progress)

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions between integers

### 🚧 v1.4 Message Cache (In Progress)

**Milestone Goal:** Persistent SQLite message cache with background prefetch to reduce Telegram API calls and speed up repeated reads.

#### Phase 19: Dialog Metadata Enrichment
**Goal**: ListDialogs surfaces members count and creation date for groups/channels
**Depends on**: Phase 18
**Requirements**: META-01, META-02
**Success Criteria** (what must be TRUE):
  1. ListDialogs output includes `members=N` for groups and channels
  2. ListDialogs output includes `created=YYYY-MM-DD` for groups and channels
  3. Private chats omit both fields (no participants_count or creation date available)
**Plans**: 1 plan
Plans:
- [x] 19-01-PLAN.md — Test coverage + docstring for members/created metadata fields

#### Phase 20: Cache Foundation
**Goal**: MessageCache SQLite table and CachedMessage proxy class exist and are wired into the shared database bootstrap
**Depends on**: Phase 19
**Requirements**: CACHE-01, CACHE-02, CACHE-07
**Success Criteria** (what must be TRUE):
  1. `message_cache` table exists in entity_cache.db after bootstrap with correct schema (dialog_id, message_id PK, all structured fields)
  2. CachedMessage proxy exposes `.sender.first_name`, `.reply_to.reply_to_msg_id` and satisfies MessageLike Protocol — formatter accepts it without changes
  3. Cache bootstrap remains parallel-session-safe (existing lock file covers new table creation)
  4. `message_versions` table exists in same DB for edit tracking (schema only, not yet populated)
**Plans**: 2 plans
Plans:
- [ ] 20-01-PLAN.md — Schema DDL + bootstrap extension for message_cache and message_versions tables (TDD)
- [ ] 20-02-PLAN.md — CachedMessage proxy dataclass satisfying MessageLike Protocol (TDD)

#### Phase 21: Cache-First Reads & Bypass Rules
**Goal**: History reads serve pages 2+ from cache when available; bypass rules ensure live data where required
**Depends on**: Phase 20
**Requirements**: CACHE-03, CACHE-04, CACHE-05, CACHE-06, BYP-01, BYP-02, BYP-03, BYP-04
**Success Criteria** (what must be TRUE):
  1. Page 2+ of ListMessages is served from cache when the range is covered — no Telegram API call observed for covered pages
  2. `navigation="newest"` (first page) always fetches from Telegram API — never served stale
  3. `unread=True` in ListMessages always fetches live regardless of cache state
  4. ListUnreadMessages always fetches live (entire tool bypasses cache)
  5. SearchMessages always fetches live; results are written to MessageCache for future ListMessages hits
  6. Cache coverage tracking is topic-aware — interleaved message IDs across topics do not produce false coverage hits
**Plans**: TBD

#### Phase 22: Edit Detection
**Goal**: Edited messages are detected at write time and marked visually in the formatter
**Depends on**: Phase 21
**Requirements**: EDIT-01, EDIT-02, EDIT-03
**Success Criteria** (what must be TRUE):
  1. When a message is re-fetched with changed text, the old text is recorded in `message_versions` before the cache row is updated
  2. Messages with `edit_date IS NOT NULL` show `[edited HH:mm]` in formatted output
  3. Messages without `edit_date` show no edited marker (no false positives)
**Plans**: TBD

#### Phase 23: Prefetch & Lazy Refresh
**Goal**: Background prefetch fills the cache ahead of navigation; lazy refresh pulls new messages into cache on access
**Depends on**: Phase 21
**Requirements**: PRE-01, PRE-02, PRE-03, PRE-04, PRE-05, REF-01, REF-02, REF-03
**Success Criteria** (what must be TRUE):
  1. First ListMessages for a dialog triggers background fetch of the next page (current direction) and the oldest page — without blocking the response
  2. Any subsequent page read triggers background fetch of the next page in current direction
  3. Reading the oldest page triggers background prefetch forward (old→new direction)
  4. Prefetch results land in MessageCache via the same write path as regular fetches
  5. Duplicate prefetch tasks for the same (dialog_id, direction, anchor_id) are suppressed — in-memory dedup set prevents redundant API calls
  6. On a cache hit for paginated pages, a background delta fetch pulls messages newer than the last cached ID — response is not blocked
**Plans**: TBD

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Support Modules | v1.0 | 4/4 | Complete | 2026-03-10 |
| 2. Tool Updates | v1.0 | 4/4 | Complete | 2026-03-10 |
| 3. New Tools | v1.0 | 2/2 | Complete | 2026-03-10 |
| 4. SearchMessages Context Window | v1.0 | 2/2 | Complete | 2026-03-11 |
| 5. Cache & Error Hardening | v1.0 | 2/2 | Complete | 2026-03-11 |
| 6. Telemetry Foundation | v1.1 | 4/4 | Complete | 2026-03-12 |
| 7. Cache Improvements & Optimization | v1.1 | 3/3 | Complete | 2026-03-12 |
| 8. Navigation Features | v1.1 | 2/2 | Complete | 2026-03-12 |
| 9. Forum Topics Support | v1.1 | 6/6 | Complete | 2026-03-12 |
| 10. Evidence Base & Audit Frame | v1.2 | 3/3 | Complete | 2026-03-13 |
| 11. Current Surface Comparative Audit | v1.2 | 3/3 | Complete | 2026-03-13 |
| 12. Redesign Options & Pareto Recommendation | v1.2 | 3/3 | Complete | 2026-03-13 |
| 13. Implementation Sequencing & Decision Memo | v1.2 | 3/3 | Complete | 2026-03-13 |
| 14. Boundary Recovery | v1.3 | 2/2 | Complete | 2026-03-13 |
| 15. Capability Seams | v1.3 | 3/3 | Complete | 2026-03-13 |
| 16. Unified Navigation Contract | v1.3 | 3/3 | Complete | 2026-03-14 |
| 17. Direct Read/Search Workflows | v1.3 | 4/4 | Complete | 2026-03-14 |
| 18. Surface Posture & Rollout Proof | v1.3 | 3/3 | Complete | 2026-03-14 |
| 19. Dialog Metadata Enrichment | v1.4 | 1/1 | Complete    | 2026-03-19 |
| 20. Cache Foundation | v1.4 | 0/2 | In progress | - |
| 21. Cache-First Reads & Bypass Rules | v1.4 | 0/TBD | Not started | - |
| 22. Edit Detection | v1.4 | 0/TBD | Not started | - |
| 23. Prefetch & Lazy Refresh | v1.4 | 0/TBD | Not started | - |

## Shipped Milestones

<details>
<summary>✅ v1.0 Core API (Phases 1-5) — SHIPPED 2026-03-11</summary>

- [x] Phase 1: Support Modules (4/4 plans) — completed 2026-03-10
- [x] Phase 2: Tool Updates (4/4 plans) — completed 2026-03-10
- [x] Phase 3: New Tools (2/2 plans) — completed 2026-03-10
- [x] Phase 4: SearchMessages Context Window (2/2 plans) — completed 2026-03-11
- [x] Phase 5: Cache & Error Hardening (2/2 plans) — completed 2026-03-11

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>✅ v1.1 Observability & Completeness (Phases 6-9) — SHIPPED 2026-03-13</summary>

- [x] Phase 6: Telemetry Foundation (4/4 plans) — completed 2026-03-12
- [x] Phase 7: Cache Improvements & Optimization (3/3 plans) — completed 2026-03-12
- [x] Phase 8: Navigation Features (2/2 plans) — completed 2026-03-12
- [x] Phase 9: Forum Topics Support (6/6 plans) — completed 2026-03-12

Full details: `.planning/milestones/v1.1-ROADMAP.md`

</details>

<details>
<summary>✅ v1.2 MCP Surface Research (Phases 10-13) — SHIPPED 2026-03-13</summary>

- [x] Phase 10: Evidence Base & Audit Frame (3/3 plans) — completed 2026-03-13
- [x] Phase 11: Current Surface Comparative Audit (3/3 plans) — completed 2026-03-13
- [x] Phase 12: Redesign Options & Pareto Recommendation (3/3 plans) — completed 2026-03-13
- [x] Phase 13: Implementation Sequencing & Decision Memo (3/3 plans) — completed 2026-03-13

Full details: `.planning/milestones/v1.2-ROADMAP.md`

</details>

<details>
<summary>✅ v1.3 Medium Implementation (Phases 14-18) — SHIPPED 2026-03-14</summary>

- [x] Phase 14: Boundary Recovery (2/2 plans) — completed 2026-03-13
- [x] Phase 15: Capability Seams (3/3 plans) — completed 2026-03-13
- [x] Phase 16: Unified Navigation Contract (3/3 plans) — completed 2026-03-14
- [x] Phase 17: Direct Read/Search Workflows (4/4 plans) — completed 2026-03-14
- [x] Phase 18: Surface Posture & Rollout Proof (3/3 plans) — completed 2026-03-14

Full details: `.planning/milestones/v1.3-ROADMAP.md`

</details>
