# Requirements: mcp-telegram

**Defined:** 2026-03-11
**Milestone:** v1.0 Core API
**Core Value:** LLM can work with Telegram using natural names — zero cold-start friction

## v1.0 Requirements

### Resolution

- [x] **RES-01**: LLM can refer to a dialog by name string; server resolves to entity ID via fuzzy match (WRatio ≥90 auto, 60–89 candidates, <60 not found)
- [x] **RES-02**: LLM can refer to a message sender by name string; same resolution algorithm and thresholds as dialog resolution

### Format

- [x] **FMT-01**: Messages returned in unified human-readable format: `HH:mm FirstName: text [reactions]` with date headers, session breaks (>60 min gaps), reply annotations, and inline media descriptions

### Tools

- [x] **TOOL-01**: `ListDialogs` returns `type` (user/group/channel) and `last_message_at` for each dialog
- [x] **TOOL-02**: `ListMessages` accepts dialog by name, returns messages in unified format
- [x] **TOOL-03**: `ListMessages` uses cursor-based pagination (opaque tokens, stable under concurrent message arrival)
- [x] **TOOL-04**: `ListMessages` accepts optional `sender` name filter
- [x] **TOOL-05**: `ListMessages` accepts optional `unread` filter
- [x] **TOOL-06**: `SearchMessages` accepts dialog by name, returns each result with ±3 messages of surrounding context *(audit gap — Phase 4)*
- [x] **TOOL-07**: `SearchMessages` uses offset-based pagination (`next_offset` absent when exhausted)
- [x] **TOOL-08**: `GetMe` returns own name, id, and username
- [x] **TOOL-09**: `GetUserInfo` returns target user's profile and list of common chats

### Cache

- [x] **CACH-01**: Entity metadata (users, groups, channels) persisted in SQLite (`entity_cache.db`); TTL 30d users, 7d groups/channels
- [x] **CACH-02**: Cache populated lazily from API responses (upsert on every entity-bearing response)

### Cleanup

- [x] **CLNP-01**: `GetDialog` tool removed (no stubs, no BC obligations)
- [x] **CLNP-02**: `GetMessage` tool removed (no stubs, no BC obligations)

## Future Requirements

*(None identified — scope is focused)*

## Out of Scope

| Feature | Reason |
|---------|--------|
| Write operations (send/edit/delete) | Security invariant — read-only is permanent, not deferred |
| Media download/streaming | Format describes media, doesn't fetch it |
| Real-time notifications/webhooks | Polling model only |
| Native HTTP/SSE transport | mcp-proxy covers this; no disruption needed |
| Multi-account support | Single session per deployment |
| Message content caching | Messages always fetched fresh |
| Group membership table | High staleness risk, no v1 tool depends on it |
| `transliterate` dependency | Defer until validated against real contacts |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| RES-01 | Phase 1 | Complete |
| RES-02 | Phase 1 | Complete |
| FMT-01 | Phase 1 | Complete |
| CACH-01 | Phase 1 / Phase 5 (TTL enforcement) | Partial |
| CACH-02 | Phase 1 / Phase 5 (search upsert) | Partial |
| TOOL-01 | Phase 2 | Complete |
| TOOL-02 | Phase 2 | Complete |
| TOOL-03 | Phase 2 / Phase 5 (error hardening) | Partial |
| TOOL-04 | Phase 2 | Complete |
| TOOL-05 | Phase 2 | Complete |
| TOOL-06 | Phase 4 (gap closure) | Complete |
| TOOL-07 | Phase 2 | Complete |
| CLNP-01 | Phase 2 | Complete |
| CLNP-02 | Phase 2 | Complete |
| TOOL-08 | Phase 3 | Complete |
| TOOL-09 | Phase 3 | Complete |

**Coverage:**
- v1.0 requirements: 16 total
- Fully satisfied: 13
- Pending gap closure (Phases 4–5): 3 (TOOL-06, CACH-01/02, TOOL-03)

---
*Requirements defined: 2026-03-11*
*Last updated: 2026-03-11 — TOOL-06 reset to pending; Phases 4–5 added to close audit gaps*
