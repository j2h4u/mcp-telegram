# Roadmap: mcp-telegram

## Overview

Three phases that follow the dependency graph of the codebase. Phase 1 builds three
pure-Python support modules (resolver, formatter, pagination) plus the SQLite entity
cache — all testable in isolation. Phase 2 wires those modules into the existing tools,
retiring ID-based interfaces and deprecated endpoints. Phase 3 adds two new tools that
fit the existing singledispatch pattern without touching anything else.

Phases 4–5 close gaps identified by the v1.0 milestone audit: TOOL-06 (SearchMessages
context window was never implemented) and tech debt in cache TTL, search entity upsert,
and error handling.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Support Modules** - Build resolver, formatter, pagination, and entity cache — the tested foundation everything else depends on (completed 2026-03-10)
- [x] **Phase 2: Tool Updates** - Wire support modules into existing tools; retire deprecated GetDialog and GetMessage (completed 2026-03-10)
- [x] **Phase 3: New Tools** - Add GetMe and GetUserInfo using the now battle-tested resolver (completed 2026-03-10)
- [ ] **Phase 4: SearchMessages Context Window** - Implement ±3 context messages per search hit (closes TOOL-06 audit gap)
- [ ] **Phase 5: Cache & Error Hardening** - Enforce entity cache TTL, add search entity upsert, harden cursor error handling (tech debt)

## Phase Details

### Phase 1: Support Modules
**Goal**: Tested support modules exist and can be called by tools
**Depends on**: Nothing (first phase)
**Requirements**: RES-01, RES-02, FMT-01, CACH-01, CACH-02
**Success Criteria** (what must be TRUE):
  1. Given a dialog name string, resolver returns the correct entity ID (or a candidates list, or not-found) — no raw IDs required from the caller
  2. Given a sender name string, resolver applies the same WRatio thresholds and returns the same result structure as dialog resolution
  3. A message object fed to the formatter produces `HH:mm FirstName: text` output with date headers on day change and session-break lines at >60-min gaps
  4. Entity metadata (users, groups, channels) is persisted to SQLite and survives process restart; a re-fetched entity reads from cache within TTL
  5. Cursor tokens encode and decode round-trip correctly; a cross-dialog decode raises an error
**Plans**: 4 plans

Plans:
- [x] 01-01-PLAN.md — Test scaffold: install rapidfuzz + pytest deps, create stub test files and conftest
- [x] 01-02-PLAN.md — Resolver (TDD): implement resolve() with WRatio thresholds, tagged-union result types (RES-01, RES-02)
- [x] 01-03-PLAN.md — Formatter (TDD): implement format_messages() pure function with date headers and session breaks (FMT-01)
- [x] 01-04-PLAN.md — Cache + Pagination (TDD): implement EntityCache (SQLite) and cursor encode/decode (CACH-01, CACH-02)

### Phase 2: Tool Updates
**Goal**: All existing tools accept names instead of IDs and return human-readable output; deprecated tools are gone
**Depends on**: Phase 1
**Requirements**: TOOL-01, TOOL-02, TOOL-03, TOOL-04, TOOL-05, TOOL-06, TOOL-07, CLNP-01, CLNP-02
**Success Criteria** (what must be TRUE):
  1. `ListDialogs` response includes `type` (user/group/channel) and `last_message_at` for every dialog
  2. `ListMessages` called with a dialog name (not a numeric ID) returns messages in `HH:mm FirstName: text` format with cursor-based next-page token
  3. `ListMessages` with a `sender` name filter returns only messages from the matched sender; with `unread: true` returns only unread messages
  4. `SearchMessages` called with a dialog name returns each match surrounded by ±3 context messages, with an `offset`-based next-page value that is absent on the last page
  5. `GetDialog` and `GetMessage` are removed from the codebase (no stubs, no deprecation shims)
**Plans**: 4 plans

Plans:
- [x] 02-01-PLAN.md — Test scaffold (TDD Wave 0): 14 failing stub tests in test_tools.py + mock fixtures in conftest.py
- [x] 02-02-PLAN.md — Cleanup + ListDialogs: remove GetDialog/GetMessage, add type/last_message_at, add EntityCache singleton (TOOL-01, CLNP-01, CLNP-02)
- [x] 02-03-PLAN.md — ListMessages rewrite: name resolution, cursor pagination, sender/unread filters (TOOL-02, TOOL-03, TOOL-04, TOOL-05)
- [x] 02-04-PLAN.md — SearchMessages rewrite: name resolution, ±3 context window, offset pagination (TOOL-06, TOOL-07)

### Phase 3: New Tools
**Goal**: LLM can query own account info and look up any user's profile and shared chats
**Depends on**: Phase 2
**Requirements**: TOOL-08, TOOL-09
**Success Criteria** (what must be TRUE):
  1. `GetMe` returns own display name, numeric id, and username without any arguments
  2. `GetUserInfo` called with a name string returns the matched user's profile and a list of chats shared with the account; resolver annotation prefix appears in the response
**Plans**: 2 plans

Plans:
- [x] 03-01-PLAN.md — Test stubs (TDD Wave 0): 6 failing tests for GetMe and GetUserInfo (TOOL-08, TOOL-09)
- [x] 03-02-PLAN.md — Implement GetMe and GetUserInfo in tools.py (TOOL-08, TOOL-09)

### Phase 4: SearchMessages Context Window
**Goal**: SearchMessages returns each hit surrounded by ±3 context messages, satisfying TOOL-06 as specified
**Depends on**: Phase 3
**Requirements**: TOOL-06
**Gap Closure**: Closes TOOL-06 gap from v1.0 milestone audit — context window was never implemented despite Phase 2 SUMMARY claiming otherwise
**Success Criteria** (what must be TRUE):
  1. `SearchMessages` with a query returns each matched message surrounded by up to 3 messages before and after it (±3 context window)
  2. Context messages are visually distinguishable from hit messages (e.g. hit lines prefixed or grouped)
  3. Reaction names are passed to `format_messages` for search results (same as ListMessages)
  4. All 42 existing tests remain green; new tests cover context window behaviour
**Plans**: 2 plans

Plans:
- [x] 04-01-PLAN.md — Test stubs (TDD Wave 0): failing tests asserting ±3 context in SearchMessages output (TOOL-06)
- [ ] 04-02-PLAN.md — Implement context window fetch in search_messages; pass reaction_names_map (TOOL-06)

### Phase 5: Cache & Error Hardening
**Goal**: Entity cache respects TTL, search results populate the cache, cursor errors return friendly messages
**Depends on**: Phase 4
**Requirements**: CACH-01, CACH-02, TOOL-03
**Gap Closure**: Tech debt identified in v1.0 audit — non-blocking but degrades correctness over time
**Success Criteria** (what must be TRUE):
  1. `EntityCache.get()` is called during resolution with correct TTL per entity type; stale entries are not returned
  2. `search_messages` upserts sender entities into the cache after a search
  3. `ListMessages` with an invalid cursor returns a user-readable error instead of a generic RuntimeError
**Plans**: 2 plans

Plans:
- [ ] 05-01-PLAN.md — Test stubs (TDD Wave 0): failing tests for TTL eviction, search upsert, cursor error message (CACH-01, CACH-02, TOOL-03)
- [ ] 05-02-PLAN.md — Implement TTL enforcement, search entity upsert, cursor error handling (CACH-01, CACH-02, TOOL-03)

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Support Modules | 4/4 | Complete   | 2026-03-10 |
| 2. Tool Updates | 4/4 | Complete   | 2026-03-10 |
| 3. New Tools | 2/2 | Complete   | 2026-03-10 |
| 4. SearchMessages Context Window | 0/2 | Not started | - |
| 5. Cache & Error Hardening | 0/2 | Not started | - |
