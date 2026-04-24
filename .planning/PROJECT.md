# mcp-telegram

## What This Is

MCP server that exposes Telegram as a set of tools for LLMs. Lets Claude read conversation
history, search messages with surrounding context, and look up contact info without the LLM
needing to know Telegram entity IDs. Built on Telethon (MTProto) with stdio transport,
deployable via Docker. Ships with fuzzy name resolution (WRatio), unified readable message
format, SQLite entity cache, and a complete read-only tool surface.

## Core Value

LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup
boilerplate before every real task.

## Current State

Latest shipped milestone: **v1.5 Persistent Sync** on 2026-04-22.

12 read-only MCP tools on Python 3.14, Telethon, MCP SDK, Docker stdio transport.
924 passing tests, mypy clean, ruff lint/format clean.

v1.5 delivered the sync-daemon architecture: standalone `mcp-telegram sync` owns the
TelegramClient; MCP server reads sync.db only. FTS5 full-text search with Russian
snowball stemming. Full message history persisted with edit/delete tracking, bidirectional
DM read-state, reactions live-sync + JIT freshen. 29/29 v1.5 requirements shipped.

## Next Milestone Goals

**v1.6 — Local Mirror as Source of Truth.** Enforce the "daemon = local mirror"
principle fully. Today `ListDialogs` and `ListTopics` still call Telegram live via
`iter_dialogs` / `GetForumTopicsRequest` — every invocation costs a ~2s RTT and
carries FloodWait risk. v1.6 adds a `dialogs` snapshot table with bootstrap on
daemon start, real-time event-driven updates, and periodic reconciliation, so
dialog listing becomes pure SQL and filtering pushes down to indexed columns.

Secondary v1.6 themes (to be shaped during `/gsd-new-milestone`):
- Tool surface audit (2026-03-30 todo) — map every remaining live-Telegram call in
  daemon_api and decide per call: cache, push to event-driven, or accept as inherently
  live (e.g., `photos.GetUserPhotos`, `GetFullUser`).
- Capability-oriented MCP tool refactor (2026-03-13 todo) — consolidate overlapping
  tools, sharpen descriptions for LLM discovery.
- Promote backlog items 999.1-999.4 as scope permits.

<details>
<summary>Prior state (v1.4)</summary>

Phase 34 complete — code quality kaizen applied 68 findings from 8-agent review.
Architecture: sync daemon owns TelegramClient + sync.db (14 API methods); MCP tools/ is fully
stateless — zero sqlite3 imports, zero disk writes. Entity resolution, telemetry recording,
and usage stats all route through daemon Unix socket IPC with request_id correlation.
FTS5 full-text search with Russian morphological stemming (quoted tokens prevent operator injection).
sync.db schema at v8 (synced_dialogs, messages, message_versions, entities, topic_metadata,
telemetry_events, message_reactions, message_entities, message_forwards, + read_inbox_max_id).
Phase 34 hardened: PII stripped from tool output, error messages sanitized, input bounds clamped,
shutdown ordering fixed, duplication eliminated (shared extract_message_row, _msg_to_dict,
_daemon_not_running_text), observability improved (request_id, 6 logging gaps closed).
Phase 37 complete — normalized data model: reactions/entities/forwards moved to child tables,
ExtractedMessage write path, _fetch_reaction_counts read path, emoji glyph reaction display.
Phase 38 complete — ListUnreadMessages makes zero Telegram API calls. read_inbox_max_id stored
per dialog in sync.db (schema v8), updated real-time via events.MessageRead (monotonic writes).
All three former live-API call sites replaced with pure SQL; bootstrap_pending field surfaces
dialogs not yet bootstrapped. FloodWait immunity on unread hot path.

</details>

## Requirements

### Validated

- ✓ List dialogs (chats, groups, channels) with filters — existing
- ✓ List messages from a dialog with pagination — existing
- ✓ Search messages by text query — existing
- ✓ stdio MCP transport — existing
- ✓ Docker deployment with HTTP/SSE via mcp-proxy — existing
- ✓ Interactive sign-in and session persistence — existing
- ✓ Name-based dialog resolution (fuzzy match, WRatio scorer) — v1.0
- ✓ Name-based sender resolution (same algorithm, same thresholds) — v1.0
- ✓ Unified message format across all tools (sessions, reactions, replies, media) — v1.0
- ✓ `ListDialogs` — `type` and `last_message_at` fields — v1.0
- ✓ `ListMessages` — readable format, cursor pagination, `sender` filter, `unread` filter — v1.0
- ✓ `SearchMessages` — offset-based pagination, results with ±3 message context — v1.0
- ✓ `GetMyAccount` tool — returns own name, id, username — v1.0
- ✓ `GetUserInfo` tool — returns profile + common chats list — v1.0
- ✓ Entity metadata cache (L2 SQLite, TTL-enforced) — users 30d, groups/channels 7d — v1.0
- ✓ Remove `GetDialog` tool (no stubs) — v1.0
- ✓ Remove `GetMessage` tool (no stubs) — v1.0
- ✓ `GetUsageStats` tool and privacy-safe `analytics.db` telemetry — v1.1
- ✓ Cache indexes, reaction metadata cache, and bounded cleanup strategy — v1.1
- ✓ `ListDialogs` archived-dialog discovery via `exclude_archived` semantics — v1.1
- ✓ `ListMessages` bidirectional navigation via `from_beginning` — v1.1
- ✓ Forum-topic support in `ListMessages` plus `ListTopics` dialog topic discovery — v1.1
- ✓ Grounded audit of the current MCP tool surface against MCP and Anthropic guidance — v1.2
- ✓ Option matrix for minimal, medium, and maximal redesign paths — v1.2
- ✓ Medium-path Pareto recommendation for the next implementation milestone — v1.2
- ✓ Implementation-ready sequencing memo with runtime validation gates and open questions — v1.2

- ✓ Actionable server-boundary error recovery with stage-aware detail — v1.3
- ✓ Capability-oriented internal seams for read, search, and topic behavior — v1.3
- ✓ Unified navigation contract replacing split cursor/offset/from_beginning — v1.3
- ✓ Direct read/search workflows reducing helper-first choreography — v1.3
- ✓ Surface posture classification (primary vs secondary) with runtime proof — v1.3
- ✓ Parallel-session-safe SQLite cache bootstrap — v1.3
- ✓ Privacy-safe telemetry verified after surface changes — v1.3

- ✓ `ListDialogs` members=N (participant count) for groups/channels — v1.4 (META-01)
- ✓ `ListDialogs` created=YYYY-MM-DD (creation date) for groups/channels — v1.4 (META-02)
- ✓ SQLite message cache (message_cache table, CachedMessage proxy, MessageLike Protocol) — v1.4 (CACHE-01, CACHE-02)
- ✓ Cache-first history reads for page 2+, bypass rules for newest/unread/search — v1.4 (CACHE-03–06, BYP-01–04)
- ✓ Edit detection with message_versions table and `[edited HH:mm]` formatter marker — v1.4 (EDIT-01–03)
- ✓ Background prefetch (next page + oldest page) and delta refresh on cache hits — v1.4 (PRE-01–05, REF-01–03)

### Active

## Current Milestone: v1.6 Local Mirror as Source of Truth

**Goal:** Enforce the "daemon = local mirror" principle architecturally. Every tool-level query that can be answered from sync.db is answered from sync.db, not from live Telegram. Eliminate residual ListDialogs / ListTopics live-fetch leaks; audit every remaining live-Telegram call site in daemon_api and classify it.

**Target features:**
- `dialogs` snapshot table in sync.db — name, type, archived, pinned, members, created, last_message_at, unread_count, updated_at (and optional raw_json later)
- Bootstrap on daemon start — one `iter_dialogs()` sweep populates the snapshot on first run (or resumable refresh)
- Real-time updates via event handlers — `NewMessage` (last_message_at), `UpdateReadHistoryInbox` (unread_count), `UpdateDialogPinned`, `UpdateDialogUnreadMark`, and friends
- Periodic reconciliation — hourly/daily sweep catches added-without-message dialogs and kicked/left reconciliation (soft-delete with `hidden=1`)
- `ListDialogs` migrated to pure SQL — filter becomes indexed SQL, fuzzy/acronym fallback runs on filtered subset; 0 Telegram API roundtrips per call
- `ListTopics` migrated to the same pattern (forum topics table with real-time updates)
- Tool surface audit — map every remaining `self._client.*` call in daemon_api.py and classify: mirror-to-DB / push-to-event-stream / accept as inherently live (`GetFullUser`, `photos.GetUserPhotos`, `GetDialogFilters`, etc.)
- Capability-oriented MCP tool surface refactor — consolidate overlapping tools, sharpen descriptions for LLM discovery (addresses 2026-03-13 todo)

### Backlog Candidates

- Deferred `v1.1` cleanup and large-forum validation.
- Broader Maximal-path tool-surface redesign after Medium migration has proven stable.
- Native eval or benchmark harnesses for measuring model burden reduction over time.

### Out of Scope

- Sending/editing/deleting messages — read-only by design (security invariant, not just product decision)
- Media download/streaming — format describes media, doesn't fetch it
- Real-time notifications / webhooks — polling model only
- Native HTTP/SSE transport — mcp-proxy covers this; deferred
- Multi-account support — single session per deployment
- ~~Message content caching~~ — shipped in v1.4: persistent SQLite cache with background prefetch
- Group membership table in entity cache — high staleness risk, no v1 tool depends on it
- `transliterate` dependency — rapidfuzz WRatio proved sufficient for Latin+Cyrillic; add only if validated against real contacts
- Maximal surface compression or large structured-output redesign — deferred until Medium migration has proven stable in production

## Context

Shipped runtime: `v1.4` — 8 MCP tools, ~6,600 LOC Python src + ~9,600 LOC tests, 393 passing tests.
Tech stack: Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz, SQLite (WAL).
Deployment: Docker-based with stdio MCP transport and `mcp-proxy` for HTTP/SSE access.
v1.5 progress: Phases 24-28 (sync infrastructure), Phase 31 (deployment wiring), Phase 34 (code quality kaizen — 68 findings, 6 plans, 3 waves) complete.

**Known deferred follow-ups:**
- Large-forum live validation using `.planning/phases/09-forum-topics-support/09-MANUAL-VALIDATION.md`
- `tz` param accepted by `format_messages()` but never passed at call sites — defaults to UTC
- Nyquist validation incomplete for v1.4 phases (19-23 all draft/missing)
- Cache analytics (hit/miss ratio, prefetch effectiveness) not yet instrumented

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Names as strings (not str\|int union type) | LLM always sends strings; Pydantic union type has MCP client compatibility risk | ✓ Good — no issues; str-only API works cleanly |
| WRatio scorer, thresholds 90/60 as named constants | Deterministic, handles partial matches; named constants allow tuning during test phase | ✓ Good — shipped, all test cases pass; thresholds not yet stress-tested against real contacts |
| Cursor pagination for ListMessages | message_id hidden -> before_id unusable; cursor stable under real-time inserts | ✓ Good — base64+JSON opaque token works; cursor error handling added in Phase 5 |
| Offset pagination for SearchMessages | Telegram search RPC uses add_offset, incompatible with max_id/cursor | ✓ Good — confirmed correct; offset pagination shipped |
| Channel sender = channel/group name | Anonymous posting has no user identity available | ✓ Good — correct fallback; no edge cases surfaced in testing |
| Two cache layers (L1 in-memory, L2 SQLite) | No message cache — messages always fresh; entity metadata safe to cache 30d | ✓ Good — shipped as designed; TTL enforcement added in Phase 5 |
| transliterate deferred | Validate need against real contacts first; rapidfuzz alone may be sufficient | ✓ Good — confirmed correct decision; no transliterate needed for v1 |
| GetUserInfo in v1 | Entity cache makes it cheap after first call; completes communication-map flow | ✓ Good — shipped cleanly with GetCommonChatsRequest |
| Remove GetDialog + GetMessage (no stubs) | No BC obligations; tools require IDs unavailable in new format | ✓ Good — clean removal, no issues |
| Read-only scope as security invariant | Prompt injection from message content cannot trigger write actions | ✓ Good — permanent constraint; enforced at architecture level |
| mcp-proxy stays for HTTP | Native HTTP/SSE deferred — proxy works, not worth disruption | ✓ Good — working in production |
| Pin Python 3.13 | pydantic-core (PyO3 0.22.6) cannot build against Python 3.14 (system default) | ✓ Good — .python-version pinned, reproducible builds |
| asyncio_mode=auto in pytest | Forward-compatible for future async tests | ✓ Good — no noise on sync tests, clean async test support |
| Use MCP/Anthropic docs as normative external guidance and reflection/code/tests as brownfield authority | Keeps research anchored to primary sources and live runtime reality | ✓ Good — produced a grounded audit instead of a literature review |
| Freeze the redesign baseline against the reflected seven-tool runtime | Stale planning notes were already drifting from the real surface | ✓ Good — all v1.2 artifacts share one authoritative baseline |
| Choose the Medium path as the next milestone | Removes a large share of model burden with the smallest safe change set | ✓ Good — adopted as the implementation direction |
| Require reflected-schema checks plus restarted-runtime freshness once public schemas move | Prevents stale container/runtime contracts after MCP-surface changes | ✓ Good — mandatory acceptance gate for the next coding milestone |
| Do not preserve backward compatibility by default for the Medium path | Cleaner contract is more valuable than shims unless a concrete client forces them | ✓ Good — shipped without shims; no client breakage observed |
| Capability-oriented seams behind public tools | Shared read/search/topic behavior evolves through one internal path | ✓ Good — tools are thin adapters, capability layer owns orchestration |
| Unified navigation contract (navigation/next_navigation) | One continuation vocabulary instead of cursor/offset/from_beginning | ✓ Good — both read and search use the same model |
| Direct selectors (dialog_id, topic_id) on public tools | Skip helper choreography when target is known | ✓ Good — reduces LLM steps for common workflows |
| Surface posture as code-level constant (TOOL_POSTURE) | Single source of truth for primary/secondary classification | ✓ Good — reflected in code, tests, and tool descriptions |
| Lock file for parallel cache bootstrap | Prevents SQLite contention across concurrent MCP sessions | ✓ Good — fixed production race condition |
| Structured field cache (not JSON blob) | Individual columns enable topic-aware coverage queries and range-based reads | ✓ Good — try_read_page uses WHERE clauses on structured fields |
| No TTL on message cache | Messages are near-immutable; delta refresh on access handles new messages | ✓ Good — avoids unnecessary re-fetches of stable data |
| Application-level edit versioning | INSERT OR REPLACE = DELETE+INSERT means SQLite BEFORE UPDATE trigger never fires | ✓ Good — Python-side diff before executemany works reliably |
| Fire-and-forget prefetch via asyncio.create_task | Non-blocking; dedup set prevents redundant API calls | ✓ Good — response latency unaffected by background work |
| Same SQLite DB for message cache | Reuses entity_cache.db connection — no new DB file or connection pool | ✓ Good — single bootstrap path, lock file covers both |

## Constraints

- **Tech stack**: Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz — no new heavy deps
- **Fuzzy matching**: rapidfuzz only (transliterate still deferred)
- **Privacy**: No real user IDs, names, or usernames in planning docs or code comments
- **Read-only**: Permanent constraint — write tools expand prompt injection blast radius dramatically

<details>
<summary>Archived v1.4 milestone notes</summary>

**Goal:** Persistent SQLite message cache with background prefetch to reduce Telegram API calls and speed up repeated reads.

**Delivered:**
- Dialog metadata enrichment (members count, creation date for groups/channels)
- SQLite message_cache table with CachedMessage proxy satisfying MessageLike Protocol
- Cache-first history reads for page 2+, with bypass rules for newest/unread/search
- Edit detection via message_versions table and [edited HH:mm] formatter marker
- Background prefetch (next page + oldest page on first access, delta refresh on cache hits)

</details>

<details>
<summary>Archived v1.3 milestone notes</summary>

**Goal:** Implement the Medium-path MCP surface refactor in small verified steps, keeping the
migration bounded and observable instead of turning it into a speculative Maximal rewrite.

**Delivered:**
- Actionable server-boundary error recovery replacing generic failure collapse
- Capability-oriented internal seams for read, search, and topic behavior
- Unified navigation contract across read and search workflows
- Direct read/search workflows reducing helper-first choreography
- Surface posture classification with runtime proof and privacy verification

</details>

<details>
<summary>Archived v1.2 milestone notes</summary>

**Goal:** Research MCP and Anthropic tool-design best practices, audit the current Telegram MCP
surface against them, and produce grounded recommendations for a future refactor milestone.

**Delivered:**
- Comparative audit of the current model-facing MCP surface against external best practices and primary-source guidance
- Refactor option set covering minimal, medium, and maximal redesign paths
- Pareto-style recommendation for the smallest safe change set likely to deliver most of the model-usage impact
- Migration guidance and decision criteria for the follow-up implementation milestone

</details>

---
*Last updated: 2026-04-24 after Phase 999.1 complete*
