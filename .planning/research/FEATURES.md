# Feature Research

**Domain:** LLM-facing Telegram read interface (MCP server)
**Researched:** 2026-03-11
**Confidence:** HIGH for design decisions already validated in PROJECT.md; MEDIUM for competitor feature analysis

## Feature Landscape

### Table Stakes (Users Expect These)

Features the LLM consumer assumes work correctly. Missing any of these makes the tool feel broken or requires an awkward workaround on every call.

| Feature | Why Expected | Complexity | Notes |
|---------|--------------|------------|-------|
| Name-based dialog resolution | LLMs work with names, not IDs. Requiring `dialog_id: int` forces a mandatory `ListDialogs` cold-start before every task — this is the core friction the tool exists to remove | MEDIUM | WRatio scorer (rapidfuzz) + transliterate. Threshold gates: ≥90 auto-resolve, 60–89 return ambiguity list, <60 not found. Resolution result prepended to output as `[resolve: "query" → Name, id:N]` |
| Name-based sender/contact resolution | Same problem as dialog resolution but for the `sender` filter on `ListMessages` and `GetUserInfo` | MEDIUM | Same algorithm, same dependencies — reuse resolver |
| Human-readable message output | Current output is `[id=N] raw text` — no sender name, no timestamp, no context. LLMs lose conversation structure immediately | MEDIUM | Chat-log style: `HH:mm FirstName: text  [reactions]`. Date header on day change. No raw message_id in output |
| Cursor-based pagination | `before_id: int` pagination is broken in the new format — message_ids are not exposed. Must be replaceable | LOW | Opaque base64 cursor tokens. LLM passes `cursor` back from previous page response. Internally encodes message_id |
| Session/conversation grouping | LLM reading 50 messages sees one flat list — can't tell where one conversation topic ends and another starts | LOW | Session break line when gap > 60 min between messages |
| Reply annotation in output | Replies are common in Telegram group chats; without context the reply target is invisible to the LLM | LOW | Append `[reply to: FirstName: "first 40 chars"]` inline |
| Media type placeholders | Messages with photos/videos/stickers silently disappear in text-only output — the LLM has no idea a media message was sent | LOW | `[photo]`, `[video]`, `[voice note]`, `[sticker: emoji]`, `[document: filename.pdf]` inline in message text |
| `GetMe` tool | LLM needs to know whose account this is to reason about "my messages" vs. others | LOW | Returns own name, id, username |
| `ListDialogs` with type and timestamp | Knowing only the name is not enough — LLM needs to distinguish DM vs group vs channel and see recency | LOW | Add `type` field (private/group/channel/supergroup) and `last_message_at` ISO timestamp |
| `ListMessages` unread filter | Primary use case: "what did I miss?" — must work without knowing how many unread messages there are | LOW | Already exists; confirm it works correctly with new resolver and pagination |
| `SearchMessages` with context | Search hit alone is useless — the LLM needs surrounding messages to understand what the discussion was about | MEDIUM | Return ±3 message context per match, hits grouped by conversation session |

### Differentiators (Competitive Advantage)

Features that make this tool meaningfully better than the raw-ID approach or than write-capable competitors.

| Feature | Value Proposition | Complexity | Notes |
|---------|-------------------|------------|-------|
| Resolver transparency (`[resolve: ...]` prefix) | LLM sees exactly what was matched — prevents silent wrong-contact mistakes, gives user auditability. No other Telegram MCP tool does this | LOW | Prepend to every tool output where resolution occurred. Format: `[resolve: "query" → Display Name, id:N]` |
| Ambiguity list on low-confidence match | Instead of silently picking the wrong contact or failing with an error, return top candidates — LLM can ask the user to disambiguate | LOW | Score 60–89 → return list of up to 5 candidates with scores. Score <60 → "not found" |
| Transliteration-aware matching | Russian names typed in Latin and vice versa is extremely common. No competitor handles this | LOW | `transliterate` library. Normalize both query and candidate before scoring |
| `GetUserInfo` with common chats | Gives context about who someone is relative to the account owner — not available in read-only competitors | MEDIUM | Returns profile fields + list of shared group/channel names |
| Read-only by design | Eliminates the entire class of "LLM accidentally sent a message" accidents. Simpler threat model, easier to audit | NONE | Already a constraint. Reinforce in tool descriptions so LLM doesn't try to use it for writes |
| Strict privacy in output | message_ids, internal user IDs are not surfaced in output — reduces risk of LLM leaking or misusing identifiers | LOW | Only names, timestamps, and opaque cursor tokens visible in output |

### Anti-Features (Commonly Requested, Often Problematic)

| Feature | Why Requested | Why Problematic | Alternative |
|---------|---------------|-----------------|-------------|
| Exposing raw message_id in output | "Useful for referencing specific messages" | Once LLMs see numeric IDs they start passing them back as arguments. This breaks when message_ids change across MTProto sessions, and it forces us to maintain ID-based tools we want to remove | Opaque cursor tokens for pagination; resolver output for identification |
| Send/edit/delete message tools | "More powerful — LLM can act, not just read" | Write access turns a read-only observer into an agent that can cause real harm (wrong recipient, wrong content, irrecoverable deletes). Dramatically expands attack surface of the session token | Deliberate read-only scope. If write tools are ever added, they belong in a separate tool set with explicit confirmation flow |
| Real-time / push / webhook support | "Get notified when new messages arrive" | MTProto polling model is incompatible with the stateless stdio MCP execution model. SSE/webhook would require a persistent background process managing state — entirely different architecture | Polling via `ListMessages(unread=True)` is the correct model for LLM-initiated workflows |
| Media download / file streaming | "LLM should be able to see the photos" | Binary content over MCP text channels is either base64-bloat (token waste) or requires out-of-band storage (new infrastructure). Most LLM tasks don't require pixel-level image data | Descriptive media placeholders in text output. File download as a future, opt-in, separate tool with explicit size limits |
| Multi-account support | "Different contexts need different accounts" | Single-session architecture is simple and matches the deployment model (one Docker container = one account). Multi-account requires session multiplexing, credential management, and a selector argument on every call | Deploy multiple instances if needed — each container is a separate account |
| Full history pagination with offset | "Page 3 of results" semantics with numeric offsets | Offset pagination breaks when the underlying data changes (new messages arrive). Also requires exposing message_ids or timestamps as offset anchors, which leaks internal IDs | Cursor-based pagination with opaque tokens — stable across data changes, no ID exposure |
| Fuzzy search on message content | "Find messages about X even if not exact" | Telegram's MTProto API does full-text search server-side. Client-side fuzzy match on message content requires loading potentially thousands of messages into memory | Use `SearchMessages` with a good query — Telegram's own search is better than client-side fuzzy |

## Feature Dependencies

```
[Name Resolution]
    └──required by──> [ListMessages with sender filter]
    └──required by──> [GetUserInfo]
    └──required by──> [SearchMessages scoped to dialog]

[Cursor Pagination]
    └──requires──> [message_id hidden from output]
                       └──conflicts with──> [before_id: int pagination]

[Human-readable message format]
    └──requires──> [Name Resolution] (for sender display names)
    └──enables──> [Session grouping]
    └──enables──> [Reply annotation]
    └──enables──> [Media placeholders]
    └──enables──> [Reaction representation]

[GetMe]
    └──enhances──> [Human-readable format] (own name displayed vs. others)
```

### Dependency Notes

- **Name Resolution required by message format:** Sender display names require resolving peer IDs to display names at format time — the resolver must work on both input (tool args) and output (message rendering).
- **Cursor pagination conflicts with before_id:** The two pagination schemes are mutually exclusive. `before_id: int` requires message_id in output; cursor tokens require it hidden. Migration is a breaking change — done once.
- **Human-readable format enables everything else:** Session grouping, reply annotation, media placeholders, and reaction representation are all formatting concerns that layer on top of the base chat-log format. They share the same rendering pass.

## MVP Definition

### Launch With (v1 — the current milestone)

Minimum to make the tool genuinely usable for "read my Telegram" tasks without ID boilerplate.

- [ ] Name-based dialog resolution (str | int accepted everywhere) — without this, every session starts with a mandatory ListDialogs call
- [ ] Name-based sender resolution — same dependency for ListMessages sender filter
- [ ] Human-readable message format (HH:mm Name: text) — current `[id=N] text` is machine output, not LLM-consumable
- [ ] Cursor pagination (opaque tokens, replaces before_id) — pagination is broken once message_ids are hidden
- [ ] Media type placeholders — silent disappearance of media messages causes hallucinations ("there was no photo")
- [ ] Reply annotation — without it, group chat threads are incomprehensible
- [ ] Session break lines — without them, 50 messages looks like one undifferentiated wall
- [ ] Resolver transparency prefix — required for auditability; low effort, high value
- [ ] `GetMe` tool — needed to interpret "my messages" correctly
- [ ] `ListDialogs` with type and last_message_at — needed to filter the right conversation type
- [ ] Remove `GetDialog` and `GetMessage` (ID-based, superseded)

### Add After Validation (v1.x)

- [ ] `GetUserInfo` with common chats — useful but not blocking the core use case
- [ ] `SearchMessages` with ±3 context — current search returns isolated hits; context is a quality-of-life improvement
- [ ] Reaction representation in message format — emoji reactions are common in active chats; the format should show them

### Future Consideration (v2+)

- [ ] Forward annotation (`[forwarded from: Name]`) — adds completeness but rarely affects task outcome
- [ ] Scheduled / pinned message queries — niche use cases
- [ ] Message thread / topic support (forum supergroups) — requires Telegram Forum API, distinct architecture
- [ ] Media download as separate opt-in tool — only if a concrete use case justifies the infrastructure cost

## Feature Prioritization Matrix

| Feature | User Value | Implementation Cost | Priority |
|---------|------------|---------------------|----------|
| Name-based dialog resolution | HIGH | MEDIUM | P1 |
| Human-readable message format | HIGH | MEDIUM | P1 |
| Cursor pagination | HIGH | LOW | P1 |
| Media placeholders | HIGH | LOW | P1 |
| Reply annotation | MEDIUM | LOW | P1 |
| Session break lines | MEDIUM | LOW | P1 |
| Resolver transparency prefix | HIGH | LOW | P1 |
| `GetMe` tool | MEDIUM | LOW | P1 |
| `ListDialogs` type + timestamp | MEDIUM | LOW | P1 |
| Name-based sender resolution | MEDIUM | LOW | P1 |
| Reaction representation | MEDIUM | LOW | P2 |
| `GetUserInfo` with common chats | MEDIUM | MEDIUM | P2 |
| `SearchMessages` ±3 context | HIGH | MEDIUM | P2 |
| Forward annotation | LOW | LOW | P3 |
| Forum/thread support | LOW | HIGH | P3 |
| Media download tool | LOW | HIGH | P3 |

**Priority key:**
- P1: Must have for launch (current milestone)
- P2: Should have, add when core is stable
- P3: Nice to have, future consideration

## Competitor Feature Analysis

| Feature | chigwell/telegram-mcp (80+ tools) | IQAIcom/mcp-telegram | sparfenyuk/mcp-telegram (this project, current) | Our target approach |
|---------|-----------------------------------|----------------------|-------------------------------------------------|---------------------|
| Name resolution | None — all tools require numeric IDs | None | None | Fuzzy + transliteration, explicit resolver output |
| Message format | Raw Telethon object fields | Not documented | `[id=N] text` | Chat-log: `HH:mm Name: text [reactions]` |
| Pagination | Offset (limit + offset args) | Not applicable (send-only) | `before_id: int` (exposes message_id) | Opaque cursor tokens |
| Reactions | `get_message_reactions()` separate tool | Not documented | Not implemented | Inline in message format: `[👍3 ❤️1]` |
| Media | `download_media()`, `send_file()` | send-only | Not represented | Descriptive placeholders inline |
| Scope | Read + write (80+ tools) | Send-only (5 tools) | Read-only (6 tools) | Read-only — deliberate |
| Reply context | Not in message format | N/A | Not implemented | `[reply to: Name: "..."]` inline |

## Sources

- Current codebase: `/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py` — ground truth for existing behavior
- PROJECT.md: validated requirements and key decisions — HIGH confidence
- chigwell/telegram-mcp (github.com/chigwell/telegram-mcp) — most feature-complete competitor, write-capable — MEDIUM confidence
- IQAIcom/mcp-telegram (github.com/IQAIcom/mcp-telegram) — send-only, 5 tools — MEDIUM confidence
- MCP Tools writing guide (modelcontextprotocol.info) — name resolution and pagination best practices — HIGH confidence
- Telegram MTProto reactions API (core.telegram.org/api/reactions) — reaction type structure — HIGH confidence
- Telegram Desktop chat export format — media type taxonomy, reaction field presence — MEDIUM confidence
- WebSearch: MCP pagination opaque cursor patterns — MEDIUM confidence (multiple sources agree)

---
*Feature research for: LLM-facing Telegram read interface (MCP server)*
*Researched: 2026-03-11*
