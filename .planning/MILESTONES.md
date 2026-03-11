# Milestones

## v1.0 Core API (Shipped: 2026-03-11)

**Phases completed:** 5 phases, 14 plans, 26 tasks

**Key accomplishments:**
- rapidfuzz + pytest-asyncio installed via uv, 19 failing stub tests collected across 4 modules with shared conftest fixtures, unblocking parallel Wave 1 plans 02-04
- WRatio fuzzy resolver with Resolved/Candidates/NotFound tagged union, numeric bypass, and ambiguity detection — 6 tests green via TDD RED-GREEN-REFACTOR cycle
- Pure format_messages() function with HH:mm output, date headers on day change, and session-break lines at >60 min gaps — no Telethon dependency at import time
- SQLite entity cache with WAL mode and Unix-int TTL (users 30d, groups/channels 7d) plus base64+JSON opaque cursor tokens; 8 TDD tests fully green
- 14 pytest-asyncio stub tests establishing the Phase 2 test contract with mock_cache/mock_client/make_mock_message fixtures in conftest.py
- GetDialog and GetMessage removed; ListDialogs updated with type/last_message_at fields and EntityCache warm-up; get_entity_cache() singleton established for all subsequent tools
- ListMessages rewritten to accept dialog name string with fuzzy resolution, cursor pagination, sender/unread filters, and format_messages() output — 7 new tests green (TOOL-02 through TOOL-05)
- SearchMessages rewritten with name resolution, ±3 context window per hit, and add_offset-based pagination; closes Phase 2 with all 36 tests green
- 6 TDD RED-phase stubs for TOOL-08 (GetMe) and TOOL-09 (GetUserInfo) appended to tests/test_tools.py — all fail with ImportError until plan 03-02 implements the tools
- GetMe and GetUserInfo tools implemented via Telethon get_me() and GetCommonChatsRequest, completing the v1.0 milestone — LLM can query own account and look up any user's profile with shared chats using natural names.
- 4 failing TOOL-06 tests establishing red baseline for ±3 context messages, hit marker, and reaction names fetch in SearchMessages
- search_messages rewritten with ±3 context fetch, hit-group formatting, and reaction_names_map parity — closes TOOL-06, all 52 tests green
- 5 TDD Red stubs defining exact observable behaviour for TTL-filtered name resolution, search sender upsert, and cursor error message before Plan 02 implements production code
- TTL-filtered entity resolution and cursor error hardening — EntityCache.all_names_with_ttl, 4 call-site updates, search_messages sender upsert, and list_messages cursor try/except — all 57 tests green

---

## v1.1 Observability & Completeness (Planning: 2026-03-12)

**Target:** 5 phases, TBD plans

**Planned phases:**
- Phase 6: Telemetry Foundation — privacy-safe event logging with async queue, analytics.db, GetUsageStats tool
- Phase 7: Cache Improvements & Optimization — SQLite indexes, cache invalidation policy, retention/cleanup
- Phase 8: Navigation Features — ListMessages from_beginning parameter, archived dialog support in ListDialogs
- Phase 9: Forum Topics Support — ListMessages topic filtering with edge-case handling
- Phase 10: Tech Debt Cleanup — remove orphaned code, dead imports, fix timezone handling

**Research confidence:** HIGH across all dimensions (stack validated, features well-scoped, architecture patterns established, pitfalls enumerable)

**Critical constraints:**
- analytics.db MUST be separate from entity_cache.db (write contention mitigation)
- Telemetry must use async queue (fire-and-forget, never blocks tool execution)
- Privacy-first design required (zero PII, bounds-based metrics, hourly batching, side-channel prevention)
- Dialog list never cached (fetch fresh on every ListDialogs call)
- Entity metadata cached with long TTL (30d for users, 7d for groups/channels)
