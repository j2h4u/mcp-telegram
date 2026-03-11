# mcp-telegram

## What This Is

MCP server that exposes Telegram as a set of tools for LLMs. Lets Claude read conversation
history, search messages with surrounding context, and look up contact info — without the LLM
needing to know Telegram entity IDs. Built on Telethon (MTProto) with stdio transport, deployable
via Docker. Ships with fuzzy name resolution (WRatio), unified readable message format, SQLite
entity cache, and a complete read-only tool surface.

## Core Value

LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup
boilerplate before every real task.

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
- ✓ `GetMe` tool — returns own name, id, username — v1.0
- ✓ `GetUserInfo` tool — returns profile + common chats list — v1.0
- ✓ Entity metadata cache (L2 SQLite, TTL-enforced) — users 30d, groups/channels 7d — v1.0
- ✓ Remove `GetDialog` tool (no stubs) — v1.0
- ✓ Remove `GetMessage` tool (no stubs) — v1.0

### Active

*(None — v1.0 shipped complete. Next milestone requirements TBD via `/gsd:new-milestone`.)*

### Out of Scope

- Sending/editing/deleting messages — read-only by design (security invariant, not just product decision)
- Media download/streaming — format describes media, doesn't fetch it
- Real-time notifications / webhooks — polling model only
- Native HTTP/SSE transport — mcp-proxy covers this; deferred
- Multi-account support — single session per deployment
- Message content caching — messages always fetched fresh from API
- Group membership table in entity cache — high staleness risk, no v1 tool depends on it
- `transliterate` dependency — rapidfuzz WRatio proved sufficient for Latin+Cyrillic; add only if validated against real contacts

## Context

Shipped v1.0 with 1,246 LOC Python (src/).
Tech stack: Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz, SQLite (WAL).
57 tests green. TDD throughout: RED → GREEN → REFACTOR on every plan.

**Known tech debt (from v1.0 audit):**
- `tz` param accepted by `format_messages()` but never passed at call sites — defaults to UTC
- Dead imports in `tools.py:18` (TelegramClient, custom, functions, types)
- `EntityCache.all_names()` orphaned by `all_names_with_ttl()` — safe to remove

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Names as strings (not str\|int union type) | LLM always sends strings; Pydantic union type has MCP client compatibility risk | ✓ Good — no issues; str-only API works cleanly |
| WRatio scorer, thresholds 90/60 as named constants | Deterministic, handles partial matches; named constants allow tuning during test phase | ✓ Good — shipped, all test cases pass; thresholds not yet stress-tested against real contacts |
| Cursor pagination for ListMessages | message_id hidden → before_id unusable; cursor stable under real-time inserts | ✓ Good — base64+JSON opaque token works; cursor error handling added in Phase 5 |
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

## Constraints

- **Tech stack**: Python 3.13, Telethon, MCP SDK, Pydantic v2, rapidfuzz — no new heavy deps
- **Fuzzy matching**: rapidfuzz only (transliterate still deferred)
- **Privacy**: No real user IDs, names, or usernames in planning docs or code comments
- **Read-only**: Permanent constraint — write tools expand prompt injection blast radius dramatically

---
*Last updated: 2026-03-11 after v1.0 milestone*
