# Roadmap: mcp-telegram

## Overview

Three phases that follow the dependency graph of the codebase. Phase 1 builds three
pure-Python support modules (resolver, formatter, pagination) plus the SQLite entity
cache — all testable in isolation. Phase 2 wires those modules into the existing tools,
retiring ID-based interfaces and deprecated endpoints. Phase 3 adds two new tools that
fit the existing singledispatch pattern without touching anything else.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [ ] **Phase 1: Support Modules** - Build resolver, formatter, pagination, and entity cache — the tested foundation everything else depends on
- [ ] **Phase 2: Tool Updates** - Wire support modules into existing tools; retire deprecated GetDialog and GetMessage
- [ ] **Phase 3: New Tools** - Add GetMe and GetUserInfo using the now battle-tested resolver

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
- [ ] 01-04-PLAN.md — Cache + Pagination (TDD): implement EntityCache (SQLite) and cursor encode/decode (CACH-01, CACH-02)

### Phase 2: Tool Updates
**Goal**: All existing tools accept names instead of IDs and return human-readable output; deprecated tools are gone
**Depends on**: Phase 1
**Requirements**: TOOL-01, TOOL-02, TOOL-03, TOOL-04, TOOL-05, TOOL-06, TOOL-07, CLNP-01, CLNP-02
**Success Criteria** (what must be TRUE):
  1. `ListDialogs` response includes `type` (user/group/channel) and `last_message_at` for every dialog
  2. `ListMessages` called with a dialog name (not a numeric ID) returns messages in `HH:mm FirstName: text` format with cursor-based next-page token
  3. `ListMessages` with a `sender` name filter returns only messages from the matched sender; with `unread: true` returns only unread messages
  4. `SearchMessages` called with a dialog name returns each match surrounded by ±3 context messages, with an `offset`-based next-page value that is absent on the last page
  5. Calling `GetDialog` or `GetMessage` produces a clear deprecation error (not an unhandled exception)
**Plans**: TBD

### Phase 3: New Tools
**Goal**: LLM can query own account info and look up any user's profile and shared chats
**Depends on**: Phase 2
**Requirements**: TOOL-08, TOOL-09
**Success Criteria** (what must be TRUE):
  1. `GetMe` returns own display name, numeric id, and username without any arguments
  2. `GetUserInfo` called with a name string returns the matched user's profile and a list of chats shared with the account; resolver annotation prefix appears in the response
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Support Modules | 3/4 | In Progress|  |
| 2. Tool Updates | 0/? | Not started | - |
| 3. New Tools | 0/? | Not started | - |
