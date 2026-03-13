# Milestones

## v1.2 MCP Surface Research (Shipped: 2026-03-13)

**Phases completed:** 4 phases, 12 plans, 32 tasks

**Audit:** `tech_debt` — no blocking gaps; validation and runtime-freshness follow-ups remain

**Key accomplishments:**
- Established the retained-source evidence hierarchy and froze the reflected seven-tool brownfield baseline for all later analysis.
- Produced a comparative audit of the current MCP surface across both tool-level and workflow-level model burden.
- Compared minimal, medium, and maximal redesign paths across the current public contract.
- Selected the Medium path as the Pareto recommendation for the next implementation milestone.
- Delivered an implementation-ready memo with sequencing, open questions, and restarted-runtime validation gates.

---

## v1.1 Observability & Completeness (Shipped: 2026-03-13)

**Phases completed:** 4 phases, 15 plans, 37 tasks

**Key accomplishments:**
- Privacy-safe telemetry shipped with `analytics.db`, exception-safe instrumentation, and the `GetUsageStats` tool.
- Cache improvements shipped: SQLite indexes, reaction metadata caching, and bounded cleanup/optimization strategy.
- Navigation completed: `from_beginning` history reads and archived dialogs now work as part of the normal discovery flow.
- Forum-topic support shipped with dialog-scoped resolution, explicit tombstone/inaccessible responses, and topic-safe unread pagination.
- Operator-only topic debug commands and a rebuilt-runtime validation checklist now exist for deferred large-forum evidence capture.

---

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
