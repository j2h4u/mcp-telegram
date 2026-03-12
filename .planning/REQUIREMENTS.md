# mcp-telegram v1.1 Requirements

**Defined:** 2026-03-12
**Milestone:** v1.1 — Observability & Completeness
**Core value:** LLM understands its own usage patterns and navigates Telegram more completely

---

## v1.1 Requirements

### Telemetry

- [x] **TEL-01** — `analytics.py` module: SQLite event store (`analytics.db`, separate from `entity_cache.db`), `record_event()` with async background queue, zero PII in schema (tool name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type — no IDs, names, content)
- [ ] **TEL-02** — `GetUsageStats` MCP tool: queries analytics DB, returns concise natural-language summary (<100 tokens) with actionable patterns (deep scroll detection, tool frequency, error rates) — designed for LLM consumption, not dashboards
- [ ] **TEL-03** — Privacy audit: all event recording code reviewed to confirm zero PII leakage (no entity IDs, names, usernames, message content, dialog names — not even hashed)
- [ ] **TEL-04** — Telemetry hook in every tool handler: `ListDialogs`, `ListMessages`, `SearchMessages`, `GetMe`, `GetUserInfo`; `GetUsageStats` calls NOT recorded (avoid noise)

### Cache

- [ ] **CACHE-01** — SQLite indexes added to `entity_cache.db`: `idx_entities_type_updated` on `(type, updated_at)`, `idx_entities_username` on `(username)` — improves `all_names_with_ttl()` from O(N) to O(log N)
- [ ] **CACHE-02** — Reaction cache: store reaction data per message in `entity_cache.db` with short TTL (10 min); avoid re-fetching reaction names on every `ListMessages` call for same messages
- [x] **CACHE-03** — VACUUM / cleanup strategy: stale entity records deleted on startup or timer; DB file size bounded; `PRAGMA optimize` called after bulk writes

### Navigation

- [x] **NAV-01** — `ListMessages` gains `from_beginning: bool` parameter (default `false`): when true, fetches oldest messages first (`reverse=True, min_id=1` in Telethon), ignores any cursor — enables LLM to read chat history from the start without iterating through all pages
- [ ] **NAV-02** — `ListDialogs` returns archived and non-archived dialogs by default (`exclude_archived: bool = False`); archived chats in Telegram are a UI organization tool, not true archival — LLM must see all dialogs to avoid false-negative "contact not found" responses; entity cache populated from both

### Forum Topics

- [x] **TOPIC-01** — `ListMessages` gains `topic: str | None` parameter: fuzzy-resolves topic name to topic ID in given supergroup, filters messages to that topic only
- [x] **TOPIC-02** — Topic metadata cache: topic ID → name mapping stored with short TTL; resolved via `GetForumTopicsRequest`; handles edge cases (General normalized explicitly, deleted/inaccessible topics → clear error, >50 topics → pagination)
- [x] **TOPIC-03** — Topic name shown in `ListMessages` output header when `topic` filter is active

### Tech Debt (v1.0 carry-over)

- [ ] **DEBT-01** — Remove `EntityCache.all_names()` (orphaned by `all_names_with_ttl()`)
- [ ] **DEBT-02** — Remove dead imports in `tools.py:18` (TelegramClient, custom, functions, types)
- [ ] **DEBT-03** — Fix `tz` param: either pass timezone at call sites or remove from `format_messages()` signature

---

## v2 Requirements (deferred)

- **Transliteration** — add `transliterate` dep for Ukrainian/Belarusian name matching; validate need against real contacts first (research flag from v1.0)
- **Pydantic str|int union** — expose `dialog` as `str | int` once MCP client `anyOf` compatibility is confirmed (research flag from v1.0)
- **Message content cache** — cache immutable message text/metadata by (dialog_id, message_id); high effort, moderate impact
- **ListTopics tool** — dedicated tool to list all forum topics in a supergroup
- **tz support** — pass user timezone to `format_messages()` for localized timestamps

---

## Out of Scope (v1.1)

- **Dialog list caching** — changes too frequently; TTL-based cache causes stale data; research confirmed do NOT cache
- **L1 in-memory hot cache** — premature optimization; SQLite is fast enough for current entity counts (<5K)
- **Write tools** — permanent constraint; sending/editing/deleting expands prompt injection blast radius
- **Multi-account** — single session per deployment
- **Real-time notifications** — polling model only

---

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| TEL-01 | 6 | Complete |
| TEL-02 | 6 | Pending |
| TEL-03 | 6 | Pending |
| TEL-04 | 6 | Pending |
| CACHE-01 | 7 | Complete |
| CACHE-02 | 7 | Complete |
| CACHE-03 | 7 | Complete |
| NAV-01 | 8 | Complete |
| NAV-02 | 8 | Pending |
| TOPIC-01 | 9 | Complete |
| TOPIC-02 | 9 | Complete |
| TOPIC-03 | 9 | Complete |
| DEBT-01 | 10 | Pending |
| DEBT-02 | 10 | Pending |
| DEBT-03 | 10 | Pending |
