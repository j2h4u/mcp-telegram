# Phase 3: New Tools - Research

**Researched:** 2026-03-11
**Domain:** Telethon MTProto API — account introspection and user profile lookup
**Confidence:** HIGH

## Summary

Phase 3 adds two read-only tools to the existing singledispatch/ToolArgs pattern: `GetMe` (no arguments, returns own account info) and `GetUserInfo` (name string argument, returns target user profile + common chats). Both tools slot cleanly into the established architecture — no new dependencies, no new modules, no schema changes.

`GetMe` calls `client.get_me()`, which returns a Telethon `User` object with `id`, `first_name`, `last_name`, and `username` fields. This is already exercised in `telegram.py`'s `connect_to_telegram` function, so the pattern is proven. `GetUserInfo` requires two Telethon calls: `client.get_entity(entity_id)` to fetch the `User` object, plus `functions.messages.GetCommonChatsRequest(user_id=entity_id, max_id=0, limit=100)` to fetch shared chats. Name resolution uses the existing `resolve()` + entity cache pathway already used by `ListMessages` and `SearchMessages`.

**Primary recommendation:** Add both tools directly to `tools.py` following the existing 3-step pattern (define ToolArgs subclass, implement registered async function, done). No new files, no new dependencies.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TOOL-08 | `GetMe` returns own name, id, and username | `client.get_me()` returns Telethon `User` with `id`, `first_name`, `last_name`, `username` fields — all available directly |
| TOOL-09 | `GetUserInfo` returns target user's profile and list of common chats | Name resolution via existing `resolve()` + cache; user object via `client.get_entity(entity_id)`; common chats via `GetCommonChatsRequest`; result has `.chats` list with `id` and `title`/`first_name` |
</phase_requirements>

## Standard Stack

### Core (already installed — no changes needed)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| telethon | >=1.23.0 | MTProto client — `get_me()`, `get_entity()`, `GetCommonChatsRequest` | Project-wide Telegram client |
| mcp | >=1.1.0 | TextContent, Tool types | Project-wide MCP transport |
| pydantic | >=2.0.0 | ToolArgs BaseModel subclasses | Project-wide input schema |

### Telethon APIs for Phase 3
| Call | Import | Returns |
|------|--------|---------|
| `await client.get_me()` | no extra import | `telethon.tl.types.User` |
| `await client.get_entity(entity_id)` | no extra import | `telethon.tl.types.User` |
| `await client(GetCommonChatsRequest(...))` | `from telethon.tl.functions.messages import GetCommonChatsRequest` | `messages.Chats` with `.chats` list |

**Installation:** No new packages needed.

## Architecture Patterns

### How New Tools Integrate

The project has a documented, tested pattern (see `tools.py` lines 29–49):

1. Define a `ToolArgs` subclass — its docstring becomes the MCP tool description; attributes become the JSON input schema.
2. Register an async function via `@tool_runner.register`.
3. The server auto-discovers it at startup.

### Recommended Structure in tools.py
```
### GetMe ###         (after SearchMessages section)

class GetMe(ToolArgs):
    """..."""
    pass   # no arguments

@tool_runner.register
async def get_me(args: GetMe) -> ...:
    ...

### GetUserInfo ###

class GetUserInfo(ToolArgs):
    """..."""
    user: str

@tool_runner.register
async def get_user_info(args: GetUserInfo) -> ...:
    ...
```

### Pattern: GetMe Implementation
**What:** Call `client.get_me()`, extract `id`, display name (`first_name` + optional `last_name`), and `username`. Return as a single formatted `TextContent`.
**Telethon return:** `User` object; `id` is numeric; `username` may be `None`; `first_name` is always set for real users.
**Example:**
```python
# Source: telegram.py (existing usage), tl.telethon.dev/constructors/user.html
async with create_client() as client:
    me = await client.get_me()
    if me is None:
        return [TextContent(type="text", text="Not logged in")]
    name = " ".join(filter(None, [me.first_name, me.last_name]))
    username = me.username or "none"
    text = f"id={me.id} name='{name}' username=@{username}"
    return [TextContent(type="text", text=text)]
```

### Pattern: GetUserInfo Implementation
**What:** Resolve name via `resolve()`, call `get_entity()` for user fields, call `GetCommonChatsRequest` for shared chats, format both into a single TextContent.
**Name resolution:** Same pattern as `list_messages` — `resolve(args.user, cache.all_names())`, handle `NotFound` and `Candidates` early returns.
**Common chats call:**
```python
# Source: tl.telethon.dev/methods/messages/get_common_chats.html
from telethon.tl.functions.messages import GetCommonChatsRequest

result = await client(GetCommonChatsRequest(
    user_id=entity_id,
    max_id=0,
    limit=100,
))
# result.chats is a list; each chat has .id, .title (group/channel) or .first_name (bot user)
```

**Resolver annotation prefix:** The success criterion requires the resolver annotation prefix to appear in the response. Looking at existing tools: `resolve()` returns a `Resolved` with `display_name`. The pattern is to include a `[resolved: "display_name"]` prefix — but the existing tools do NOT emit this prefix; they use the resolved `entity_id` silently. For Phase 3, the success criterion explicitly states "resolver annotation prefix appears in the response", so `GetUserInfo` must emit something like `[resolved: "Display Name"]` in its output. This is new behavior specific to this tool. See Open Questions.

### Anti-Patterns to Avoid
- **Calling `GetCommonChatsRequest` with `user_id` as a raw integer directly:** Telethon accepts "anything entity-like" for `user_id` but needs the user to be known. Use the entity_id from `resolve()` — Telethon will resolve it internally via its input-peer cache.
- **Calling `get_entity()` before connecting:** Both calls must be inside `async with create_client() as client:`.
- **Assuming `username` is always set:** It may be `None` — use `or "none"` or similar.
- **Assuming `me` is not `None`:** `get_me()` returns `None` if not authorized. Guard with an early return.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Fetching common chats | Custom dialog iteration + intersection logic | `GetCommonChatsRequest` | Telegram API returns this natively; rolling it risks rate-limits and is O(N*M) |
| Name display for groups | Custom title extraction logic | `getattr(chat, "title", None) or getattr(chat, "first_name", "")` | Chat objects are polymorphic; safe attr access handles both |
| User display name | Manual string construction | `" ".join(filter(None, [u.first_name, u.last_name]))` | Same pattern already used in list_messages sender name logic |

**Key insight:** The Telegram MTProto API has first-class support for both operations; no approximations needed.

## Common Pitfalls

### Pitfall 1: `get_me()` Outside Client Context
**What goes wrong:** `AttributeError` or `RuntimeError` — client not connected.
**Why it happens:** `create_client()` returns a `TelegramClient` that must be used as an async context manager.
**How to avoid:** Always call inside `async with create_client() as client:`.
**Warning signs:** Test raises `RuntimeError: You must be connected` or `AttributeError: 'NoneType'`.

### Pitfall 2: `UserIdInvalidError` from GetCommonChatsRequest
**What goes wrong:** Telethon raises `UserIdInvalidError` if `user_id` can't be resolved.
**Why it happens:** The entity_id from the resolver may refer to a group/channel, not a user, or the user may have blocked the account.
**How to avoid:** Wrap the `GetCommonChatsRequest` call in a try/except; return an informative error message.
**Warning signs:** `UserIdInvalidError: The provided user is not valid`.

### Pitfall 3: `get_me()` Returns `UserEmpty` for Bots or Deleted Accounts
**What goes wrong:** `me.first_name` is None or attribute missing.
**Why it happens:** In some edge cases `get_me()` can return a `UserEmpty` type.
**How to avoid:** Check `me is not None` and use `getattr(me, "first_name", None)`.

### Pitfall 4: Mock Setup for `client.get_me()` in Tests
**What goes wrong:** `AsyncMock` for `get_me()` not set up — returns default `MagicMock` instead of a User-like object.
**Why it happens:** Existing `mock_client` fixture doesn't configure `get_me`.
**How to avoid:** In each test that exercises `GetMe`, set `mock_client.get_me = AsyncMock(return_value=MagicMock(id=999, first_name="Test", last_name=None, username="testuser"))`.

### Pitfall 5: Common Chats Result Format
**What goes wrong:** `result.chats` elements have inconsistent `title` vs `first_name` — groups have `.title`, user-bots have `.first_name`.
**Why it happens:** `messages.Chats.chats` is a polymorphic list (Chat, Channel, etc.).
**How to avoid:** Use `getattr(chat, "title", None) or getattr(chat, "first_name", str(chat.id))`.

## Code Examples

Verified patterns from official sources and existing project code:

### GetMe — Full Pattern
```python
# Source: tl.telethon.dev/constructors/user.html, existing telegram.py usage
@tool_runner.register
async def get_me(args: GetMe) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetMe] args[%s]", args)
    async with create_client() as client:
        me = await client.get_me()
    if me is None:
        return [TextContent(type="text", text="Not authenticated")]
    name = " ".join(filter(None, [
        getattr(me, "first_name", None),
        getattr(me, "last_name", None),
    ]))
    username = getattr(me, "username", None) or "none"
    text = f"id={me.id} name='{name}' username=@{username}"
    return [TextContent(type="text", text=text)]
```

### GetUserInfo — Resolver + Profile + Common Chats
```python
# Source: tl.telethon.dev/methods/messages/get_common_chats.html
# existing list_messages resolver pattern
@tool_runner.register
async def get_user_info(args: GetUserInfo) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetUserInfo] args[%s]", args)
    cache = get_entity_cache()
    result = resolve(args.user, cache.all_names())
    if isinstance(result, NotFound):
        return [TextContent(type="text", text=f'User not found: "{args.user}"')]
    if isinstance(result, Candidates):
        names = ", ".join(f'"{m[0]}"' for m in result.matches[:5])
        return [TextContent(type="text", text=f'Ambiguous user "{args.user}". Matches: {names}')]
    entity_id: int = result.entity_id
    display_name: str = result.display_name

    async with create_client() as client:
        try:
            user = await client.get_entity(entity_id)
            common_result = await client(GetCommonChatsRequest(
                user_id=entity_id,
                max_id=0,
                limit=100,
            ))
        except Exception as exc:
            return [TextContent(type="text", text=f"Error fetching user info: {exc}")]

    name = " ".join(filter(None, [
        getattr(user, "first_name", None),
        getattr(user, "last_name", None),
    ]))
    username = getattr(user, "username", None) or "none"
    chat_lines = []
    for chat in common_result.chats:
        chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", str(chat.id))
        chat_lines.append(f"  id={chat.id} name='{chat_name}'")
    chats_text = "\n".join(chat_lines) if chat_lines else "  (none)"
    text = (
        f"[resolved: \"{display_name}\"]\n"
        f"id={entity_id} name='{name}' username=@{username}\n"
        f"Common chats ({len(common_result.chats)}):\n{chats_text}"
    )
    return [TextContent(type="text", text=text)]
```

### Import to Add in tools.py
```python
from telethon.tl.functions.messages import GetCommonChatsRequest
```
This import is already present for `GetPeerDialogsRequest` (same `telethon.tl.functions.messages` module) — add `GetCommonChatsRequest` to that import line.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `telethon.sync` blocking calls | `async with client:` context manager | Project-wide from start | All tools are async |
| `client.get_user()` (hypothetical) | `client.get_me()` (native Telethon method) | Always | Direct, no intermediate step |

**No deprecated patterns identified for this phase.**

## Open Questions

1. **Resolver annotation prefix exact format**
   - What we know: Success criterion says "resolver annotation prefix appears in the response"
   - What's unclear: The exact format is not specified anywhere in REQUIREMENTS.md or STATE.md. The other tools do not emit such a prefix.
   - Recommendation: Emit `[resolved: "Display Name"]` as the first line of GetUserInfo output. This is visible, unambiguous, and easy to test. Confirm with planner or accept as discretionary choice.

2. **GetUserInfo: user-only or also groups/channels?**
   - What we know: TOOL-09 says "target user's profile" — profile implies individual user, not group.
   - What's unclear: What should happen if the resolved entity is a group? `GetCommonChatsRequest` with a group entity_id will fail with `UserIdInvalidError`.
   - Recommendation: Treat non-user entities as an error: return `"Entity is not a user"`. Cache has `type` field — can pre-check before calling.

3. **GetMe: include phone number?**
   - What we know: TOOL-08 says "name, id, and username" — phone is not mentioned.
   - Recommendation: Omit phone number from the output (privacy consideration, not required).

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x with pytest-asyncio |
| Config file | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode = "auto" |
| Quick run command | `uv run pytest tests/test_tools.py -x -q` |
| Full suite command | `uv run pytest -x -q` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TOOL-08 | GetMe returns id, name, username | unit | `uv run pytest tests/test_tools.py -k "get_me" -x` | ❌ Wave 0 |
| TOOL-08 | GetMe handles unauthenticated (me=None) | unit | `uv run pytest tests/test_tools.py -k "get_me_unauthenticated" -x` | ❌ Wave 0 |
| TOOL-09 | GetUserInfo resolves name, returns profile + chats | unit | `uv run pytest tests/test_tools.py -k "get_user_info" -x` | ❌ Wave 0 |
| TOOL-09 | GetUserInfo not-found path returns error text | unit | `uv run pytest tests/test_tools.py -k "get_user_info_not_found" -x` | ❌ Wave 0 |
| TOOL-09 | GetUserInfo ambiguous path returns candidates | unit | `uv run pytest tests/test_tools.py -k "get_user_info_ambiguous" -x` | ❌ Wave 0 |
| TOOL-09 | GetUserInfo resolver annotation prefix in output | unit | `uv run pytest tests/test_tools.py -k "get_user_info_resolver_prefix" -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_tools.py -x -q`
- **Per wave merge:** `uv run pytest -x -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_tools.py` — add test functions for TOOL-08 and TOOL-09 (file exists, append to it)
- [ ] `conftest.py` — extend `mock_client` fixture with `get_me` and `get_entity` stubs, OR set these up per-test (per-test preferred — less fixture coupling)

*(Existing test infrastructure covers all framework needs — pytest, asyncio_mode=auto, mock_client, mock_cache fixtures are all in place)*

## Sources

### Primary (HIGH confidence)
- `tl.telethon.dev/constructors/user.html` — User constructor fields: id, first_name, last_name, username, phone, bot flags
- `tl.telethon.dev/methods/messages/get_common_chats.html` — GetCommonChatsRequest parameters and return type
- `docs.telethon.dev/en/stable/examples/users.html` — GetFullUserRequest pattern (bio/about field)
- Existing `src/mcp_telegram/tools.py` — tool pattern, imports, singledispatch usage
- Existing `src/mcp_telegram/telegram.py` — `client.get_me()` usage in `connect_to_telegram`

### Secondary (MEDIUM confidence)
- `docs.telethon.dev/en/stable/concepts/entities.html` — entity resolution patterns
- WebSearch cross-verification of `GetCommonChatsRequest` usage

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all libraries already in pyproject.toml, no additions needed
- Architecture: HIGH — same pattern used by all 3 existing tools; code examples verified against official Telethon docs
- Pitfalls: HIGH — edge cases (None username, UserIdInvalidError, me=None) verified from official docs and existing code patterns
- Resolver annotation prefix format: LOW — requirement wording is ambiguous; exact format is a planner decision

**Research date:** 2026-03-11
**Valid until:** 2026-04-11 (Telethon API is stable; 30-day window)
