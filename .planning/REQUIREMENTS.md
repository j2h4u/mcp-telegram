# Requirements: mcp-telegram v1.6 — Local Mirror as Source of Truth

**Defined:** 2026-04-22
**Previous milestone:** v1.5 Persistent Sync (archived to `.planning/milestones/v1.5-REQUIREMENTS.md`)

## v1.6 Requirements

### Dialog Snapshot (MIRROR)

- [ ] **MIRROR-01**: sync.db has a `dialogs` table with snapshot columns: `dialog_id`, `name`, `type`, `archived`, `pinned`, `members`, `created`, `last_message_at`, `snapshot_at`, `hidden`
- [ ] **MIRROR-02**: Schema migration v12→v13 adds `dialogs` table and predicate helper `_dialogs_snapshot_populated()` for bootstrap idempotency
- [ ] **MIRROR-03**: `dialogs` is a separate table from `synced_dialogs` (sync machinery) and `entities` (sender data) — concerns evolve independently
- [ ] **MIRROR-04**: Soft-delete via `hidden=1` on kick/leave; row preserved as anchor for the messages chain
- [ ] **MIRROR-05**: `unread_count` is NOT stored in `dialogs` — remains computed via `_BATCHED_UNREAD_COUNTS_SQL` (local cursor is authoritative)

### Bootstrap (BOOTSTRAP)

- [ ] **BOOTSTRAP-01**: Daemon on first start after v1.6 upgrade runs a single `iter_dialogs()` sweep and populates the `dialogs` table
- [ ] **BOOTSTRAP-02**: Bootstrap runs as a background `asyncio.Task` and does NOT block the `/health` endpoint
- [ ] **BOOTSTRAP-03**: FloodWait handling — the sweep sleeps via `asyncio.wait_for(shutdown_event.wait(), timeout=exc.seconds)` and resumes, does not restart
- [ ] **BOOTSTRAP-04**: Resumable via cursor checkpoint — a partial sweep on daemon restart continues from the last processed dialog, not from scratch
- [ ] **BOOTSTRAP-05**: Event handlers are registered BEFORE the bootstrap sweep starts, so live events never overwrite fresher snapshot data with stale bootstrap data
- [ ] **BOOTSTRAP-06**: Bootstrap uses `INSERT OR IGNORE` + recency guard (`WHERE dialogs.snapshot_at < excluded.snapshot_at`) — never clobbers newer event-written rows

### Real-time Event Handlers (EVENTS)

- [ ] **EVENTS-01**: New `events.Raw` handlers for `UpdateDialogPinned`, `UpdatePinnedDialogs`, `UpdateDialogUnreadMark` update `dialogs.pinned` and related flags
- [ ] **EVENTS-02**: New `events.Raw` handlers for `UpdateReadHistoryInbox` and `UpdateReadChannelInbox` capture `still_unread_count` (the high-level `events.MessageRead` wrapper drops this field)
- [ ] **EVENTS-03**: New handlers for `UpdateChannel` / `UpdateChat` set `dialogs.needs_refresh=1` (dirty flag, no immediate API call — reconciliation picks it up)
- [ ] **EVENTS-04**: `on_new_message` forward-writes `dialogs.last_message_at` (monotonic — only when `new_date > existing_date`)
- [ ] **EVENTS-05**: Forum topic create/edit captured via `NewMessage` with `MessageActionTopicCreate` / `MessageActionTopicEdit` and `ForumTopicDeleted` / `UpdatePinnedForumTopic` Raw handlers write to `forum_topics` table

### Reconciliation (RECON)

- [ ] **RECON-01**: New module `src/mcp_telegram/dialog_sync.py` hosts reconciliation loop, mirroring `delta_sync.py` structure and lifecycle
- [ ] **RECON-02**: Hourly light pass — fetches only metadata-changed dialogs (via `UpdateChannelTooLong` / `needs_refresh=1` dirty flags)
- [ ] **RECON-03**: Daily full pass — complete `iter_dialogs()` sweep with soft-delete of missing rows (kicked/left dialogs → `hidden=1`)
- [ ] **RECON-04**: `synced_dialogs.status='access_lost'` transition atomically sets `dialogs.hidden=1` in the same transaction
- [ ] **RECON-05**: Reconciliation loop is FloodWait-tolerant and shutdown-responsive (same patterns as `run_access_probe_loop`)

### ListDialogs Migration (LISTDIALOGS)

- [ ] **LISTDIALOGS-01**: `ListDialogs` tool reads exclusively from `dialogs` table — zero Telegram API calls per invocation
- [ ] **LISTDIALOGS-02**: `filter` parameter pushes down to SQL (`LIKE '%...%' COLLATE NOCASE` on indexed columns)
- [ ] **LISTDIALOGS-03**: Fuzzy fallback (acronym match, `partial_ratio` ≥ 80) runs on the SQL-filtered subset, not over the full Telegram result
- [ ] **LISTDIALOGS-04**: When `snapshot_at` is older than 12h the output includes a `snapshot_age=Xh` annotation (SWR pattern — no blocking live fetch)

### ListTopics Migration (LISTTOPICS)

- [ ] **LISTTOPICS-01**: `forum_topics` snapshot table with `dialog_id`, `topic_id`, `title`, `icon_emoji_id`, `pinned`, `date`, `hidden`, `snapshot_at` columns
- [ ] **LISTTOPICS-02**: `ListTopics` tool reads exclusively from `forum_topics` table — zero Telegram API calls per invocation
- [ ] **LISTTOPICS-03**: Topic title changes reconciled via targeted `GetForumTopicsRequest` (no real-time event carries the new title)

### Differentiator Fields (DIFF)

- [ ] **DIFF-01**: `dialogs` snapshot carries `unread_mentions_count` from `iter_dialogs()` result
- [ ] **DIFF-02**: `dialogs` snapshot carries `unread_reactions_count` from `iter_dialogs()` result
- [ ] **DIFF-03**: `dialogs` snapshot carries `draft_text` (truncated to first 80 chars)
- [ ] **DIFF-04**: `ListDialogs` exposes `mentions=N reactions=N draft="..."` inline on rows when non-zero/non-empty

### Tool Surface Audit (AUDIT)

- [ ] **AUDIT-01**: `.planning/TOOL-SURFACE-AUDIT.md` enumerates every `self._client.*` call site in `daemon_api.py` post-v1.6
- [ ] **AUDIT-02**: Each call site classified as `mirror-to-db` / `push-via-event` / `inherently-live` with rationale and future-work disposition

## Future Requirements

- Capability-oriented MCP tool surface refactor (todo 2026-03-13) — deferred to v1.7
- Native Telegram channel stats (SEED-001) — deferred to v1.7 when analytics scope is opened
- Raw JSON blob storage for entities/messages — deferred; revisit if v1.6 reveals concrete field-drift pain

## Out of Scope

- `unread_count` denormalization in `dialogs` — local cursor path already authoritative (MIRROR-05 is explicit)
- `folder_id` / `GetDialogFilters` integration — requires separate folders table + filter-change listener; not in v1.6 theme
- `last_online_at` on DM peer rows in `dialogs` — user-entity field, privacy-gated, already covered by `GetUserInfo`
- Scheduled-job library — asyncio loops in daemon are sufficient; no new dependency
- Deletion without preserving messages — messages rows must always have an anchor `dialogs` row (enforced via soft-delete)

## Traceability

| Requirement ID | Phase | Status |
|----------------|-------|--------|
| MIRROR-01 | Phase 40 | Pending |
| MIRROR-02 | Phase 40 | Pending |
| MIRROR-03 | Phase 40 | Pending |
| MIRROR-04 | Phase 40 | Pending |
| MIRROR-05 | Phase 40 | Pending |
| DIFF-01 | Phase 40 | Pending |
| DIFF-02 | Phase 40 | Pending |
| DIFF-03 | Phase 40 | Pending |
| BOOTSTRAP-01 | Phase 41 | Pending |
| BOOTSTRAP-02 | Phase 41 | Pending |
| BOOTSTRAP-03 | Phase 41 | Pending |
| BOOTSTRAP-04 | Phase 41 | Pending |
| BOOTSTRAP-05 | Phase 41 | Pending |
| BOOTSTRAP-06 | Phase 41 | Pending |
| EVENTS-01 | Phase 42 | Pending |
| EVENTS-02 | Phase 42 | Pending |
| EVENTS-03 | Phase 42 | Pending |
| EVENTS-04 | Phase 42 | Pending |
| EVENTS-05 | Phase 42 | Pending |
| RECON-01 | Phase 43 | Pending |
| RECON-02 | Phase 43 | Pending |
| RECON-03 | Phase 43 | Pending |
| RECON-04 | Phase 43 | Pending |
| RECON-05 | Phase 43 | Pending |
| LISTDIALOGS-01 | Phase 44 | Pending |
| LISTDIALOGS-02 | Phase 44 | Pending |
| LISTDIALOGS-03 | Phase 44 | Pending |
| LISTDIALOGS-04 | Phase 44 | Pending |
| DIFF-04 | Phase 44 | Pending |
| LISTTOPICS-01 | Phase 45 | Pending |
| LISTTOPICS-02 | Phase 45 | Pending |
| LISTTOPICS-03 | Phase 45 | Pending |
| AUDIT-01 | Phase 46 | Pending |
| AUDIT-02 | Phase 46 | Pending |
