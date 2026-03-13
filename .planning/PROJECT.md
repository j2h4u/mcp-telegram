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

Latest shipped milestone: `v1.2 MCP Surface Research` on 2026-03-13.

The live product surface is still the shipped `v1.1` runtime: 7 read-only MCP tools on Python 3.13,
Telethon, the MCP SDK, SQLite caches, and Docker + `mcp-proxy`, with 169 passing tests captured
before the research milestone began.

`v1.2` added no runtime behavior. It produced the evidence hierarchy, comparative audit, option
matrix, Pareto recommendation, and implementation memo that now define the next coding milestone.

The active milestone is `v1.3 Medium Implementation`. It turns the `v1.2` Medium-path
recommendation into a bounded coding milestone that starts at Phase 14 and ships through explicit
reflection, restarted-runtime, and privacy-safe telemetry gates.

## Current Milestone: v1.3 Medium Implementation

**Goal:** Implement the Medium-path MCP surface refactor in small verified steps, keeping the
migration bounded and observable instead of turning it into a speculative Maximal rewrite.

**Target features:**
- Clean up server-boundary failures so escaped tool errors keep actionable recovery direction.
- Introduce capability-oriented internal seams behind the public tool adapters.
- Unify continuation and reshape primary read/search workflows with restarted-runtime verification.

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

### Active

- [ ] Ship the bounded Medium-path implementation milestone from the `v1.2` memo.
- [ ] Reduce helper-step burden in primary read/search workflows without losing topic fidelity.
- [ ] Prove every public-contract move through tests, reflected schemas, restarted runtime, and privacy-safe telemetry checks.

### Backlog Candidates

- Deferred `v1.1` cleanup and large-forum validation if they do not block the Medium implementation path.
- Broader Maximal-path tool-surface redesign after the Medium migration lands cleanly.
- Native eval or benchmark harnesses for measuring model burden reduction over time.

### Out of Scope

- Sending/editing/deleting messages — read-only by design (security invariant, not just product decision)
- Media download/streaming — format describes media, doesn't fetch it
- Real-time notifications / webhooks — polling model only
- Native HTTP/SSE transport — mcp-proxy covers this; deferred
- Multi-account support — single session per deployment
- Message content caching — messages always fetched fresh from API
- Group membership table in entity cache — high staleness risk, no v1 tool depends on it
- `transliterate` dependency — rapidfuzz WRatio proved sufficient for Latin+Cyrillic; add only if validated against real contacts
- Backward-compatibility shims by default — cleaner Medium contract wins unless a concrete client constraint forces compatibility work
- Maximal surface compression or large structured-output redesign — explicitly deferred until after the Medium migration proves the new seams

## Context

Shipped runtime remains `v1.1`: 7 MCP tools and 169 passing tests.
Tech stack: Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz, SQLite (WAL).
Deployment remains Docker-based with stdio MCP transport and `mcp-proxy` for HTTP/SSE access.

**v1.2 outcomes now available for planning:**
- Retained-source evidence hierarchy and brownfield baseline for future tool-surface work.
- Comparative audit of the seven-tool MCP surface across tool-level and workflow-level pressure points.
- Medium-path recommendation with explicit rejected alternatives and bounded Maximal prep.
- Implementation memo defining sequencing, open questions, and runtime freshness gates.

**Known deferred follow-ups:**
- Large-forum live validation using `.planning/phases/09-forum-topics-support/09-MANUAL-VALIDATION.md`
- `tz` param accepted by `format_messages()` but never passed at call sites — defaults to UTC
- Dead imports in `tools.py:18` (TelegramClient, custom, functions, types)
- `EntityCache.all_names()` orphaned by `all_names_with_ttl()` — safe to remove
- Reflected tool-schema freshness remains an operational risk until the follow-on implementation milestone executes rebuild/restart verification.
- Phase VALIDATION artifacts for 10-13 remain partial even though the milestone audit passed with `tech_debt` status.

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
| Do not preserve backward compatibility by default for the Medium path | Cleaner contract is more valuable than shims unless a concrete client forces them | — Pending — validate against implementation constraints during the next milestone |

## Constraints

- **Tech stack**: Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz — no new heavy deps
- **Fuzzy matching**: rapidfuzz only (transliterate still deferred)
- **Privacy**: No real user IDs, names, or usernames in planning docs or code comments
- **Read-only**: Permanent constraint — write tools expand prompt injection blast radius dramatically

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
*Last updated: 2026-03-14 after starting milestone v1.3*
