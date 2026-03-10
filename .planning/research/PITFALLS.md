# Pitfalls Research

**Domain:** Telethon-based MCP server — fuzzy name resolution, message formatting, cursor pagination
**Researched:** 2026-03-11
**Confidence:** HIGH (most pitfalls verified against Telethon source, GitHub issues, and MCP SDK behavior)

---

## Critical Pitfalls

### Pitfall 1: Stale Entity Cache Breaks Name Resolution After Username Changes

**What goes wrong:**
Telethon caches all encountered entities in the `.session` SQLite file. When a user changes their username or display name, the cached entry is stale. The fuzzy resolver matches against cached dialog names — but using that stale peer to call any API method raises `ValueError: Could not find input entity` because the `access_hash` no longer matches.

**Why it happens:**
The resolver fetches dialogs once (or re-uses a warm cache from a previous session) to build the candidate list. It never re-validates that a matched entity is still reachable. The session file is persistent across restarts, so a peer cached months ago may be completely invalid.

**How to avoid:**
- Always source the candidate list from a fresh `iter_dialogs()` call, not from the session entity store alone.
- After fuzzy matching, attempt to obtain a valid `InputPeer` via `client.get_input_entity()`. If that raises `ValueError`, evict the entry and report "not found" rather than crashing.
- For the resolver, explicitly document that entity resolution requires a live connection and will re-fetch on failure.

**Warning signs:**
- `ValueError: Could not find input entity` at the point of using a resolved ID, not at resolution time.
- Resolution succeeds and returns a name, but the subsequent `iter_messages` call fails.
- Happens to contacts who recently changed usernames or privacy settings.

**Phase to address:** Name resolution phase — resolver must wrap `get_input_entity()` in a try/except and fall back gracefully.

---

### Pitfall 2: `min_id`/`max_id` vs `offset_id` — Different Exclusion Semantics

**What goes wrong:**
The current code uses `max_id=args.before_id` for pagination. The Telethon `max_id` filter excludes messages with ID **greater than or equal** to `max_id` — meaning the boundary message itself is excluded. This is correct for "page after message N" semantics. However, `offset_id` has different behavior: it fetches messages **older than** `offset_id` (exclusive), and it interacts with `add_offset` to implement skip-based pagination. Mixing them produces subtle off-by-one errors or missing messages at page boundaries.

The new opaque cursor design encodes a message ID inside a base64 token. If the decode produces the ID of the **last** message on the previous page, and the code passes that as `offset_id` instead of `max_id`, the cursor boundary shifts by one, silently losing messages or duplicating the boundary message on consecutive pages.

**Why it happens:**
The Telegram MTProto API documentation does not clearly state the inclusive/exclusive semantics for each parameter. Developers guess, and the guess is often wrong. `iter_messages` with `reverse=False` (newest-first) and `max_id=N` returns messages with `id < N`, which is what "before" means — but `offset_id=N` with `reverse=False` returns messages older than N starting from the next batch, which can differ when `add_offset` is non-zero.

**How to avoid:**
- Use `max_id` exclusively for "before cursor" semantics. It means "give me messages with id strictly less than this value."
- Do **not** use `offset_id` for cursor pagination. It is designed for skip-based pagination (with `add_offset`) and behaves differently.
- Document this decision in code comments with an explicit test case: if cursor encodes message ID 500, the next page must not include message 500.
- Write a unit test or integration smoke test that verifies no duplication and no gap at page boundaries.

**Warning signs:**
- Duplicate message at the seam between two pages.
- Off-by-one: the "oldest" message of page N is the "newest" of page N+1.
- Inconsistent page sizes despite a constant `limit` setting.

**Phase to address:** Cursor pagination implementation — define `max_id` usage in a single internal helper, never let callers choose between `max_id` and `offset_id`.

---

### Pitfall 3: `MessageService` Objects Crash Text-Oriented Formatting Code

**What goes wrong:**
`iter_messages()` returns both `Message` and `MessageService` objects. `MessageService` has no `.text` attribute (it has `.action` instead). Code that accesses `.text`, `.reactions`, `.reply_to`, or `.fwd_from` without a type check will raise `AttributeError` on service messages (user joined, call started, pinned message, etc.).

The current code already guards with `isinstance(message, custom.Message) and message.text`, which is correct — but the new formatter that adds reactions, replies, and sender attribution must preserve that guard at every access point, not just the top-level branch.

**Why it happens:**
Telegram injects service messages into every dialog timeline. Developers who test against personal chats rarely see them. They appear in groups and channels frequently (join/leave events, pinned messages, calls).

**How to avoid:**
- Gate **all** message formatting behind `isinstance(message, types.Message)` (the concrete TL type), not `custom.Message` if reactions and TL-level fields are needed.
- Alternatively, use `hasattr(message, 'reactions')` guards around every optional field access.
- Render service messages as a distinct, minimal format (e.g., `--- [System: user joined] ---`) rather than skipping silently — skipping them causes gaps in timestamp continuity that break session-break detection.

**Warning signs:**
- `AttributeError: 'MessageService' object has no attribute 'reactions'`
- Session-break lines appear in wrong places because service messages are skipped, disrupting the 60-min gap calculation.
- Formatted output omits expected events (pin notifications, etc.) with no explanation.

**Phase to address:** Message formatting phase — formatter must have an explicit `MessageService` branch.

---

### Pitfall 4: `Message.reactions` Is `None` When No Reactions Exist — And the Structure Is Nested

**What goes wrong:**
`Message.reactions` returns `None` when a message has no reactions. When reactions exist, it returns a `MessageReactions` object whose `.results` is a list of `ReactionCount` objects. Each `ReactionCount` has a `.reaction` field (a `ReactionEmoji` or `ReactionCustomEmoji`) and a `.count`. Accessing `.reaction.emoticon` without checking the type raises `AttributeError` for custom emoji reactions.

Additionally, `MessageReactions` may not be returned by older API calls. In channels with restricted reaction access, the field is present but `.results` is an empty list, not `None`.

**Why it happens:**
Reactions were added to Telegram relatively recently and the type hierarchy has several layers. Developers assume that if `message.reactions` is not `None`, then `message.reactions.results[0].reaction.emoticon` is safe — it is not when custom emoji are involved.

**How to avoid:**
- Check `message.reactions is not None` before any access.
- Iterate over `.results` with a guard: `getattr(r.reaction, 'emoticon', '?')` to safely handle `ReactionCustomEmoji`.
- Represent reactions as `[emoji×count, ...]` string, e.g., `[👍×3, ❤️×1]`, using the safe accessor.

**Warning signs:**
- `AttributeError: 'ReactionCustomEmoji' object has no attribute 'emoticon'` in groups that use custom sticker packs for reactions.
- `NoneType` error on `message.reactions.results` in chats where reactions are disabled.

**Phase to address:** Message formatting phase — reactions formatter must be isolated as a pure function with explicit None/empty guards.

---

### Pitfall 5: `fwd_from` Sender Name May Be Unavailable or Anonymized

**What goes wrong:**
`message.fwd_from` returns a `MessageFwdHeader`. For channel forwards, `.from_id` is a `PeerChannel` but the channel name is not included — you must call `get_entity()` to resolve it, which is an additional API round-trip (with flood risk). For users who have privacy settings restricting forward attribution, `fwd_from.from_id` is `None` and `fwd_from.from_name` contains a plain string (the display name the user chose to show). Accessing `.from_id.user_id` when `from_id` is a `PeerChannel` crashes.

**Why it happens:**
`from_id` is a polymorphic union type (`PeerUser | PeerChannel | PeerChat | None`). Code that assumes it is always a user ID or always `None` breaks on channel forwards.

**How to avoid:**
- Do not resolve `fwd_from` sender names at formatting time — the API call introduces latency and flood risk.
- Use `fwd_from.from_name` when available (user-chosen privacy name), fall back to `fwd_from.channel_post` for channel forwards, and format as `↩ fwd from [channel post]` or `↩ fwd from [Name]`.
- Never call `get_entity()` inside the message formatter loop.

**Warning signs:**
- Formatting becomes slow in channels with many forwarded messages (network calls inside formatter).
- `AttributeError` on `.user_id` when `from_id` is a `PeerChannel`.
- `FloodWaitError` triggered by resolving forward origins at scale.

**Phase to address:** Message formatting phase — fwd_from must be rendered from inline data only, no entity resolution.

---

### Pitfall 6: `FloodWaitError` During Dialog Iteration Blocks the Entire Tool Call

**What goes wrong:**
`iter_dialogs()` with hundreds of dialogs triggers Telegram's rate limiter. Telethon auto-sleeps for `FloodWaitError` up to `flood_sleep_threshold` seconds (default: 60). An LLM tool call waiting 60 seconds will time out at the MCP transport layer or appear hung to the client. Beyond the threshold, the error is re-raised, crashing the tool with an unhandled exception.

**Why it happens:**
Telegram limits `GetDialogs` requests. Each page is 100 dialogs; with 500+ dialogs, multiple requests fire in quick succession. The fuzzy resolver must iterate all dialogs to build the candidate list, making this unavoidable on large accounts.

**How to avoid:**
- Cache the dialog list in-process (Python dict keyed by session ID) with a TTL of 5–10 minutes. The first `ListDialogs` or any name-resolution call populates it; subsequent calls within the TTL use the cache.
- Set `flood_sleep_threshold` to a value that fits within the expected MCP timeout (e.g., 30s), and catch `FloodWaitError` with `.seconds > threshold` to return a user-friendly error message rather than crashing.
- Document that accounts with 500+ dialogs may see slower first-resolution.

**Warning signs:**
- Tool call takes >10s on first invocation.
- Telethon logs show `Sleeping for N seconds due to FloodWaitError`.
- `FloodWaitError: A wait of 120 seconds is required` propagates to MCP client as unhandled exception.

**Phase to address:** Name resolution phase — dialog cache must be designed before the resolver is built, not added later.

---

### Pitfall 7: Transliteration Auto-Detection Fails on Mixed-Script Names

**What goes wrong:**
The `transliterate` library's language/script detection is character-based and "very basic" (per its own documentation). For a query like `"Ivan Petrov"` (Latin), the library may not detect it as Cyrillic to transliterate into, or may misidentify a name that uses both Latin and Cyrillic characters (common in Telegram where usernames are Latin but display names are Cyrillic).

More critically: if the input query is already in Latin and the dialog name is in Cyrillic, the resolver must transliterate the **dialog name** to Latin (not the query to Cyrillic) for a fair comparison. Doing it the wrong direction doubles the error rate.

**Why it happens:**
The natural instinct is "user typed Latin, so transliterate the query to Cyrillic." But this introduces transliteration errors in the query. The better approach is to normalize both sides to the same script (Latin is canonical ASCII, so transliterate Cyrillic names to Latin).

**How to avoid:**
- Always transliterate Cyrillic **candidate names** to Latin, never transliterate the query.
- Normalize both query and candidate to lowercase ASCII before scoring.
- Test the resolver with: all-Cyrillic name, all-Latin name, mixed name (e.g., `"Алексей Smith"`), and names with diacritics (`"José"`).
- Do not rely on auto-detection — explicitly call `translit(candidate, 'ru', reversed=True)` (Cyrillic → Latin) and handle the `LanguageDetectionError` for non-Cyrillic strings.

**Warning signs:**
- Resolver finds `"Ivanov"` but not `"Иванов"` from the same query.
- `LanguageDetectionError` or `LanguagePackNotFound` for non-Russian Cyrillic (e.g., Ukrainian, Bulgarian).
- Transliteration of `"Щ"` produces `"Shch"` which fails WRatio against `"Sch"` due to multi-character expansion.

**Phase to address:** Name resolution phase — transliteration normalization strategy must be decided before WRatio scoring is implemented.

---

### Pitfall 8: Removing MCP Tools Requires Client-Side Cache Invalidation

**What goes wrong:**
The plan removes `GetDialog` and `GetMessage` tools. MCP clients (Claude Desktop, Claude Code, custom integrations) cache the tool list at connection time. After the server is updated and restarted, existing client sessions still see the old tools in their cached list. Calling a removed tool results in either a silent failure (tool not found in dispatcher) or an unhandled `NotImplementedError` from the `@singledispatch` default.

For Claude Code specifically: tools are only rediscovered at session startup. An open session will retain the stale tool list for its entire lifetime.

**Why it happens:**
MCP spec allows clients to cache `tools/list` responses for performance. There is no mandatory server-push invalidation mechanism in the stdio transport. The `listChanged` notification exists in the MCP spec but is not guaranteed to be honored by all clients.

**How to avoid:**
- Keep removed tool classes registered but have their `tool_runner` implementations return a `TextContent` error message: `"This tool has been removed. Use ListDialogs instead."` This prevents unhandled exceptions.
- Alternatively, during the transition period, keep the old tool stubs registered and log a deprecation warning.
- For Docker deployments: after deploying the new image, instruct users to restart their MCP client sessions (or add a note in the AGENTS.md).
- Do not rely on `listChanged` notification to remove tools from client UI — it is best-effort.

**Warning signs:**
- LLM attempts to call `GetDialog` after it has been removed, gets an unhandled `NotImplementedError`.
- Client tool list still shows removed tools after server restart.
- No error shown to user — tool call silently returns nothing.

**Phase to address:** Tool removal phase — add stub error responses before removing registrations; remove stubs only after confirming no active sessions use the old tools.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| No dialog cache (re-fetch on every resolution) | Simpler code, always fresh | FloodWaitError on large accounts, slow tool calls | Only for accounts with <50 dialogs |
| Skip service message type check, silently drop | Less code | Session-break gaps are wrong; crash if attribute accessed | Never — the type check is two lines |
| Use `offset_id` instead of `max_id` for cursor | Consistent parameter name | Off-by-one on page boundary, hard to debug | Never — use `max_id` exclusively |
| Transliterate query to Cyrillic (not the reverse) | Intuitive direction | Double error: transliteration errors in query propagate to WRatio | Never — always transliterate candidates, not the query |
| Hard-code `flood_sleep_threshold=60` | No config needed | Tool calls time out silently during dialog iteration | Only if MCP transport timeout is >90s |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| Telethon entity resolution | Call `get_entity(id)` for every resolved dialog | Pre-populate entity cache via `iter_dialogs()` at startup; `get_input_entity()` is then a cache hit |
| Telethon reactions | Access `message.reactions.results[0].reaction.emoticon` directly | Check `message.reactions is not None`, iterate `.results`, use `getattr(r.reaction, 'emoticon', '?')` |
| MCP `@singledispatch` routing | Remove class → remove registration → unhandled call | Keep stub registration returning error text during transition |
| Telethon forwarded messages | Call `get_entity(fwd_from.from_id)` inside formatter | Use `fwd_from.from_name` inline; never resolve inside formatter loop |
| rapidfuzz WRatio | Pass raw display names with emojis or punctuation | Pre-process: strip emojis, normalize Unicode, lowercase before scoring |
| Telethon `iter_messages` with `search=` | Combine `search` with `max_id` expecting both to filter | Telegram API ignores `max_id` when `search` is set — search and pagination are mutually exclusive |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Entity resolution inside message formatter loop | Formatting 100 messages takes 30s+ | Never call `get_entity()` inside formatter; use `.sender` cached on the message object | Any conversation with forwarded messages from unknown channels |
| Rebuilding dialog candidate list on every tool call | First-call latency always high, flood errors under load | In-process TTL cache (5-10 min) keyed on session | Accounts with >100 dialogs, any repeated tool call |
| WRatio against full dialog list with no early termination | Slow resolution on accounts with 500+ dialogs | `rapidfuzz.process.extractOne` with `score_cutoff=60` stops early | Accounts with >200 dialogs |
| Decoding base64 cursor without validation | Crash on corrupted or client-modified cursor | Validate decoded cursor is a positive integer; return 400-style error on failure | Any client that modifies the cursor value |

---

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| Logging resolved entity names or message text | Leaks real user data to server logs (security concern already addressed in commit `8532917`) | Log only event type and arg structure, never content or names |
| Exposing numeric message IDs in formatted output | LLM can construct arbitrary GetMessage calls using leaked IDs | Keep IDs internal; opaque base64 cursor is the only pagination handle exposed |
| Accepting cursor tokens without validation | Malformed cursor crashes formatter or enables ID enumeration | Validate base64 decode succeeds and result is a positive integer in expected range |

---

## "Looks Done But Isn't" Checklist

- [ ] **Fuzzy resolver:** Returns ambiguity list (60-89 range) — verify the list is surfaced to LLM, not silently dropped
- [ ] **Fuzzy resolver:** Handles query that matches nothing (<60) — verify returns a clear "not found" message, not an empty list
- [ ] **Message formatter:** Tested against a group with service messages (join/leave) — verify no AttributeError and session-break timing is correct
- [ ] **Cursor pagination:** Verified no duplicate at page boundary — page N last message != page N+1 first message
- [ ] **Cursor pagination:** Verified cursor for "no more pages" case — what is returned when `iter_messages` yields fewer items than `limit`?
- [ ] **Reactions:** Tested in a chat where reactions are disabled — verify `message.reactions is None` handled
- [ ] **Reactions:** Tested in a chat with custom emoji reactions — verify no AttributeError on `.emoticon`
- [ ] **Tool removal:** `GetDialog` and `GetMessage` stubs return helpful error, not unhandled exception
- [ ] **Transliteration:** Resolver tested with Ukrainian/Belarusian names — verify no `LanguagePackNotFound`
- [ ] **Flood handling:** Tool tested on account with 300+ dialogs — verify no timeout at MCP layer

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Stale entity cache breaks resolution | LOW | Call `client.get_dialogs()` once to refresh session cache; the stale entry gets overwritten |
| `max_id` vs `offset_id` off-by-one discovered in production | MEDIUM | Fix the parameter, invalidate opaque cursors (bump cursor version prefix), inform users to restart pagination |
| `FloodWaitError` causing timeouts | LOW | Add TTL dialog cache; set `flood_sleep_threshold=30`; return descriptive error for waits beyond threshold |
| Removed tool causes unhandled exception | LOW | Add stub handler returning error text; deploy without client restart requirement |
| Transliteration wrong direction discovered | MEDIUM | Swap transliteration target (candidates, not query); re-test all resolution scenarios; no user-visible data lost |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Stale entity cache | Name resolution phase | Test: resolve a name, change dialog name in Telegram, re-resolve — must not crash |
| `max_id` vs `offset_id` semantics | Cursor pagination phase | Integration test: three pages, verify no duplicates and no gaps |
| `MessageService` crashes | Message formatting phase | Unit test formatter with `MessageService` fixture |
| `None` reactions crash | Message formatting phase | Unit test with `message.reactions = None` and custom emoji fixture |
| `fwd_from` resolution in formatter loop | Message formatting phase | Code review gate: no `get_entity()` calls inside formatter |
| `FloodWaitError` during dialog iteration | Name resolution phase (dialog cache design) | Load test: call resolver 5 times in 10s on large account |
| Transliteration direction | Name resolution phase | Test matrix: Latin query vs Cyrillic name, Cyrillic query vs Latin name |
| Removed tool unhandled call | Tool removal phase | Integration test: call `GetDialog` after removal, expect graceful error not exception |

---

## Sources

- Telethon GitHub Issues: #4540 (`from_id` None), #3183 (entity not found), #4084 (get_entity by ID), #335 (forwarded name)
- Telethon docs: https://docs.telethon.dev/en/stable/concepts/entities.html
- Telethon TL API: https://tl.telethon.dev/constructors/message_reactions.html
- `transliterate` PyPI page: language detection is "very basic and based on characters only"
- MCP GitHub Issues: #17975 (hot-reload), VS Code #256421 (tool caching)
- Codebase analysis: `src/mcp_telegram/tools.py` — existing `max_id` usage pattern
- Commit `8532917` — confirmed prior log-leaking issue; do not reintroduce

---
*Pitfalls research for: mcp-telegram Telethon/MCP refactoring*
*Researched: 2026-03-11*
