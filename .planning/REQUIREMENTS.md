# Requirements: mcp-telegram

**Defined:** 2026-03-11
**Milestone:** v1.0 Core API
**Core Value:** LLM can work with Telegram using natural names — zero cold-start friction

## v1.0 Requirements

### Resolution

- [ ] **RES-01**: LLM can refer to a dialog by name string; server resolves to entity ID via fuzzy match (WRatio ≥90 auto, 60–89 candidates, <60 not found)
- [ ] **RES-02**: LLM can refer to a message sender by name string; same resolution algorithm and thresholds as dialog resolution

### Format

- [ ] **FMT-01**: Messages returned in unified human-readable format: `HH:mm FirstName: text [reactions]` with date headers, session breaks (>60 min gaps), reply annotations, and inline media descriptions

### Tools

- [ ] **TOOL-01**: `ListDialogs` returns `type` (user/group/channel) and `last_message_at` for each dialog
- [ ] **TOOL-02**: `ListMessages` accepts dialog by name, returns messages in unified format
- [ ] **TOOL-03**: `ListMessages` uses cursor-based pagination (opaque tokens, stable under concurrent message arrival)
- [ ] **TOOL-04**: `ListMessages` accepts optional `sender` name filter
- [ ] **TOOL-05**: `ListMessages` accepts optional `unread` filter
- [ ] **TOOL-06**: `SearchMessages` accepts dialog by name, returns each result with ±3 messages of surrounding context
- [ ] **TOOL-07**: `SearchMessages` uses offset-based pagination (`next_offset` absent when exhausted)
- [ ] **TOOL-08**: `GetMe` returns own name, id, and username
- [ ] **TOOL-09**: `GetUserInfo` returns target user's profile and list of common chats

### Cache

- [ ] **CACH-01**: Entity metadata (users, groups, channels) persisted in SQLite (`entity_cache.db`); TTL 30d users, 7d groups/channels
- [ ] **CACH-02**: Cache populated lazily from API responses (upsert on every entity-bearing response)

### Cleanup

- [ ] **CLNP-01**: `GetDialog` tool removed (no stubs, no BC obligations)
- [ ] **CLNP-02**: `GetMessage` tool removed (no stubs, no BC obligations)

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
| RES-01 | — | Pending |
| RES-02 | — | Pending |
| FMT-01 | — | Pending |
| TOOL-01 | — | Pending |
| TOOL-02 | — | Pending |
| TOOL-03 | — | Pending |
| TOOL-04 | — | Pending |
| TOOL-05 | — | Pending |
| TOOL-06 | — | Pending |
| TOOL-07 | — | Pending |
| TOOL-08 | — | Pending |
| TOOL-09 | — | Pending |
| CACH-01 | — | Pending |
| CACH-02 | — | Pending |
| CLNP-01 | — | Pending |
| CLNP-02 | — | Pending |

**Coverage:**
- v1.0 requirements: 16 total
- Mapped to phases: 0 (pending roadmap)
- Unmapped: 16 ⚠️

---
*Requirements defined: 2026-03-11*
*Last updated: 2026-03-11 after initial definition*
