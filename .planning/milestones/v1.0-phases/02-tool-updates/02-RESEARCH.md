# Phase 2: Tool Updates - Research

**Researched:** 2026-03-11
**Domain:** Telethon API wiring, MCP tool refactoring, entity cache integration
**Confidence:** HIGH (all Phase 1 modules verified; Telethon API inspected from installed source)

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TOOL-01 | `ListDialogs` returns `type` (user/group/channel) and `last_message_at` for each dialog | `dialog.is_user`, `dialog.is_group`, `dialog.is_channel` confirmed on `Dialog` class; `dialog.date` = last_message_at (UTC datetime) |
| TOOL-02 | `ListMessages` accepts dialog by name, returns messages in unified format | `EntityCache.all_names()` → `resolve()` → entity_id; `format_messages()` from Phase 1; name param is `str` |
| TOOL-03 | `ListMessages` uses cursor-based pagination (opaque tokens, stable under concurrent message arrival) | `encode_cursor` / `decode_cursor` from Phase 1; `max_id` param on `iter_messages` |
| TOOL-04 | `ListMessages` accepts optional `sender` name filter | `iter_messages(entity, from_user=entity_id)` — accepts EntityLike including int; resolve sender name via `resolve()` first |
| TOOL-05 | `ListMessages` accepts optional `unread` filter | `dialog.unread_count` via `GetPeerDialogsRequest`; `dialog.dialog.read_inbox_max_id` gives cutoff; use `min_id=read_inbox_max_id` + `limit=unread_count` on `iter_messages` |
| TOOL-06 | `SearchMessages` accepts dialog by name, returns each result with ±3 messages of surrounding context | Two extra `iter_messages` calls per hit: `limit=3, max_id=hit_id` (before) and `limit=3, min_id=hit_id` (after) |
| TOOL-07 | `SearchMessages` uses offset-based pagination (`next_offset` absent when exhausted) | `iter_messages(search=q, add_offset=offset, limit=page_size)` — PROJECT.md decision: offset-based because Telegram Search uses `add_offset`, incompatible with cursor |
| CLNP-01 | `GetDialog` tool removed (no stubs, no BC obligations) | Delete class + `@tool_runner.register` function; server auto-discovery removes it from mapping |
| CLNP-02 | `GetMessage` tool removed (no stubs, no BC obligations) | Delete class + `@tool_runner.register` function; server auto-discovery removes it from mapping |
</phase_requirements>

---

## Summary

Phase 2 wires the four support modules built in Phase 1 into the existing MCP tools. Every tool change is in `src/mcp_telegram/tools.py`; the server auto-discovers tools by iterating `ToolArgs` subclasses, so no changes to `server.py` are required except to verify the final tool set.

The key integration points:
1. **Name resolution:** Every tool that takes a dialog or sender accepts a `str`; the tool resolves it via `EntityCache.all_names()` + `resolver.resolve()` before making any Telethon call.
2. **Format output:** `ListMessages` uses `format_messages()` from `formatter.py`; raw `[id=X] text` strings are gone.
3. **Pagination:** `ListMessages` emits a `next_cursor` token; `SearchMessages` emits `next_offset` (absent on last page).
4. **Cleanup:** `GetDialog` and `GetMessage` classes and their `@tool_runner.register` handlers are deleted — no deprecation stubs, no error shims.

The unread filter is the trickiest piece: the current implementation guesses at `dialog.unread_count` from `GetPeerDialogsRequest`, but the clean approach is to use `read_inbox_max_id` from the raw TL `Dialog` object (accessible as `peer_dialogs.dialogs[0].read_inbox_max_id`) combined with `iter_messages(min_id=read_inbox_max_id)`.

**Primary recommendation:** Rewrite all four affected tools in a single plan — they share the same resolver/cache wiring pattern and can be tested together with a consistent mock structure.

---

## Standard Stack

### Core (Phase 1 modules — all verified)
| Module | Location | Purpose |
|--------|----------|---------|
| `resolver.resolve()` | `mcp_telegram/resolver.py` | `str` → `Resolved | Candidates | NotFound` using fuzzy WRatio |
| `EntityCache` | `mcp_telegram/cache.py` | `all_names()` → `dict[int, str]` for resolver input |
| `format_messages()` | `mcp_telegram/formatter.py` | Message list → human-readable `HH:mm FirstName: text` |
| `encode_cursor` / `decode_cursor` | `mcp_telegram/pagination.py` | Opaque cursor token encode/decode |

### Telethon API methods used
| Method | Parameters of note | Used by |
|--------|-------------------|---------|
| `client.iter_dialogs()` | `archived`, `ignore_pinned` | `ListDialogs` |
| `client.iter_messages(entity, ...)` | `limit`, `max_id`, `min_id`, `from_user`, `search`, `add_offset` | `ListMessages`, `SearchMessages` |
| `client(GetPeerDialogsRequest([entity_id]))` | returns `PeerDialogs` with `dialogs[0].unread_count` and `dialogs[0].read_inbox_max_id` | `ListMessages` (unread filter) |

### No new dependencies required
All needed libraries are already in `pyproject.toml`: `telethon`, `rapidfuzz`, `pydantic`, `mcp`. No `uv add` commands needed.

---

## Architecture Patterns

### Recommended tool structure

Each tool follows the same four-step pattern:

```
1. Resolve dialog name → entity_id
   choices = cache.all_names()
   result = resolve(args.dialog, choices)
   if isinstance(result, NotFound): raise ValueError(...)
   if isinstance(result, Candidates): raise ValueError(... candidates ...)
   entity_id = result.entity_id

2. Fetch from Telethon
   async with create_client() as client:
       messages = [msg async for msg in client.iter_messages(entity_id, ...)]

3. Upsert entities to cache (lazy population)
   for msg in messages:
       if msg.sender: cache.upsert(msg.sender_id, "user", ...)

4. Format and return
   text = format_messages(messages, reply_map={})
   return [TextContent(type="text", text=text)]
```

### Pattern: EntityCache singleton

The `EntityCache` must be opened once and reused across tool calls — not opened per-call. The existing `create_client()` uses `@cache` for the same reason.

```python
# Source: cache.py + telegram.py pattern
from functools import cache
from pathlib import Path
from xdg_base_dirs import xdg_state_home

@cache
def get_entity_cache() -> EntityCache:
    db_path = xdg_state_home() / "mcp-telegram" / "entity_cache.db"
    return EntityCache(db_path)
```

### Pattern: Resolver error → TextContent error message

MCP tools return `Sequence[TextContent | ...]`. When resolution fails, return a TextContent error rather than raising, so the LLM can read the candidate list and retry.

```python
# Source: PROJECT.md architecture spec
from mcp_telegram.resolver import Candidates, NotFound, Resolved

result = resolve(args.dialog, get_entity_cache().all_names())
if isinstance(result, NotFound):
    return [TextContent(type="text", text=f'Dialog not found: "{args.dialog}"')]
if isinstance(result, Candidates):
    names = ", ".join(f'"{m[0]}"' for m in result.matches[:5])
    return [TextContent(type="text", text=f'Ambiguous dialog "{args.dialog}". Matches: {names}')]
entity_id = result.entity_id
```

### Pattern: ListDialogs — type + last_message_at

```python
# Source: Dialog.__init__ source + Project.MD spec
# dialog.is_user / is_group / is_channel are bool properties set in Dialog.__init__
# dialog.date is the last message datetime (UTC-aware)
async for dialog in client.iter_dialogs(...):
    if dialog.is_user:
        dtype = "user"
    elif dialog.is_group:
        dtype = "group"
    elif dialog.is_channel:
        dtype = "channel"
    else:
        dtype = "unknown"
    last_at = dialog.date.isoformat() if dialog.date else "unknown"
    # format as text line
```

### Pattern: ListMessages — cursor pagination

```python
# Source: pagination.py (encode_cursor/decode_cursor) + iter_messages source
# args.cursor: str | None — opaque token; None means "start from newest"
# Telethon max_id excludes that ID: messages with id < max_id

iter_kwargs: dict = {"entity": entity_id, "limit": args.limit, "reverse": False}
if args.cursor:
    before_id = decode_cursor(args.cursor, entity_id)
    iter_kwargs["max_id"] = before_id

messages = []
async for msg in client.iter_messages(**iter_kwargs):
    messages.append(msg)

# Build next_cursor from oldest message in page
next_cursor = None
if len(messages) == args.limit:
    oldest = messages[-1]   # newest-first; last = oldest
    next_cursor = encode_cursor(oldest.id, entity_id)
```

### Pattern: ListMessages — unread filter

```python
# Source: TL Dialog source (read_inbox_max_id confirmed) + GetPeerDialogsRequest
# The raw TL dialog (result.dialogs[0]) has read_inbox_max_id and unread_count
result = await client(GetPeerDialogsRequest(peers=[entity_id]))
tl_dialog = result.dialogs[0]   # types.Dialog (not custom.Dialog)
unread_count = tl_dialog.unread_count
read_max_id = tl_dialog.read_inbox_max_id

# Messages with id > read_max_id are unread
# iter_messages with min_id=read_max_id fetches messages NEWER than that id
iter_kwargs["min_id"] = read_max_id
iter_kwargs["limit"] = unread_count
```

### Pattern: ListMessages — sender filter

```python
# Source: iter_messages source: from_user accepts EntityLike (int entity_id works)
# Resolve sender name before calling iter_messages
if args.sender:
    sender_result = resolve(args.sender, get_entity_cache().all_names())
    if isinstance(sender_result, NotFound):
        return [TextContent(type="text", text=f'Sender not found: "{args.sender}"')]
    if isinstance(sender_result, Candidates):
        names = ", ".join(f'"{m[0]}"' for m in sender_result.matches[:5])
        return [TextContent(type="text", text=f'Ambiguous sender. Matches: {names}')]
    iter_kwargs["from_user"] = sender_result.entity_id
```

### Pattern: SearchMessages — offset pagination + context window

```python
# Source: PROJECT.md pagination spec ("offset-based because Telegram Search uses add_offset")
# iter_messages(entity, search=q, limit=page_size, add_offset=offset)
# Context: two additional iter_messages calls per result
# next_offset = offset + page_size if page had full results, else absent (None)

search_kwargs = {
    "entity": entity_id,
    "search": args.query,
    "limit": args.limit,
    "add_offset": args.offset or 0,
}
results = []
async for msg in client.iter_messages(**search_kwargs):
    # Fetch ±3 context messages
    before = list(reversed([m async for m in client.iter_messages(entity_id, limit=3, max_id=msg.id)]))
    after = [m async for m in client.iter_messages(entity_id, limit=3, min_id=msg.id)]
    window = before + [msg] + after
    results.append(format_messages(window, reply_map={}))

next_offset = (args.offset or 0) + args.limit if len(results) == args.limit else None
```

Note: `iter_messages` with `max_id=X` returns messages with id < X (exclusive); with `min_id=X` returns messages with id > X (exclusive). The hit message itself is not included in before/after — it's added explicitly to the center.

### Pattern: Tool cleanup (CLNP-01, CLNP-02)

Delete `GetDialog` and `GetMessage` classes and their `@tool_runner.register` functions entirely. The server's `enumerate_available_tools()` builds its mapping by inspecting `tools` module members — removal is automatic. No stubs, no raised errors, no backward-compat shims.

### Anti-Patterns to Avoid

- **Opening EntityCache per tool call:** Same problem as opening a new SQLite connection per call — use `@cache` singleton.
- **Passing `dialog.id` directly to iter_messages without resolving name first:** The tool input is now a `str` name, not an int.
- **iter_messages with `ids=[n-3..n+3]`:** Message IDs are not sequential (messages may be deleted); use `min_id`/`max_id` range.
- **Raising exceptions for resolution failures:** Return `TextContent` error so the LLM can act on the candidate list.
- **`Optional[X]` or `typing.Union`:** Use `X | None` — Python 3.11 project.
- **`before_id: int` pagination parameter:** This was the Phase 1 API. Phase 2 replaces it with an opaque `cursor: str | None`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Name → entity_id lookup | Custom string matching | `resolver.resolve()` | Already tested; WRatio thresholds locked in |
| Message text formatting | Inline format strings in tool | `format_messages()` | FMT-01 already implemented and tested |
| Cursor encode/decode | Custom pagination token | `encode_cursor` / `decode_cursor` | Cross-dialog validation already handled |
| Entity persistence | Inline SQLite calls | `EntityCache.upsert()` / `all_names()` | Schema and WAL mode already set up |
| Pagination for Search | cursor-based | `add_offset` (Telegram native) | PROJECT.md decision: Telegram Search API uses `add_offset`, cursor would require additional message fetches |

---

## Common Pitfalls

### Pitfall 1: `iter_messages` with `from_user` switches to Search API
**What goes wrong:** When `from_user` is set, Telethon internally uses `messages.Search` instead of `messages.getHistory`. This changes rate-limit bucket (Search has stricter limits) and means `max_id`/`min_id` behave slightly differently.
**Why it happens:** `from_user` filter is not available in `getHistory`; Telethon routes to `Search`.
**How to avoid:** When combining `from_user` with cursor pagination, verify that `max_id` still works correctly with the Search backend. Test with a small `limit`.
**Warning signs:** Pagination skips messages or returns duplicates when `from_user` is combined with `cursor`.

### Pitfall 2: `GetPeerDialogsRequest` requires `InputPeer`, not bare int
**What goes wrong:** Passing a raw int (entity_id) to `GetPeerDialogsRequest` may fail with `PeerIdInvalidError` because Telethon needs an `InputPeer` resolved from the entity.
**Why it happens:** MTProto peers require type information (User/Chat/Channel) in the InputPeer structure.
**How to avoid:** Use `await client.get_input_entity(entity_id)` to resolve to an `InputPeer` first. Alternatively, call `client.iter_dialogs()` and filter by name/id — the Dialog object already has `input_entity`.
**Warning signs:** `PeerIdInvalidError` when calling `GetPeerDialogsRequest`.

### Pitfall 3: `dialog.date` is None for dialogs with no messages
**What goes wrong:** A freshly created group with no messages has `dialog.date = None`.
**Why it happens:** `dialog.date` comes from the last message; if no message exists, it's None.
**How to avoid:** Use `dialog.date.isoformat() if dialog.date else "unknown"` in ListDialogs output.
**Warning signs:** AttributeError `'NoneType' object has no attribute 'isoformat'`.

### Pitfall 4: SearchMessages context window overlap between pages
**What goes wrong:** If `limit=5` and two search hits are close together (e.g., messages 100 and 103), their ±3 context windows overlap. The same messages appear in multiple context blocks on the same page.
**Why it happens:** Context windows are fetched independently per hit.
**How to avoid:** This is acceptable behavior — overlap is cosmetic. The LLM will see the messages twice but can recognize them. No deduplication needed for v1.
**Warning signs:** None — not a correctness issue, just verbosity.

### Pitfall 5: `encode_cursor` uses entity_id but tools.py previously used dialog_id
**What goes wrong:** `encode_cursor(message_id, dialog_id)` must be called with the **resolved** entity_id (the integer), not the string name. Passing the wrong value produces cursors that decode successfully but point to the wrong dialog.
**Why it happens:** `dialog_id` in the cursor is compared against `expected_dialog_id` at decode time — only catches cross-dialog reuse, not corruption.
**How to avoid:** Always pass `entity_id = result.entity_id` (the int from resolver) to both cursor functions and to `iter_messages`.

### Pitfall 6: `min_id` semantics (exclusive lower bound)
**What goes wrong:** `iter_messages(entity, min_id=read_inbox_max_id)` returns messages with `id > read_inbox_max_id`. This is correct for unread (the last-read message itself is already read), but confusing.
**Why it happens:** Both `min_id` and `max_id` are exclusive in Telethon/Telegram API.
**How to avoid:** Document the exclusive semantics in code comments. Test: if `read_inbox_max_id=50` and message 50 is the last-read, messages 51+ are unread — verify the test fixture uses this constraint.

### Pitfall 7: Removing tools requires restarting the running server
**What goes wrong:** `enumerate_available_tools()` uses `@cache` — it runs once at import time. Removing `GetDialog`/`GetMessage` at runtime has no effect on an already-running server.
**Why it happens:** `mapping` dict is built once when `server.py` is first imported.
**How to avoid:** Not a code problem — this is expected behavior. Document in task verification: the cleanup is confirmed by import-time inspection, not runtime patching.

---

## Code Examples

Verified from installed Telethon source and Phase 1 modules:

### ListDialogs with type and last_message_at
```python
# Source: Dialog.__init__ source (is_user/is_group/is_channel/date confirmed)
async for dialog in client.iter_dialogs(archived=False, ignore_pinned=False):
    if dialog.is_user:
        dtype = "user"
    elif dialog.is_group:
        dtype = "group"
    elif dialog.is_channel:
        dtype = "channel"
    else:
        dtype = "unknown"
    last_at = dialog.date.isoformat() if dialog.date else "unknown"
    msg = (
        f"name='{dialog.name}' id={dialog.id} type={dtype} "
        f"last_message_at={last_at} "
        f"unread={dialog.unread_count}"
    )
```

### Resolve name to entity_id
```python
# Source: resolver.py (Phase 1 verified)
from mcp_telegram.resolver import Candidates, NotFound, Resolved, resolve
from mcp_telegram.cache import EntityCache

choices = cache.all_names()   # dict[int, str]
result = resolve(args.dialog, choices)
if isinstance(result, NotFound):
    return [TextContent(type="text", text=f'Dialog not found: "{args.dialog}"')]
if isinstance(result, Candidates):
    names = ", ".join(f'"{m[0]}"' for m in result.matches[:5])
    return [TextContent(type="text", text=f'Ambiguous: "{args.dialog}". Matches: {names}')]
entity_id: int = result.entity_id
```

### ListMessages with cursor pagination
```python
# Source: pagination.py (Phase 1 verified) + iter_messages signature (confirmed)
from mcp_telegram.pagination import decode_cursor, encode_cursor

iter_kwargs: dict = {"entity": entity_id, "limit": args.limit, "reverse": False}
if args.cursor:
    iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)

messages = [msg async for msg in client.iter_messages(**iter_kwargs)]

next_cursor: str | None = None
if len(messages) == args.limit and messages:
    next_cursor = encode_cursor(messages[-1].id, entity_id)

text = format_messages(messages, reply_map={})
result_text = text
if next_cursor:
    result_text += f"\n\nnext_cursor: {next_cursor}"
return [TextContent(type="text", text=result_text)]
```

### SearchMessages with offset pagination + context window
```python
# Source: PROJECT.md spec (add_offset decision) + iter_messages signature (confirmed)
page_offset = args.offset or 0
hits = [msg async for msg in client.iter_messages(
    entity_id, search=args.query, limit=args.limit, add_offset=page_offset
)]

blocks: list[str] = []
for hit in hits:
    before = list(reversed([m async for m in client.iter_messages(
        entity_id, limit=3, max_id=hit.id
    )]))
    after = [m async for m in client.iter_messages(
        entity_id, limit=3, min_id=hit.id
    )]
    window = before + [hit] + after
    blocks.append(format_messages(window, reply_map={}))

result_text = "\n\n---\n\n".join(blocks)
if len(hits) == args.limit:
    result_text += f"\n\nnext_offset: {page_offset + args.limit}"
return [TextContent(type="text", text=result_text)]
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `dialog_id: int` in tool args | `dialog: str` (name) | Phase 2 | LLM sends name; tool resolves |
| `before_id: int` pagination | `cursor: str \| None` (opaque) | Phase 2 | Hides message IDs from LLM |
| `[id=X] text` raw output | `format_messages()` human-readable | Phase 2 | Consistent with FMT-01 |
| `GetDialog` / `GetMessage` tools | Removed | Phase 2 | Required IDs; incompatible with name API |
| Offset pagination for ListMessages | Cursor pagination | Phase 2 | Stable under real-time arrival |

**Deprecated/outdated:**
- `GetDialog(dialog_id: int)`: removed entirely (CLNP-01)
- `GetMessage(dialog_id: int, message_id: int)`: removed entirely (CLNP-02)
- `ListMessages.before_id: int | None`: replaced by `cursor: str | None`
- Raw `[id={message.id}] {message.text}` format string: replaced by `format_messages()`

---

## Open Questions

1. **EntityCache singleton: where to initialize db_path?**
   - What we know: `create_client()` uses `xdg_state_home() / "mcp-telegram"` for session storage.
   - What's unclear: Should `entity_cache.db` live in the same directory? Or should there be a separate config path?
   - Recommendation: Use the same `xdg_state_home() / "mcp-telegram" / "entity_cache.db"` path; consistent with session file location; no new env var needed.

2. **Cache warm-up: when to call `EntityCache.upsert()`?**
   - What we know: Phase 1 spec says "lazy-populated from API responses". `ListDialogs` iterates all dialogs — a natural warm-up point.
   - What's unclear: Should `ListDialogs` call `cache.upsert()` for each dialog entity? This was decided as the cache population mechanism.
   - Recommendation: Yes — `ListDialogs` should upsert each dialog entity (id, type, name, username) into cache on every call. This is the primary lazy-population trigger.

3. **Sender filter + cursor pagination combination**
   - What we know: `from_user` switches Telethon to `messages.Search` backend internally; `max_id` still works.
   - What's unclear: Whether cursor pagination is fully stable when combined with `from_user` filter (different API endpoint).
   - Recommendation: Implement and add an integration note in the task. Test stub should check that pagination kwargs are passed correctly; real validation requires a live session.

4. **SearchMessages: how many context API calls are acceptable?**
   - What we know: Each search result requires 2 extra `iter_messages` calls (before + after). With `limit=5`, that's up to 15 API calls per `SearchMessages` invocation.
   - What's unclear: Rate limit impact in practice.
   - Recommendation: For Phase 2, implement as specified (±3 per hit). Rate limits are a Phase 3+ concern. `wait_time` param on `iter_messages` can be set to 0 for small fetches.

---

## Validation Architecture

nyquist_validation is enabled (config.json).

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-asyncio (asyncio_mode=auto) |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` — already exists |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest tests/ -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TOOL-01 | `ListDialogs` output line contains `type=user/group/channel` and `last_message_at=` | unit (mock dialog) | `uv run pytest tests/test_tools.py::test_list_dialogs_type_field -x` | Wave 0 |
| TOOL-01 | `ListDialogs` handles `dialog.date = None` gracefully | unit | `uv run pytest tests/test_tools.py::test_list_dialogs_null_date -x` | Wave 0 |
| TOOL-02 | `ListMessages` called with a name returns `format_messages()` output | unit (mock client) | `uv run pytest tests/test_tools.py::test_list_messages_by_name -x` | Wave 0 |
| TOOL-02 | `ListMessages` with unresolved name returns TextContent error with "not found" | unit | `uv run pytest tests/test_tools.py::test_list_messages_not_found -x` | Wave 0 |
| TOOL-02 | `ListMessages` with ambiguous name returns TextContent with candidates list | unit | `uv run pytest tests/test_tools.py::test_list_messages_ambiguous -x` | Wave 0 |
| TOOL-03 | `ListMessages` with full page returns `next_cursor` token in output | unit | `uv run pytest tests/test_tools.py::test_list_messages_cursor_present -x` | Wave 0 |
| TOOL-03 | `ListMessages` with partial page has no `next_cursor` in output | unit | `uv run pytest tests/test_tools.py::test_list_messages_no_cursor_last_page -x` | Wave 0 |
| TOOL-04 | `ListMessages` with `sender` param passes `from_user=entity_id` to iter_messages | unit | `uv run pytest tests/test_tools.py::test_list_messages_sender_filter -x` | Wave 0 |
| TOOL-05 | `ListMessages` with `unread=True` passes `min_id=read_inbox_max_id` to iter_messages | unit | `uv run pytest tests/test_tools.py::test_list_messages_unread_filter -x` | Wave 0 |
| TOOL-06 | `SearchMessages` output contains context messages before and after each hit | unit | `uv run pytest tests/test_tools.py::test_search_messages_context -x` | Wave 0 |
| TOOL-07 | `SearchMessages` full page returns `next_offset` in output | unit | `uv run pytest tests/test_tools.py::test_search_messages_next_offset -x` | Wave 0 |
| TOOL-07 | `SearchMessages` last page has no `next_offset` in output | unit | `uv run pytest tests/test_tools.py::test_search_messages_no_next_offset -x` | Wave 0 |
| CLNP-01 | `GetDialog` class does not exist in `tools` module | unit (import check) | `uv run pytest tests/test_tools.py::test_get_dialog_removed -x` | Wave 0 |
| CLNP-02 | `GetMessage` class does not exist in `tools` module | unit (import check) | `uv run pytest tests/test_tools.py::test_get_message_removed -x` | Wave 0 |

### Testing Strategy for Telethon Integration

Tools make async Telethon calls; tests must mock `create_client()` context manager. Pattern established from Phase 1 formatter tests (mock message objects):

```python
# Approach: mock create_client() to return an AsyncMock client
# Use pytest monkeypatch or unittest.mock.patch
# Mock client.iter_dialogs(), client.iter_messages(), client(GetPeerDialogsRequest(...))
# Mock EntityCache.all_names() to return a fixed dict

from unittest.mock import AsyncMock, MagicMock, patch

@pytest.fixture
def mock_cache(tmp_db_path):
    cache = EntityCache(tmp_db_path)
    cache.upsert(101, "user", "Иван Петров", "ivan")
    return cache
```

### Sampling Rate

- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest tests/ -v`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_tools.py` — 14 stub tests covering TOOL-01 through TOOL-07, CLNP-01, CLNP-02
- [ ] Fixtures in `tests/conftest.py` — `mock_cache` fixture (EntityCache seeded with sample data); mock Telethon client helpers

*(Existing test files `test_resolver.py`, `test_formatter.py`, `test_cache.py`, `test_pagination.py` already pass — no changes needed)*

---

## Sources

### Primary (HIGH confidence)
- `src/mcp_telegram/tools.py` — current tool implementations (all four tools: ListDialogs, ListMessages, SearchMessages, GetDialog, GetMessage)
- `src/mcp_telegram/server.py` — auto-discovery mechanism via `inspect.getmembers`
- `src/mcp_telegram/resolver.py`, `cache.py`, `formatter.py`, `pagination.py` — Phase 1 verified modules
- `uv run python -c "inspect.getsource(TelegramClient.iter_messages)"` — confirmed: `from_user`, `max_id`, `min_id`, `add_offset`, `search` params all present in installed version
- `uv run python -c "inspect.getsource(Dialog.__init__)"` — confirmed: `is_user`, `is_group`, `is_channel`, `date`, `unread_count` all present
- `uv run python -c "inspect.getsource(TLDialog.__init__)"` — confirmed: `read_inbox_max_id` accessible on raw TL Dialog object
- `.planning/PROJECT.md` — locked architectural decisions (offset-based search pagination, cursor-based list pagination, names as strings)
- `.planning/STATE.md` — accumulated decisions including "Remove GetDialog + GetMessage: no BC obligations"

### Secondary (MEDIUM confidence)
- `https://docs.telethon.dev/en/stable/modules/custom.html#telethon.tl.custom.dialog.Dialog` — Dialog attribute descriptions (is_user, is_group, is_channel, date, unread_count, name, id) — WebFetch confirmed

### Tertiary (LOW confidence)
- Rate limit behavior when combining `from_user` + cursor pagination — not verified; flagged as open question

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all Phase 1 modules verified; Telethon API confirmed from installed source
- Architecture: HIGH — locked decisions in PROJECT.md; tool patterns derived from existing tools.py + Phase 1 modules
- Telethon `from_user` + cursor interaction: LOW — theoretical; needs live session to confirm
- Context window (±3) fetch strategy: HIGH — `min_id`/`max_id` semantics confirmed from iter_messages source

**Research date:** 2026-03-11
**Valid until:** 2026-04-11 (Telethon 1.x stable; all stdlib deps; rapidfuzz API stable)
