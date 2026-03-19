# Requirements: mcp-telegram

**Defined:** 2026-03-20
**Core Value:** LLM can work with Telegram using natural names — zero cold-start friction

## Development Approach

**TDD** — tests written before implementation. Each phase starts with test contracts defining expected behavior, then code is written to satisfy them. Not ceremonial red-green-refactor on every line, but the direction: define contract first, implement second.

## v1.4 Requirements

Requirements for Message Cache milestone. Primary goal: speed (weight 1.0), secondary: reduce API abuse (weight 0.5).

### Message Cache

- [x] **CACHE-01**: MessageCache SQLite table with structured fields (dialog_id, message_id, sent_at, text, sender_id, sender_first_name, media_description, reply_to_msg_id, forum_topic_id, edit_date, fetched_at). WITHOUT ROWID, PK (dialog_id, message_id).
- [x] **CACHE-02**: CachedMessage proxy class with nested attribute objects (.sender.first_name, .reply_to.reply_to_msg_id) satisfying MessageLike Protocol — transparent to formatter
- [x] **CACHE-03**: Cache-first reads in capability_history for paginated pages (page 2+). navigation="newest" always goes to Telegram API (never served stale)
- [x] **CACHE-04**: Cache coverage tracking per (dialog_id, topic_id) — knows which message_id ranges are cached. Topic-aware because messages from different topics interleave by ID
- [x] **CACHE-05**: Cache population — every Telegram API fetch writes results to MessageCache before returning. Reply map also served from cache when possible
- [x] **CACHE-06**: No TTL expiration — messages are near-immutable, cache grows indefinitely. PRAGMA optimize on bootstrap
- [x] **CACHE-07**: Same SQLite DB file as entity_cache.db — extend existing bootstrap, no separate connection

### Edit Detection

- [x] **EDIT-01**: message_versions table (dialog_id, message_id, version, old_text, edit_date) for tracking text changes
- [x] **EDIT-02**: Application-level versioning in Python — before INSERT OR REPLACE, compare text with cached version, write to message_versions if changed. No SQLite trigger (INSERT OR REPLACE = DELETE + INSERT, BEFORE UPDATE trigger never fires)
- [x] **EDIT-03**: Formatter shows [edited HH:mm] marker on messages where edit_date IS NOT NULL. No separate is_edited column — derived from edit_date

### Prefetch

- [ ] **PRE-01**: On first ListMessages for a dialog: prefetch next page (current direction) + oldest page in background via asyncio.create_task
- [ ] **PRE-02**: On any subsequent page read: prefetch next page in current direction
- [ ] **PRE-03**: When reading oldest page: prefetch next page forward (old→new direction)
- [x] **PRE-04**: Prefetch results stored in MessageCache (same path as regular cache population)
- [x] **PRE-05**: Prefetch deduplication — in-memory set of (dialog_id, direction, anchor_id) prevents duplicate API calls for same page

### Lazy Refresh

- [ ] **REF-01**: On cache hit for paginated pages, background delta refresh via asyncio.create_task — fetch new messages since last_cached_message_id
- [x] **REF-02**: Delta fetch uses iter_messages(min_id=last_cached_id) to pull only new messages
- [x] **REF-03**: No timer-based refresh — refresh only on access (zero API calls for inactive dialogs)

### Cache Bypasses

- [x] **BYP-01**: navigation="newest" (first page) always fetches from Telegram API — never served stale from cache
- [x] **BYP-02**: unread=True in ListMessages always fetches live (read state changes in real time)
- [x] **BYP-03**: ListUnreadMessages always fetches live (entire tool is real-time unread state)
- [x] **BYP-04**: SearchMessages always fetches live (server-side text search, not cacheable). Results written to cache for future ListMessages hits

### Dialog Metadata

- [ ] **META-01**: ListDialogs output includes members=N for groups/channels (from entity.participants_count)
- [ ] **META-02**: ListDialogs output includes created=YYYY-MM-DD for groups/channels (from entity.date)

## Future Requirements

### Cache Optimization (B-path delta for newest)
- **COPT-01**: Delta fetch for newest page — when returning to a recently visited dialog, fetch only new messages since last cached instead of full page

### Cache Analytics
- **CANA-01**: Cache hit/miss ratio tracked in telemetry
- **CANA-02**: Prefetch effectiveness metric (prefetch hit vs wasted fetch)

### Cache Management
- **CMGMT-01**: CLI command to inspect cache size and stats
- **CMGMT-02**: CLI command to clear cache for specific dialog or all

### Edit History
- **EHIST-01**: Tool or parameter to view edit history for a specific message

### Topic Research
- **TOPIC-01**: Research Telegram API 9.4+ topic support in personal chats (bots, premium) and Telethon coverage

## Out of Scope

| Feature | Reason |
|---------|--------|
| Search result caching | Search is server-side (Telegram does text matching), can't serve search queries from local cache |
| Timer-based background refresh | Wastes API calls for inactive dialogs; refresh-on-access is sufficient |
| Edit diff viewer tool | Defer until edit detection proves useful in practice; [edited] marker is enough for v1.4 |
| SQLite trigger for versioning | INSERT OR REPLACE = DELETE + INSERT; BEFORE UPDATE trigger never fires. Use application-level versioning |
| is_edited column | Redundant — edit_date IS NOT NULL is sufficient |
| Deleted message detection | Telegram doesn't surface deletions via iter_messages; detecting gaps is unreliable. Accept for now |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| CACHE-01 | Phase 20 | Complete |
| CACHE-02 | Phase 20 | Complete |
| CACHE-03 | Phase 21 | Complete |
| CACHE-04 | Phase 21 | Complete |
| CACHE-05 | Phase 21 | Complete |
| CACHE-06 | Phase 21 | Complete |
| CACHE-07 | Phase 20 | Complete |
| EDIT-01 | Phase 22 | Complete |
| EDIT-02 | Phase 22 | Complete |
| EDIT-03 | Phase 22 | Complete |
| PRE-01 | Phase 23 | Pending |
| PRE-02 | Phase 23 | Pending |
| PRE-03 | Phase 23 | Pending |
| PRE-04 | Phase 23 | Complete |
| PRE-05 | Phase 23 | Complete |
| REF-01 | Phase 23 | Pending |
| REF-02 | Phase 23 | Complete |
| REF-03 | Phase 23 | Complete |
| BYP-01 | Phase 21 | Complete |
| BYP-02 | Phase 21 | Complete |
| BYP-03 | Phase 21 | Complete |
| BYP-04 | Phase 21 | Complete |
| META-01 | Phase 19 | Pending |
| META-02 | Phase 19 | Pending |

**Coverage:**
- v1.4 requirements: 24 total
- Mapped to phases: 24
- Unmapped: 0

---
*Requirements defined: 2026-03-20*
*Last updated: 2026-03-20 after roadmap creation*
