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

- [ ] Name-based dialog resolution (fuzzy match, transliteration support)
- [ ] Name-based sender resolution (same algorithm)
- [ ] Unified message format across all tools (sessions, reactions, replies, media)
- [ ] `ListDialogs` — add `type` and `last_message_at` fields
- [ ] `ListMessages` — readable format, cursor pagination, `sender` filter, unread filter
- [ ] `SearchMessages` — results with ±3 message context, grouped by session
- [ ] `GetMe` tool — returns own name, id, username
- [ ] `GetUserInfo` tool — returns profile + common chats list
- [ ] Remove `GetDialog` tool (superseded by ListDialogs)
- [ ] Remove `GetMessage` tool (message_id not exposed in new format)

### Out of Scope

- Sending/editing/deleting messages — read-only access by design
- Media download/streaming — format describes media, doesn't fetch it
- Real-time notifications / webhooks — polling model only
- Native HTTP/SSE transport (mcp-proxy covers this; native is future work)
- Multi-account support — single session per deployment

## Context

Brownfield project. Existing codebase uses `@singledispatch` pattern for tool routing,
Pydantic models for tool arguments and schema generation. Tools layer in `src/mcp_telegram/tools.py`,
server layer in `src/mcp_telegram/server.py`.

Name resolution requires two new dependencies: `rapidfuzz` (fuzzy matching) and `transliterate`
(Cyrillic ↔ Latin). Algorithm: normalize → WRatio score → threshold gates (≥90 auto-resolve,
60–89 return ambiguity list, <60 not found). Resolver output is prepended to tool output as
`[резолв: "query" → Name, id:N]` so LLM sees what was matched.

Message format is chat-log style: `HH:mm FirstName: text  [reactions]`. Date header on day
change, session break line for gaps >60 min, reply annotation when present. No numeric
message_id in output — cursor pagination uses opaque base64 tokens.

## Constraints

- **Tech stack**: Python 3.11+, Telethon, MCP SDK, Pydantic v2 — no new heavy deps
- **Fuzzy matching**: rapidfuzz + transliterate only (no ML, deterministic)
- **Backwards compat**: stdio transport must stay working; Docker entrypoint unchanged
- **Privacy**: No real user IDs, names, or usernames in planning docs or code comments

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Names accepted everywhere (str \| int) | Eliminates mandatory cold-start ListDialogs call | — Pending |
| WRatio scorer for fuzzy match | Handles word order, case, partial matches, Cyrillic | — Pending |
| Cursor pagination (opaque tokens) | message_id not exposed → can't use raw IDs for paging | — Pending |
| Remove GetDialog + GetMessage | These tools require IDs the LLM can't get in new format | — Pending |
| mcp-proxy stays for HTTP | Native HTTP/SSE deferred — proxy works, not worth disruption now | — Pending |

---
*Last updated: 2026-03-11 after initialization*
