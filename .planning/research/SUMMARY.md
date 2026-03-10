# Project Research Summary

**Project:** mcp-telegram — LLM-facing Telegram read interface
**Domain:** MCP server refactoring (Telethon + Python, read-only, stdio transport)
**Researched:** 2026-03-11
**Confidence:** HIGH

## Executive Summary

This project is a focused refactoring of an existing read-only Telegram MCP server. The server currently works but exposes a machine-oriented interface: raw numeric IDs in tool arguments, bare `[id=N] text` message output, and ID-based pagination that leaks internal Telegram message IDs to the LLM. The research consensus is clear — the dominant friction for LLM consumers is being forced to work with numeric identifiers, and every major design decision in this milestone flows from eliminating that friction. The recommended approach is to add three thin support modules (resolver, formatter, pagination) that transform the interface without touching the transport or protocol layers.

The stack is almost entirely already in place. Two new dependencies are needed: `rapidfuzz` for fuzzy name matching (MIT, 10-100x faster than alternatives) and `transliterate` for Cyrillic/Latin normalization (handles the very common pattern of Russian names typed in Latin). The existing `mcp.server.Server` + `stdio` transport stays unchanged; adding HTTP/SSE transport is explicitly out of scope. The architecture is well-defined: three new pure-Python modules, a thin tool runner layer, and no changes to server.py or telegram.py.

The primary risks are operational rather than architectural. Stale Telegram entity caches can cause silent wrong-resolution after username changes; the fix is a 5-minute TTL in-process cache sourced from `iter_dialogs()`. `FloodWaitError` during dialog iteration can stall tool calls for accounts with 500+ dialogs; the same cache prevents this. The `MessageService` type (service messages like "user joined") crashes text-oriented formatters that lack type guards — this is a consistent source of subtle bugs that must be addressed in the formatter before any other formatting work. All eight identified critical pitfalls have clear, low-cost prevention strategies.

## Key Findings

### Recommended Stack

The existing stack needs only two additions. All other dependencies (Telethon 1.42.0, MCP SDK 1.26.0, Pydantic v2, pydantic-settings, Python 3.11+) are already in pyproject.toml and working. The `base64` and `json` stdlib modules handle cursor token encoding with no new dependencies. The stdio transport is preserved as-is; the `StreamableHTTPSessionManager` approach for native HTTP/SSE is documented but explicitly deferred — `mcp-proxy` handles external HTTP/SSE at the infrastructure level.

**Core technologies:**
- `rapidfuzz >= 3.0.0` — fuzzy name matching (WRatio scorer) — MIT license, C extension, 10-100x faster than fuzzywuzzy/thefuzz; use `from rapidfuzz import fuzz, process, utils`
- `transliterate >= 1.8.1` — Cyrillic/Latin bidirectional — always pass `language_code='ru'` explicitly; always transliterate Cyrillic candidate names to Latin (not the query to Cyrillic)
- `Telethon 1.42.0` — `iter_dialogs()` provides `dialog.date` (last message timestamp) and entity type info without extra API calls; `GetFullUserRequest` and `GetCommonChatsRequest` are available for GetUserInfo
- `base64.urlsafe_b64encode` + `json` (stdlib) — opaque cursor tokens; `urlsafe` variant required to avoid JSON escaping issues with `+` and `/`

**Version constraints:**
- rapidfuzz 3.x changed scorer import paths from v2.x; use top-level `from rapidfuzz import fuzz, process, utils`
- `mcp` 1.26.0 low-level Server API is unchanged since 1.1.0; FastMCP migration is not worthwhile for this milestone

### Expected Features

The full feature list is documented in FEATURES.md. The dependency structure is critical: name resolution is required by three tools before those tools can be updated; cursor pagination conflicts with the existing `before_id: int` approach and the migration is a breaking change that must be done in one step; the human-readable message format enables all other formatting features (session breaks, replies, media placeholders, reactions).

**Must have (table stakes — current milestone):**
- Name-based dialog resolution (str | int accepted everywhere) — eliminates mandatory ListDialogs cold-start
- Human-readable message format (`HH:mm FirstName: text [reactions]`) — current `[id=N] text` is machine output
- Cursor pagination (opaque base64 tokens, replaces `before_id: int`) — broken once message_ids are hidden
- Media type placeholders (`[photo]`, `[voice note]`, etc.) — silence causes LLM hallucinations
- Reply annotation (`[reply to: FirstName: "..."]`) — group chat threads are incomprehensible without it
- Session break lines (60-min gap marker) — 50 messages reads as one undifferentiated wall
- Resolver transparency prefix (`[resolve: "query" → Name, id:N]`) — auditability, low effort
- `GetMe` tool — required to interpret "my messages" correctly
- `ListDialogs` type + `last_message_at` fields — distinguish DM/group/channel/supergroup
- Name-based sender resolution for ListMessages sender filter
- Remove `GetDialog` and `GetMessage` (ID-based, superseded)

**Should have (competitive differentiators — v1.x after validation):**
- `GetUserInfo` with common chats — profile + shared groups
- `SearchMessages` with ±3 context — search hits without context are not useful
- Reaction representation inline in message format

**Defer (v2+):**
- Forward annotation (`↩ fwd from Name`)
- Forum/thread support (requires Telegram Forum API, distinct architecture)
- Media download as opt-in tool
- Real-time/push/webhook support (incompatible with stateless stdio model)
- Multi-account support (deploy multiple containers instead)

**Anti-features to explicitly avoid:**
- Exposing raw `message_id` in output — LLMs will pass it back as literals, bypassing opaque cursor design
- Send/edit/delete tools — read-only scope is a deliberate safety property
- Client-side fuzzy search on message content — use Telegram's server-side full-text search instead

### Architecture Approach

The refactoring adds three new modules to the existing flat package structure. `server.py`, `telegram.py`, and the existing MCP protocol wiring are untouched. The `tools.py` singledispatch pattern auto-discovers new `ToolArgs` subclasses via reflection — no registration step in `server.py` is needed for new tools. New tools (`GetMe`, `GetUserInfo`) fit the existing pattern without modification. Dependency direction is strictly one-way: `tools.py` imports from support modules; support modules never import from `tools.py`.

**Major components:**
1. `resolver.py` (NEW) — name-to-entity-id resolution; fuzzy match via WRatio + transliteration; 5-min TTL in-process dialog cache keyed on `id(client)`; returns `ResolveResult` with annotation string
2. `formatter.py` (NEW) — pure functions, no state; `format_message(msg, prev_msg)` returns list of lines; handles day headers, session breaks, media placeholders, reply annotation, reactions
3. `pagination.py` (NEW) — pure functions; `encode_cursor(dialog_id, message_id)` / `decode_cursor(token, expected_dialog_id)`; embeds dialog guard to prevent cross-dialog cursor misuse
4. `tools.py` (evolves) — thin runners that orchestrate the three modules; no business logic inline; single `TextContent` response per tool with annotation prepended
5. `server.py` / `telegram.py` — no changes

**Build order:** `pagination.py` first (no Telethon dependency, unit-testable immediately) → `formatter.py` (depends on Telethon message types, mock-testable) → `resolver.py` (async I/O, needs integration test) → `tools.py` updates → validation of MCP tool list

### Critical Pitfalls

1. **Stale entity cache after username changes** — After fuzzy match, wrap `client.get_input_entity()` in try/except; on `ValueError`, evict the entry and report "not found" rather than crashing with an opaque error on the subsequent API call
2. **`MessageService` objects crash text formatters** — Gate all attribute access on `isinstance(message, types.Message)`; render service messages as a minimal system line rather than skipping them silently (skipping breaks session-break gap calculation)
3. **`FloodWaitError` during `iter_dialogs()`** — The dialog cache is the fix, not an optimization; it must be designed before the resolver is built; set `flood_sleep_threshold=30` and return a user-friendly error for waits beyond that threshold
4. **Transliteration direction is inverted by instinct** — Always transliterate Cyrillic candidate names to Latin for comparison; never transliterate the query; wrong direction doubles error rate and is hard to debug
5. **`max_id` vs `offset_id` semantics** — Use `max_id` exclusively for cursor pagination (`id < max_id`, newest-first); `offset_id` is for skip-based pagination and behaves differently; mixing them causes off-by-one errors at page boundaries
6. **Removed tools cause unhandled `NotImplementedError`** — MCP clients cache tool lists at session start; keep `GetDialog` and `GetMessage` registered as stubs returning a helpful error text during the transition window

## Implications for Roadmap

Research strongly supports a four-phase structure aligned with the dependency graph. Each phase builds on a stable foundation from the previous one, and all critical pitfalls are addressed at the phase where their prevention cost is lowest.

### Phase 1: Support Modules (Foundation)
**Rationale:** Three new modules have no inter-dependencies and can be built and tested in isolation before any tool is touched. Building them first means every tool update in later phases has a complete, tested foundation to call into. Prevents the pattern where resolver/formatter logic gets inlined into tool runners and becomes untestable.
**Delivers:** `pagination.py`, `formatter.py`, `resolver.py` — all with unit tests
**Addresses:** All P1 features that require these modules (resolver transparency, cursor pagination, human-readable format, session breaks, media placeholders, reply annotation)
**Avoids:** Anti-Pattern 2 (per-call dialog fetching), Pitfall 3 (FloodWaitError — cache designed here), Pitfall 7 (transliteration direction — normalization strategy fixed here)

### Phase 2: Tool Updates — Existing Tools
**Rationale:** With support modules in place, update existing tools to use them. This is where `before_id` pagination is retired (one breaking change, done completely). Removing `GetDialog` and `GetMessage` stubs happens here too — they must stay registered as error-returning stubs, not fully removed.
**Delivers:** Updated `ListDialogs` (type + last_message_at), updated `ListMessages` (name resolution, cursor pagination, sender filter, formatted output), updated `SearchMessages` (name resolution, formatted output)
**Uses:** `resolver.py`, `formatter.py`, `pagination.py` from Phase 1; `rapidfuzz` WRatio thresholds (90/60)
**Avoids:** Pitfall 2 (max_id vs offset_id — use max_id exclusively), Pitfall 8 (removed tool stubs), Pitfall 1 (stale entity cache — resolver wraps get_input_entity)

### Phase 3: New Tools
**Rationale:** `GetMe` and `GetUserInfo` are new additions that fit the existing singledispatch pattern without touching existing tools. Building them after existing tools are updated means the resolver is already battle-tested.
**Delivers:** `GetMe` tool (own account info), `GetUserInfo` with common chats (profile + shared groups)
**Implements:** `GetFullUserRequest`, `GetCommonChatsRequest` Telethon API calls
**Avoids:** Pitfall 1 (stale entity cache — resolver already handles this)

### Phase 4: Polish and Validation
**Rationale:** Reaction representation, SearchMessages context, and the "looks done but isn't" checklist items are deferred here. This phase is for quality validation against the pitfall checklist — testing service messages, custom emoji reactions, page boundary duplicates, flood handling on large accounts.
**Delivers:** Reaction representation inline, SearchMessages ±3 context, verification against full pitfall checklist
**Addresses:** P2 features (reaction representation, search context)
**Avoids:** Pitfall 4 (None reactions + custom emoji), Pitfall 3 (MessageService type guard verified)

### Phase Ordering Rationale

- Phases 1-2 are strictly ordered by the dependency graph: support modules before tool updates
- Phase 3 (new tools) could technically be done before Phase 2, but updating existing tools first validates the resolver and formatter under real conditions before adding new entry points
- Phase 4 items are explicitly deferred because they validate behavior rather than establish it — running the checklist against a partially implemented system wastes effort
- The `before_id` → cursor migration is a breaking change that must happen entirely within Phase 2; splitting it across phases would leave the tool in an inconsistent state

### Research Flags

Phases with well-documented patterns (standard implementation, skip additional research):
- **Phase 1 (Support Modules):** All APIs are verified against official docs with exact signatures in STACK.md. WRatio/process.extractOne signatures, transliterate direction, cursor encoding — no unknowns.
- **Phase 3 (New Tools):** `GetFullUserRequest` and `GetCommonChatsRequest` are fully documented in STACK.md including return types and failure modes.

Phases that may benefit from deeper research during planning:
- **Phase 2 (Tool Updates):** The `str | int` union type in Pydantic v2 schema generation for MCP — verify the generated JSON schema is correctly interpreted by MCP clients before committing to the field type. Low risk but worth a quick validation.
- **Phase 4 (Validation):** Custom emoji reaction handling (`ReactionCustomEmoji` type) has limited documentation; the `getattr(r.reaction, 'emoticon', '?')` fallback is the safe approach but behavior in edge cases should be tested against a real account with custom emoji reactions.

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All API signatures verified against official docs and PyPI; existing codebase read directly |
| Features | HIGH | Core design decisions validated in PROJECT.md; competitor analysis MEDIUM confidence but doesn't affect MVP decisions |
| Architecture | HIGH | Based on direct codebase analysis; no speculation required; existing singledispatch pattern is well-understood |
| Pitfalls | HIGH | Most pitfalls verified against Telethon GitHub Issues and Telethon source; MCP tool caching behavior verified against MCP GitHub Issues |

**Overall confidence:** HIGH

### Gaps to Address

- **Pydantic v2 `str | int` MCP schema generation:** Research documents that it emits `anyOf: [string, integer]` but doesn't confirm all MCP client implementations handle this union correctly. Validate with a quick test before Phase 2 tool signature changes land.
- **`transliterate` library with Ukrainian/Belarusian names:** PITFALLS.md flags `LanguagePackNotFound` risk for non-Russian Cyrillic. Test coverage should include Ukrainian names before the resolver is considered complete.
- **Account size limits for dialog cache:** Research assumes 5-min TTL is sufficient. For accounts with 1000+ dialogs, the initial `iter_dialogs()` call may itself trigger FloodWait. Consider adding a configurable `DIALOG_CACHE_TTL` env var rather than hardcoding 300s.

## Sources

### Primary (HIGH confidence)
- `https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html` — WRatio signature, score_cutoff, processor
- `https://rapidfuzz.github.io/RapidFuzz/Usage/process.html` — extractOne/extract signatures (v3.14.3)
- `https://docs.telethon.dev/en/stable/modules/client.html` — get_me, get_entity, iter_dialogs, iter_messages, get_participants
- `https://tl.telethon.dev/methods/users/get_full_user.html` — GetFullUserRequest return type
- `https://tl.telethon.dev/constructors/user_full.html` — UserFull.about, UserFull.common_chats_count
- `https://tl.telethon.dev/methods/messages/get_common_chats.html` — GetCommonChatsRequest parameters
- `https://transliterate.readthedocs.io/en/1.8.1/` — translit() signature, reversed parameter
- `https://pypi.org/project/mcp/` — version 1.26.0, transport options
- Existing codebase: `server.py`, `tools.py`, `telegram.py` — direct read, ground truth
- `.planning/PROJECT.md` — requirements, algorithm specifications, format decisions

### Secondary (MEDIUM confidence)
- `chigwell/telegram-mcp` (GitHub) — competitor feature analysis
- `IQAIcom/mcp-telegram` (GitHub) — competitor feature analysis
- MCP Tools writing guide (modelcontextprotocol.info) — name resolution and pagination best practices
- `core.telegram.org/api/reactions` — reaction type structure
- MCP GitHub Issues #17975, VS Code #256421 — tool caching behavior

### Tertiary (HIGH confidence from issue tracker)
- Telethon GitHub Issues #4540, #3183, #4084, #335 — entity resolution failure modes and fwd_from behavior
- `https://tl.telethon.dev/constructors/message_reactions.html` — MessageReactions structure

---
*Research completed: 2026-03-11*
*Ready for roadmap: yes*
