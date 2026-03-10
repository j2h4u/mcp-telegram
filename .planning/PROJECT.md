# mcp-telegram

## What This Is

MCP server that exposes Telegram as a set of tools for LLMs. Lets Claude read conversation
history, search messages, and look up contact info — without the LLM needing to know Telegram
entity IDs. Built on Telethon (MTProto) with stdio transport, deployable via Docker.

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

### Active

- [ ] Name-based dialog resolution (fuzzy match, WRatio scorer)
- [ ] Name-based sender resolution (same algorithm, same thresholds)
- [ ] Unified message format across all tools (sessions, reactions, replies, media)
- [ ] `ListDialogs` — add `type` and `last_message_at` fields
- [ ] `ListMessages` — readable format, cursor pagination, `sender` filter, `unread` filter
- [ ] `SearchMessages` — offset-based pagination, results with ±3 message context
- [ ] `GetMe` tool — returns own name, id, username
- [ ] `GetUserInfo` tool — returns profile + common chats list
- [ ] Entity metadata cache (L2 SQLite) — users, groups, channels; lazy-populated
- [ ] Remove `GetDialog` tool (no stubs needed — no BC obligations)
- [ ] Remove `GetMessage` tool (no stubs needed — no BC obligations)

### Out of Scope

- Sending/editing/deleting messages — read-only by design (security invariant, not just product decision)
- Media download/streaming — format describes media, doesn't fetch it
- Real-time notifications / webhooks — polling model only
- Native HTTP/SSE transport — mcp-proxy covers this; deferred
- Multi-account support — single session per deployment
- Message content caching — messages always fetched fresh from API
- Group membership table in entity cache — high staleness risk, no v1 tool depends on it
- `transliterate` dependency — defer until empirically validated against real contacts

## Context

Brownfield project. Existing codebase uses `@singledispatch` pattern for tool routing,
Pydantic models for tool arguments and schema generation. Tools layer in `src/mcp_telegram/tools.py`,
server layer in `src/mcp_telegram/server.py`.

### Cache Architecture (two layers)

**L1 — in-memory dialog list** (existing plan):
- Dialog name→id mapping for fuzzy resolver
- TTL: 5 min
- Evicted when TTL expires; call counter to prevent FloodWait in agent loops

**L2 — SQLite entity store** (`~/.local/state/mcp-telegram/entity_cache.db`):
- Schema: `entities(id PK, type, name, username, updated_at)`
- Populated lazily: every API response that returns an entity → upsert
- TTL: 30 days for users, 7 days for groups/channels (soft expiry, lazy refresh)
- Telethon session SQLite already caches entities for `get_entity()` lookups —
  L2 adds persistence across process restarts and human-readable debug access

### Name Resolution Algorithm

- Input: `str` always (LLM sends string even for numeric IDs)
- If `query.isdigit()` → parse as int, skip fuzzy match
- Otherwise: WRatio scorer via rapidfuzz, normalize to lowercase
- Thresholds: ≥90 auto-resolve (but if ≥2 candidates both ≥90 → ambiguity), 60–89 return candidates, <60 not found
- Output prefix in tool output: `[резолв: "query" → Name, id:N]`
- `transliterate` dependency deferred — add only after validating need against real contact list

### Message Format

`HH:mm FirstName: text  [reactions]`
- Date header on day change (timezone via `TELEGRAM_TZ` env var, default UTC)
- Session break `--- N мин ---` at gaps >60 min
- Reply: `[↑ Name HH:mm]` if parent in current output window, `[↑ вне диапазона]` if not
- Reply annotation requires pre-loading message set by ID before formatting (no API calls in formatter)
- Channel/anonymous-admin sender: use channel or group name instead of FirstName
- Media replaces text: `[фото]`, `[документ: name.pdf, 240KB]`, `[голосовое: 0:34]`
- Reactions inline: `[👍 Name]`, `[👍×3: A, B, C]`
- `message_id` never exposed in output

### Pagination

- **ListMessages**: cursor-based (opaque base64 JSON tokens `{id, dialog_id}`) — stable under real-time message arrival
- **SearchMessages**: offset-based (`offset: int = 0`, `next_offset` absent when exhausted) — Telegram search RPC uses `add_offset`, not `max_id`; these are incompatible
- **ListDialogs**: no pagination

### sender Filter (ListMessages)

- `sender: str` resolves first against entity cache / dialogs
- If not found there: resolve against group participants via Telethon API
- Document limitation: resolution may fail for users with no shared history

## Constraints

- **Tech stack**: Python 3.11+, Telethon, MCP SDK, Pydantic v2 — no new heavy deps
- **Fuzzy matching**: rapidfuzz only for v1 (transliterate deferred)
- **Privacy**: No real user IDs, names, or usernames in planning docs or code comments
- **Read-only**: Permanent constraint — write tools expand prompt injection blast radius dramatically

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Names as strings (not str\|int union type) | LLM always sends strings; Pydantic union type has MCP client compatibility risk | — Pending |
| WRatio scorer, thresholds 90/60 as named constants | Deterministic, handles partial matches; named constants allow tuning during test phase | — Pending |
| Cursor pagination for ListMessages | message_id hidden → before_id unusable; cursor stable under real-time inserts | — Pending |
| Offset pagination for SearchMessages | Telegram search RPC uses add_offset, incompatible with max_id/cursor | — Pending |
| Channel sender = channel/group name | Anonymous posting has no user identity available | — Pending |
| Two cache layers (L1 in-memory, L2 SQLite) | No message cache — messages always fresh; entity metadata safe to cache 30d | — Pending |
| transliterate deferred | Validate need against real contacts first; rapidfuzz alone may be sufficient | — Pending |
| GetUserInfo in v1 | Entity cache makes it cheap after first call; completes communication-map flow | — Pending |
| Remove GetDialog + GetMessage (no stubs) | No BC obligations; tools require IDs unavailable in new format | — Pending |
| Read-only scope as security invariant | Prompt injection from message content cannot trigger write actions | — Pending |
| mcp-proxy stays for HTTP | Native HTTP/SSE deferred — proxy works, not worth disruption | — Pending |

---
*Last updated: 2026-03-11 after expert panel review (architecture, UX, cache, pagination)*
