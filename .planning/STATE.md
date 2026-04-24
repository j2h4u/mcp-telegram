---
gsd_state_version: 1.0
milestone: v1.6
milestone_name: Local Mirror as Source of Truth
status: ready_to_plan
stopped_at: v1.6 roadmap created — ready to plan Phase 40
last_updated: "2026-04-24T10:50:53.642Z"
last_activity: 2026-04-24 -- Phase 999.1 execution started
progress:
  total_phases: 13
  completed_phases: 1
  total_plans: 4
  completed_plans: 0
  percent: 8
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-04-22)

**Core value:** LLM can work with Telegram using natural names — zero cold-start friction
**Current focus:** Phase 999.1.1 — unify-messages-table

## Current Position

Phase: 999.1.1
Plan: Not started
Status: Ready to plan
Last activity: 2026-04-24

Progress: [░░░░░░░░░░] 0%

## Deferred Items

Items acknowledged and carried forward at v1.5 close (2026-04-22):

| Category | Item | Status |
|----------|------|--------|
| quick_tasks | 1-resolver-redesign | parked (spike artifact) |
| quick_tasks | 2-code-review-fixes | parked (spike artifact) |
| quick_tasks | 3-implement-listunreadmessages-tool | parked (spike artifact) |
| quick_tasks | 260416-frw-add-getdialogstats-mcp-tool | shipped in v1.5, directory kept for history |
| quick_tasks | 260416-h4z-add-dialog-type-granularity-to-listdialogs | shipped in v1.5, directory kept for history |
| quick_tasks | 260416-ifp-fix-listunreadmessages-latency | superseded by later work |
| todos | 2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices | feed into v1.6 scope |
| todos | 2026-03-30-tool-audit-results-and-gap-analysis | feed into v1.6 scope |
| seeds | 001-native-telegram-channel-stats | carry forward |
| uat_gaps | 39.1 UAT | PASS recorded in STATE but no frontmatter marker; genuine PASS |
| uat_gaps | 39.2 UAT | PASS recorded in STATE but no frontmatter marker; genuine PASS |
| uat_gaps | 39.3 UAT | PASS recorded in STATE but no frontmatter marker; genuine PASS |
| arch_leak | ListDialogs/ListTopics live-Telegram fetch | v1.6 primary theme — addressed in Phases 44-45 |

## Accumulated Context

### Decisions

Key architectural constraint driving entire v1.5 design:

- One MTProto connection per account — sync-daemon owns TelegramClient exclusively; MCP server reads sync.db only (never calls `client.connect()`)
- SQLite WAL shared DB is the only IPC — no sockets, queues, or HTTP between containers
- Non-synced dialogs return explicit error — no silent partial-data degradation
- [Phase 24-sync-db-foundation]: schema_version integer-version table for explicit migration tracking (not implicit column-presence check)
- [Phase 24-sync-db-foundation]: fcntl bootstrap lock in ensure_sync_schema mirrors cache.py exactly — probe-then-lock pattern
- [Phase 24-sync-db-foundation]: asyncio Handle._run() used in SIGTERM test to invoke registered callback without sending actual OS signal
- [Phase 25]: MagicMock (not AsyncMock) for TelegramClient.is_connected() — synchronous method; AsyncMock creates unawaited coroutine in sync context
- [Phase 25]: monkeypatch pre-registration pattern for global mutation: register flag BEFORE test body mutates it so pytest teardown restores correct value
- [Phase 26-fullsyncworker]: FloodWait uses asyncio.wait_for(shutdown_event.wait()) not bare sleep — SIGTERM responsive during long rate-limit pauses
- [Phase 26-fullsyncworker]: Non-FloodWait RPCError skips dialog (is_done=True) — Phase 28 handles access_lost transitions
- [Phase 26-fullsyncworker]: Reactions serialized as JSON dict {emoji: count} — consistent with ReactionMetadataCache in cache.py
- [Phase 26-fullsyncworker]: process_one_batch() in tight loop (not per-heartbeat tick) with time.monotonic() interval for heartbeat — batches run at full speed
- [Phase 26-fullsyncworker]: Idle mode uses asyncio.wait_for(shutdown_event.wait(), timeout=HEARTBEAT_INTERVAL_S) — SIGTERM-responsive same pattern as FloodWait
- [Phase 27-event-handlers]: EventHandlerManager: callback-level dialog filtering via in-memory set (not Telethon chats= param) — dynamic enrollment without re-registering handlers
- [Phase 27-event-handlers]: EventHandlerManager registered BEFORE FullSyncWorker — INSERT OR REPLACE handles real-time/bulk overlap idempotently
- [Phase 27-event-handlers]: GAP_SCAN_INTERVAL_S initialized to time.monotonic() not negative-sentinel; gap scan test uses process_then_shutdown pattern to ensure loop body executes
- [Phase 28-deltasyncworker]: Access-loss tuple excludes ChatAdminRequiredError (write-permission error, not read-access loss per RESEARCH.md correction)
- [Phase 28-deltasyncworker]: DeltaSyncWorker uses entity= keyword arg for iter_messages; two SQL constants for distinct filter semantics (access_lost vs synced)
- [Phase 28-deltasyncworker]: catch_up parameter added to create_client() with default False — backward compat for non-daemon callers; daemon passes catch_up=True for PTS replay
- [Phase 28-deltasyncworker]: Daemon startup sequence: connect(catch_up=True) -> register handlers -> delta catch-up -> bootstrap DMs -> sync loop
- [Phase 29-02-wiring]: FTS5 INSERT OR REPLACE doesn't replace by content columns — edit updates use DELETE + INSERT pattern; DELETE_FTS_SQL added to fts.py
- [Phase 29-02-wiring]: FTS executemany inside same with-conn block as message executemany — one atomic transaction per batch covers both tables
- [Phase 29-02-wiring]: Updated daemon startup sequence: ensure_schema -> backfill_fts -> register_shutdown -> connect -> start_unix_server -> register_handlers -> delta_catch_up -> bootstrap_DMs -> sync_loop
- [Phase 30-sync-mcp-tools]: mark_dialog_for_sync enable=False resets status to 'not_synced' (no new 'disabled' status — avoids schema migration)
- [Phase 30-sync-mcp-tools]: delete_detection derived from dialog_id sign only — no Telegram API call, fast offline heuristic
- [Phase 32-01]: Inlined unread collection logic in daemon_api.py rather than importing capability_unread — keeps daemon self-contained
- [Phase 32-02]: Stale test_tools.py tests patching get_peer_id/telegram.create_client skipped (not deleted) — coverage moved to test_tool_routing.py and test_daemon_api.py
- [Phase 32-02]: _DaemonUnreadMessage uses __slots__ + _Sender helper matching reading.py _DaemonMessage pattern for consistency
- [Phase 32-02]: test_get_user_info_records_telemetry updated in-place (not skipped) — telemetry is MCP-layer concern, must remain tested
- [Phase 30-sync-mcp-tools]: get_sync_alerts since=caller-supplied timestamp, no server-side acknowledgement state
- [Phase 30-sync-mcp-tools]: MarkDialogForSync/GetSyncAlerts posture=primary, GetSyncStatus posture=secondary/helper
- [Phase 32-01-daemon-api-migration]: _list_unread_messages logic inlined in daemon_api.py (not imported from capability_unread) — keeps daemon self-contained, avoids cross-module dependency
- [Phase 32-01-daemon-api-migration]: Test patches GetCommonChatsRequest sentinel alongside _TELETHON_AVAILABLE — telethon not installed in test env; mirrors existing list_topics test pattern
- [Phase 33]: cli.py gets local get_entity_cache() inlined — debug tool not MCP surface; avoids re-exporting removed symbol from tools/
- [Phase 33]: upsert_entities in list_dialogs opens a second daemon_connection — asynccontextmanager closes writer on yield exit, cannot reuse
- [Phase 33]: test_get_user_info_records_telemetry skipped — telemetry now fire-and-forget via daemon IPC, not synchronous analytics collector
- [Phase 31]: format_usage_summary inlined into tools/stats.py — pure function, no extra imports needed
- [Phase 31]: USER_TTL/GROUP_TTL placed as module-level constants in daemon_api.py before import block
- [Phase 31]: Delete test_load.py: exclusively tested deleted TelemetryCollector (analytics.py)
- [Phase 31]: Remove DialogResolver type alias from models.py: used deleted EntityCache, never referenced
- [Phase 31]: _MockEntityCache inline class in conftest.py: same SQLite schema as deleted EntityCache, keeps resolver tests valid
- [Phase 31]: Use Any | None instead of object | None for cache params in resolver.py — object type blocks attribute access; Any preserves duck typing without deleted EntityCache
- [Phase 31]: healthcheck_daemon.py uses 4-byte length-prefixed JSON over Unix socket matching daemon_api.py protocol exactly
- [Phase 31]: Dockerfile healthcheck start-period increased from 15s to 30s — daemon needs time to connect to Telegram before socket is ready
- [Phase 34]: dialog_not_found ValueError messages left as-is: they contain only the caller-supplied dialog name, no system internals
- [Phase 34-02]: Delta sync stores already-fetched messages before sleeping FloodWait — returns preserved count not 0
- [Phase 34-02]: shutdown_event.set() precedes conn.rollback/close — prevents use-after-close in running coroutines
- [Phase 34-02]: Gap scan DELETE marks use 'with self._conn' per-batch — removes standalone commit(), matches existing pattern
- [Phase 34]: _clamp helper covers all four limit-accepting daemon handlers; stem_query quotes tokens to prevent FTS5 operator interpretation
- [Phase 34]: _daemon_not_running_text defined once in tools/_base.py — imported by all tool modules instead of duplicated
- [Phase 34]: event_handlers.py imports extract_message_row/serialize_reactions from sync_worker — FullSyncWorker thin wrappers removed
- [Phase 34]: _msg_to_dict @staticmethod on DaemonAPIServer — unread call site picks 5 keys from full dict (no flag parameter)
- [Phase 34]: request_id generated on client side (uuid4 hex[:8]) and echoed by server — avoids server-side state, preserves correlation across process boundary
- [Phase 34]: Phase planning codes removed from inline comments — descriptive text preserved, only the D-xx/Phase-N prefix stripped
- [Phase 35-01]: _build_list_messages_query() is module-level function (not method) — testable in isolation without DaemonAPIServer instance
- [Phase 35-01]: Navigation token dialog_id mismatch returns ok=False error (not silent ignore) — explicit contract validation
- [Phase 35-01]: sender_id filter takes precedence over sender_name when both supplied — avoids ambiguous compound filter
- [Phase 35-01]: edit_date in sync.db from MAX(message_versions.edit_date) subquery — returns latest edit timestamp per message
- [Phase 35-02]: topic_id resolved client-side via conn.list_topics before calling conn.list_messages — avoids a second daemon round-trip inside _list_messages
- [Phase 35-02]: unread=True flag resolved to read_inbox_max_id on daemon side via GetPeerDialogsRequest — daemon has TelegramClient, client side does not
- [Phase 35-02]: format_messages moved to module-level import in reading.py — enables patching in tests; no circular import
- [Phase 36-sync-coverage-and-access-recovery]: _UPDATE_PROGRESS_SQL split into two constants: _UPDATE_PROGRESS_SQL (4 params) and _UPDATE_PROGRESS_DONE_SQL (5 params with last_synced_at) to avoid writing last_synced_at on intermediate batches
- [Phase 36-sync-coverage-and-access-recovery]: probe-worker uses gap-fill-first pattern: gap-fill runs while status=access_lost, status reset to syncing only after success
- [Phase 36]: _compute_sync_coverage + _build_access_metadata helpers in daemon_api; access_lost dialogs routed to sync.db; global search omits dialog_access; warning text uses last_synced_at not access_lost_at
- [Phase 37]: serialize_reactions deleted in Plan 02 (Plan 01 preserved it for inter-wave compat); json import also removed
- [Phase 37]: _PreformattedReactions removal criterion: when reaction_names_map removed from MessageLike protocol in models.py (concrete condition, not just Phase 38)
- [Phase 37]: Global search returns empty reactions_display intentionally (cross-dialog result sets, complexity vs value)
- [Phase 39.1-01]: self_id cached on DaemonAPIServer instance (sync_main is a function, not a class); Plan 02 reads api_server.self_id for SQL binding
- [Phase 39.1-01]: sync.db schema v9 adds out + is_service INTEGER NOT NULL DEFAULT 0 on messages; O(1) ALTER TABLE, no row rewrite
- [Phase 39.1-02]: EFFECTIVE_SENDER_ID_SQL constant + dual e_raw/e_eff entity JOINs across all 5 read-path SELECTs; formatter.resolve_sender_label as single 5-branch source of truth shared by list_messages and search_messages
- [Phase 39.1-02]: _build_list_messages_query switched from positional-list params to named-dict params — required for mixing :self_id CASE with other filters in one SQLite query
- [Phase 39.1-03] UAT clean: DM smoke on 268071163 shows zero System: lines; Я branch verified forward on out=1 row (dialog 8583106747); group regression clean; phase 39.1 complete
- [Phase 39.3-04] UAT PASS: Doronin DM split form + both boundary markers live; natural split-form evidence on Nikolay (inbox-unread=4) and DM 152975038 (outbox-unread=3) — all four markers witnessed without operator synthesis
- [Phase 39.3-04] AC-6 outbox-side split-form upgraded from planned unit-test-only to LIVE-observed (natural state surfaced it)
- [Phase 39.3-04] AC D-03 bootstrap-pending form remains unit-test-backed only — Plan 02 outbox bootstrap converged to 0 NULL-outbox rows across 332 synced DMs before UAT probe
- [Phase 39.3-04] ListUnreadMessages now emits per-chat `[inbox: …]`/`[outbox: …]` headers (Plan 03 HIGH-3); plan's `limit=20` was a typo — schema enforces `limit ≥ 50`
- [Phase 39.3-04] 854 pytest tests passing (bar ≥ 820 exceeded)

### Roadmap Evolution

- Phase 999.1.1 inserted after Phase 999.1: unify messages table — merge activity_comments into messages, drop message_cache, add own_only status to synced_dialogs (INSERTED)
- Phase 32 added: Complete daemon API migration — migrate GetUserInfo and ListUnreadMessages to daemon API, remove all direct Telegram imports from tools/
- Phase 33 added: Consolidate all persistent state into daemon-owned sync.db — migrate entity_cache and analytics into sync.db, MCP server becomes fully stateless
- Phase 35 added: Daemon API Feature Parity — eliminate sync.db vs on-demand dual path, normalize message format, wire pagination/filters/reactions/edit markers/topic labels/search context
- v1.6 Phases 40-46 added 2026-04-21: dialogs snapshot table, bootstrap, events, reconciliation, ListDialogs/ListTopics SQL migration, tool surface audit

### Pending Todos

- 2 pending in `.planning/todos/pending`
- `2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices.md`
- `2026-03-13-carry-deferred-v1-1-cleanup-and-forum-validation.md`

### Blockers/Concerns

None active. v1.5 complete.

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 260416-frw | Add GetDialogStats MCP tool | 2026-04-16 | b53c41e | [260416-frw-add-getdialogstats-mcp-tool](./quick/260416-frw-add-getdialogstats-mcp-tool/) |
| 260416-h4z | Add dialog type granularity to ListDialogs | 2026-04-16 | 7af98d8 | [260416-h4z-add-dialog-type-granularity-to-listdialo](./quick/260416-h4z-add-dialog-type-granularity-to-listdialo/) |
| 260416-ifp | Fix ListUnreadMessages latency: replace iter_dialogs with GetPeerDialogsRequest for synced dialogs | 2026-04-16 | 71b5bf4 | [260416-ifp-fix-listunreadmessages-latency-replace-i](./quick/260416-ifp-fix-listunreadmessages-latency-replace-i/) |

## Session Continuity

Last session: 2026-04-21T00:00:00.000Z
Stopped at: v1.6 roadmap created — ready to plan Phase 40
Resume file: None

**Planned Phase:** Phase 40 — dialogs Snapshot Schema
