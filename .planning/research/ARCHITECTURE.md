# Architecture Research

**Domain:** MCP server refactoring — Telethon + singledispatch tool bridge
**Researched:** 2026-03-11
**Confidence:** HIGH (direct codebase analysis, no speculation required)

## Standard Architecture

### System Overview (Current)

```
┌──────────────────────────────────────────────────────────────┐
│                   MCP Protocol Layer                          │
│   server.py: app.call_tool() → mapping dict → tool_runner()  │
├──────────────────────────────────────────────────────────────┤
│                   Tool Layer (tools.py)                       │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐   │
│  │ ListDialogs  │  │  ListMessages │  │  SearchMessages  │   │
│  │ + runner     │  │  + runner     │  │  + runner        │   │
│  └──────────────┘  └───────────────┘  └──────────────────┘   │
│  ┌──────────────┐  ┌───────────────┐                         │
│  │  GetMessage  │  │   GetDialog   │  (to be removed)        │
│  │  + runner    │  │   + runner    │                         │
│  └──────────────┘  └───────────────┘                         │
├──────────────────────────────────────────────────────────────┤
│                   Telegram Layer (telegram.py)                │
│   create_client() @cache → TelegramClient singleton          │
│   TelegramSettings (Pydantic) → env vars                     │
└──────────────────────────────────────────────────────────────┘
```

### System Overview (Target — after refactoring)

```
┌──────────────────────────────────────────────────────────────┐
│                   MCP Protocol Layer                          │
│   server.py: unchanged — still routes via mapping + dispatch  │
├──────────────────────────────────────────────────────────────┤
│                   Tool Layer (tools.py)                       │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────────┐   │
│  │ ListDialogs  │  │  ListMessages │  │  SearchMessages  │   │
│  │ + runner     │  │  + runner     │  │  + runner        │   │
│  └──────┬───────┘  └───────┬───────┘  └────────┬─────────┘   │
│         │                  │                   │             │
│  ┌──────────────┐  ┌───────────────┐            │             │
│  │   GetMe      │  │  GetUserInfo  │            │             │
│  │   + runner   │  │  + runner     │            │             │
│  └──────┬───────┘  └───────┬───────┘            │             │
│         └──────────────────┴────────────────────┘             │
│                            │ calls                            │
├────────────────────────────▼─────────────────────────────────┤
│              Support Modules (new files)                      │
│  ┌───────────────┐  ┌────────────────┐  ┌──────────────────┐  │
│  │ resolver.py   │  │  formatter.py  │  │  pagination.py   │  │
│  │ (name→entity) │  │  (msg format)  │  │  (cursor tokens) │  │
│  └───────┬───────┘  └────────────────┘  └──────────────────┘  │
│          │                                                    │
├──────────▼───────────────────────────────────────────────────┤
│                   Telegram Layer (telegram.py)                │
│   create_client() @cache → TelegramClient singleton          │
│   TelegramSettings (Pydantic) → env vars                     │
└──────────────────────────────────────────────────────────────┘
```

### Component Responsibilities

| Component | Responsibility | Location |
|-----------|----------------|----------|
| `server.py` | MCP protocol: list_tools, call_tool dispatch | existing, no changes |
| `tools.py` | ToolArgs classes + @tool_runner.register handlers | existing, evolves |
| `telegram.py` | TelegramClient factory + auth flows | existing, no changes |
| `resolver.py` | Name → entity_id resolution with fuzzy match | NEW |
| `formatter.py` | Message → chat-log string conversion | NEW |
| `pagination.py` | Cursor token encode/decode | NEW |

---

## Component Design

### resolver.py — Name Resolution

**Where it lives:** `src/mcp_telegram/resolver.py` — separate module, not inline.

**Rationale for separate module:**
- Used by at least three tools: `ListMessages`, `SearchMessages`, `GetUserInfo`
- Has its own dependencies (`rapidfuzz`, `transliterate`)
- Contains state (dialog cache); isolating it prevents `tools.py` from becoming aware of caching concerns
- Testable in isolation without an MCP server

**Interface:**

```python
async def resolve_entity(
    client: TelegramClient,
    query: str | int,
) -> ResolveResult:
    ...

@dataclass
class ResolveResult:
    entity_id: int          # resolved Telegram entity ID
    display_name: str       # canonical name for annotation
    ambiguous: list[str]    # non-empty when 60–89 WRatio match → caller raises
    annotation: str         # "[резолв: "query" → Name, id:N]" or ""
```

**Algorithm:**

```
if query is int:
    return ResolveResult(entity_id=query, display_name=str(query), annotation="")

normalize(query):
    lowercase, strip, transliterate Cyrillic→Latin for comparison

candidates = await _get_dialog_cache(client)   # list[(name, entity_id)]

for each candidate:
    score = WRatio(normalize(query), normalize(candidate.name))

if max_score >= 90: auto-resolve → ResolveResult with annotation
if 60 <= max_score < 90: return with non-empty ambiguous list
if max_score < 60: raise ValueError("not found")
```

**Dialog cache design — how to avoid stale data:**

Use a module-level dict keyed on the TelegramClient instance (identity, not value), storing `(timestamp, list[DialogEntry])`. TTL = 5 minutes. On cache miss or TTL expiry, call `client.iter_dialogs()` to refresh.

Do NOT use `@functools.cache` for this — it cannot invalidate by time. Use an explicit dict with timestamps.

```python
_dialog_cache: dict[int, tuple[float, list[DialogEntry]]] = {}  # id(client) → (ts, entries)
CACHE_TTL = 300  # seconds

async def _get_dialog_cache(client: TelegramClient) -> list[DialogEntry]:
    key = id(client)
    now = time.monotonic()
    if key in _dialog_cache:
        ts, entries = _dialog_cache[key]
        if now - ts < CACHE_TTL:
            return entries
    entries = [DialogEntry(name=d.name, entity_id=d.id) async for d in client.iter_dialogs()]
    _dialog_cache[key] = (now, entries)
    return entries
```

Stale-data tradeoff: 5 min TTL means a newly added contact may not resolve for up to 5 min. Acceptable — the alternative (always fetching) adds 1-2s latency per tool call. LLM sessions are short; this TTL covers a full session.

**Integration with tools.py:**

Each tool runner that accepts a `dialog_id: str | int` field calls `resolve_entity()` at the top of its handler, before any other Telegram API call. The annotation is prepended to the tool output:

```python
@tool_runner.register
async def list_messages(args: ListMessages):
    async with create_client() as client:
        result = await resolve_entity(client, args.dialog_id)
        if result.ambiguous:
            return [TextContent(type="text", text=f"Ambiguous: {result.ambiguous}")]
        # ... rest of handler
        output = [result.annotation] + message_lines  # annotation first
```

---

### formatter.py — Message Formatting

**Where it lives:** `src/mcp_telegram/formatter.py` — standalone module of pure functions.

**Rationale for standalone functions, not a class:**
- No persistent state needed between messages
- Functions compose better for the context-window feature (`SearchMessages` needs ±3 context messages formatted the same way)
- A class would add ceremony without benefit

**Interface:**

```python
def format_message(
    message: custom.Message,
    prev_message: custom.Message | None = None,
) -> list[str]:
    """Returns zero or more lines to emit (day header, session break, message line)."""

def format_message_line(message: custom.Message) -> str:
    """HH:mm FirstName: text  [reactions]"""

def format_day_header(date: datetime) -> str:
    """─── 11 March 2026 ───"""

def format_session_break() -> str:
    """--- (60+ min gap) ---"""
```

**format_message logic:**

```
if prev_message is None or message.date.date() != prev_message.date.date():
    emit day header

if prev_message is not None and gap > 60 min:
    emit session break

emit message line:
    sender = message.sender.first_name or username or "Unknown"
    text = message.text or describe_media(message)
    reactions = format_reactions(message.reactions)
    reply_annotation = "[reply to HH:mm FirstName]" if message.reply_to else ""
```

**Caller pattern (ListMessages tool runner):**

```python
lines = []
prev = None
async for message in client.iter_messages(...):
    lines.extend(format_message(message, prev))
    prev = message
return [TextContent(type="text", text="\n".join(lines))]
```

Returning a single `TextContent` with newline-joined lines is more efficient than one `TextContent` per message. The LLM reads it as a chat log, not a list of items.

---

### pagination.py — Cursor Tokens

**Where it lives:** `src/mcp_telegram/pagination.py` — standalone module.

**Design: base64-encoded JSON, not a hash.**

Rationale:
- Cursor must be decodable by the server without external state (no database, no session store)
- The token encodes the anchor message_id internally so it never appears in tool output
- JSON is inspectable during debugging; base64 makes it opaque to the LLM
- A hash would require a server-side lookup table — adds state the project cannot afford

**Token structure:**

```python
@dataclass
class CursorPayload:
    dialog_id: int     # guard: prevent cursor from being used with wrong dialog
    message_id: int    # the Telethon max_id anchor for next page
    direction: str     # "older" (always, for now — pagination goes backwards)

def encode_cursor(dialog_id: int, message_id: int) -> str:
    payload = {"d": dialog_id, "m": message_id, "dir": "older"}
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()

def decode_cursor(token: str, expected_dialog_id: int) -> int:
    """Returns message_id to use as max_id. Raises ValueError on mismatch or corruption."""
    payload = json.loads(base64.urlsafe_b64decode(token.encode()))
    if payload["d"] != expected_dialog_id:
        raise ValueError("cursor belongs to a different dialog")
    return payload["m"]
```

**Integration with ListMessages:**

`ListMessages` field changes:
- Remove: `before_id: int | None` (exposes internal IDs)
- Add: `cursor: str | None = None` (opaque token)

Runner decodes cursor if present:
```python
max_id = decode_cursor(args.cursor, resolved_dialog_id) if args.cursor else None
```

The tool response includes the next cursor token as the last line:
```
cursor:eyJkIjogMTIzLCAibSI6IDQ1NiwgImRpciI6ICJvbGRlciJ9
```

The LLM passes this value verbatim as `cursor` in the next call. It never knows the underlying message ID.

---

### GetMe and GetUserInfo — Fitting the singledispatch Pattern

**Answer: they fit perfectly, no pattern changes needed.**

The `server.py` tool discovery loop (`inspect.getmembers(tools, inspect.isclass)` filtering for `ToolArgs` subclasses) automatically picks up any new class added to `tools.py`. No registration step in `server.py` is required.

**GetMe:**

```python
class GetMe(ToolArgs):
    """Return the authenticated user's own name, id, and username."""
    pass  # no arguments

@tool_runner.register
async def get_me(args: GetMe) -> ...:
    async with create_client() as client:
        me = await client.get_me()
        # format and return
```

**GetUserInfo:**

```python
class GetUserInfo(ToolArgs):
    """Return profile and common chats for a user."""
    user: str | int  # name or id

@tool_runner.register
async def get_user_info(args: GetUserInfo) -> ...:
    async with create_client() as client:
        result = await resolve_entity(client, args.user)
        # fetch profile, common chats, format
```

The `str | int` union type in Pydantic v2 works correctly: the JSON schema emits `anyOf: [string, integer]`, and the MCP client can pass either. Pydantic validates on instantiation.

---

## Recommended Project Structure

```
src/mcp_telegram/
├── __init__.py          # CLI entry points — no changes
├── server.py            # MCP protocol layer — no changes
├── telegram.py          # TelegramClient factory — no changes
├── tools.py             # ToolArgs classes + runners — evolves
├── resolver.py          # NEW: name → entity_id
├── formatter.py         # NEW: message → chat-log lines
└── pagination.py        # NEW: cursor token encode/decode
```

**Structure rationale:**
- All new files are in the same package — no import path changes anywhere
- Flat structure matches the existing convention (no subpackages)
- Each new file has a single responsibility and no circular imports
- `tools.py` imports from `resolver`, `formatter`, `pagination` — dependency direction is one-way

---

## Data Flow

### Name Resolution Path

```
LLM call: ListMessages(dialog_id="Иван Петров", cursor=None)
    │
    ▼
tools.py: list_messages(args: ListMessages)
    │
    ├──► resolver.resolve_entity(client, "Иван Петров")
    │        │
    │        ├──► _get_dialog_cache(client)
    │        │        ├── cache hit (< 5 min): return cached list
    │        │        └── cache miss: client.iter_dialogs() → store → return
    │        │
    │        ├──► transliterate("Иван Петров") → "Ivan Petrov"
    │        ├──► WRatio score each candidate
    │        └──► score=95 → ResolveResult(entity_id=123, annotation="[резолв: ...]")
    │
    ├──► pagination.decode_cursor(args.cursor, 123) → None (no cursor)
    │
    ├──► client.iter_messages(entity=123, limit=50)
    │
    ├──► formatter.format_message(msg, prev) for each message
    │
    └──► return [TextContent("[резолв: ...]\n" + chat_log + "\ncursor:TOKEN")]
```

### Cursor Pagination Path

```
First call: ListMessages(dialog_id="Иван Петров", cursor=None)
    → returns 50 messages + "cursor:TOKEN_A" as last line

Second call: ListMessages(dialog_id="Иван Петров", cursor="TOKEN_A")
    │
    ├──► resolve_entity → entity_id=123 (cache hit)
    ├──► decode_cursor("TOKEN_A", 123) → message_id=456
    ├──► client.iter_messages(entity=123, max_id=456, limit=50)
    └──► returns next 50 messages + "cursor:TOKEN_B" or no cursor (end of history)
```

---

## Build Order

The three new modules have no dependencies on each other. The build order is driven by tool runner changes:

```
1. pagination.py       — pure functions, no Telethon dependency
                         Build first: can be tested with unit tests immediately

2. formatter.py        — depends on Telethon message types (read-only)
                         Build second: can be tested with mock messages

3. resolver.py         — depends on Telethon client (async I/O)
                         Build third: requires integration test with real/fake client

4. tools.py updates    — depends on all three above
   ├── Update ListDialogs (add type, last_message_at fields)
   ├── Update ListMessages (str|int dialog, cursor pagination, sender filter)
   ├── Update SearchMessages (str|int dialog, ±3 context, session grouping)
   ├── Add GetMe
   ├── Add GetUserInfo
   ├── Remove GetDialog class + runner
   └── Remove GetMessage class + runner

5. Validation          — ensure server.py mapping reflects removed/added tools correctly
                         (automatic via reflection, but test the MCP tool list)
```

**What must exist before what:**
- `pagination.py` must exist before updating `ListMessages`
- `formatter.py` must exist before updating any message-returning tool
- `resolver.py` must exist before updating any tool that accepts `str | int` dialog/user
- All three support modules must exist before removing `GetDialog` and `GetMessage` (which are the current workarounds for ID-only access)

---

## Architectural Patterns

### Pattern 1: Tool Runner Stays Thin

**What:** Each `@tool_runner.register` function orchestrates calls to support modules but contains no business logic itself.

**When to use:** Always — this is the discipline that keeps `tools.py` readable as it grows.

**Trade-offs:** Slightly more indirection; worth it because each module is independently testable.

**Example:**
```python
@tool_runner.register
async def list_messages(args: ListMessages):
    async with create_client() as client:
        result = await resolve_entity(client, args.dialog_id)   # resolver
        if result.ambiguous:
            return [TextContent(type="text", text=_fmt_ambiguous(result.ambiguous))]
        max_id = decode_cursor(args.cursor, result.entity_id) if args.cursor else None   # pagination
        lines, next_cursor = [], None
        prev = None
        async for msg in client.iter_messages(result.entity_id, limit=args.limit, max_id=max_id):
            lines.extend(format_message(msg, prev))   # formatter
            prev = msg
        if prev:
            next_cursor = encode_cursor(result.entity_id, prev.id)
        body = "\n".join(lines)
        if next_cursor:
            body += f"\ncursor:{next_cursor}"
        return [TextContent(type="text", text=result.annotation + "\n" + body)]
```

### Pattern 2: Annotation Prepend

**What:** Resolution annotations are prepended to tool output as the first line, not returned as a separate `TextContent` item.

**When to use:** Always, when name resolution occurs.

**Trade-offs:** LLM reads annotation as part of the response narrative, not metadata — but this is desirable. The LLM needs to see what was matched to trust the result.

### Pattern 3: Single TextContent Per Tool Response

**What:** Return one `TextContent` item with newline-joined content rather than one item per message.

**When to use:** ListMessages, SearchMessages — any tool returning multiple messages.

**Trade-offs:** Slightly harder to parse programmatically, but the MCP spec is text-first; LLMs read structured text natively. Also reduces MCP protocol overhead.

---

## Anti-Patterns

### Anti-Pattern 1: Logic Inside ToolArgs Classes

**What people do:** Add methods to `ListMessages` or other ToolArgs classes that perform Telegram API calls or business logic.

**Why it's wrong:** ToolArgs is a data contract (Pydantic model). Adding behavior breaks the schema generation path and makes classes untestable without a live Telegram client.

**Do this instead:** Keep ToolArgs as pure Pydantic models. All logic goes in the `@tool_runner.register` function or support modules.

### Anti-Pattern 2: Per-Call Dialog Fetching in Resolver

**What people do:** Call `client.iter_dialogs()` on every `resolve_entity()` invocation.

**Why it's wrong:** `iter_dialogs()` fetches all dialogs from Telegram API — 100-1000+ entries. At 1-2s per call, this adds unacceptable latency to every tool invocation that uses name resolution.

**Do this instead:** Use the TTL cache described in the resolver design. 5 minutes is long enough for a session, short enough to pick up new contacts.

### Anti-Pattern 3: Exposing message_id in Tool Output

**What people do:** Include `[id=12345]` in formatted message output "for pagination convenience."

**Why it's wrong:** The LLM will pass it back as a literal integer in fields expecting cursor tokens, bypassing the opaque token design. IDs from one dialog can be accidentally applied to another.

**Do this instead:** Never emit numeric message IDs in user-facing output. All pagination uses opaque cursor tokens. Cursor tokens embed the dialog guard internally.

### Anti-Pattern 4: Storing Dialog Cache in tools.py

**What people do:** Put `_dialog_cache = {}` as a module-level variable in `tools.py`.

**Why it's wrong:** `tools.py` is already large. Adding cache state to it makes the module responsible for too many concerns. Cache invalidation bugs become harder to find.

**Do this instead:** Cache state lives in `resolver.py`, co-located with the code that uses it.

---

## Integration Points

### Internal Boundaries

| Boundary | Communication | Notes |
|----------|---------------|-------|
| `tools.py` → `resolver.py` | Direct async function call | resolver takes `TelegramClient` instance, not a factory |
| `tools.py` → `formatter.py` | Direct sync function call | formatter is pure functions over message objects |
| `tools.py` → `pagination.py` | Direct sync function call | encode/decode are pure, no I/O |
| `tools.py` → `telegram.py` | `create_client()` factory (unchanged) | still `async with create_client() as client` |
| `server.py` → `tools.py` | unchanged — reflection + dispatch | no server.py changes required |

### Telethon Entity Types in formatter.py

The formatter receives `custom.Message` objects. Accessing sender name requires `message.sender` which may be `None` for service messages or anonymized senders in channels. formatter.py must handle:

- `message.sender` is `None` → use "Channel" or "Unknown"
- `message.sender` is `types.User` → `first_name` (may also be None)
- `message.sender` is `types.Channel` → `title`
- `message.text` is empty → describe media type: "(photo)", "(document)", "(sticker)", etc.

---

## Scaling Considerations

This is a single-user MCP server — scaling in the traditional sense is not relevant. The relevant concern is latency per tool call:

| Concern | Current | After Refactoring |
|---------|---------|-------------------|
| Dialog list fetch | Not needed | 1-2s on first call per session, ~0ms cached |
| Name resolution | Not applicable | ~1ms WRatio over cached list |
| Message formatting | ~0ms (minimal format) | ~1ms per 50 messages |
| Cursor decode | Not applicable | ~0.1ms |
| Overall ListMessages latency | ~500ms (Telegram API) | ~500ms + ~2ms overhead first call, ~502ms cached |

The dominant cost is always the Telegram MTProto API round-trip. All new components add negligible overhead.

---

## Sources

- Direct codebase analysis: `src/mcp_telegram/tools.py`, `server.py`, `telegram.py`
- `.planning/PROJECT.md`: requirements, algorithm specifications, format decisions
- `.planning/codebase/ARCHITECTURE.md`: existing layer analysis
- Telethon docs: `iter_dialogs()`, `iter_messages()`, `custom.Message` attributes — behavioral expectations based on current codebase usage patterns (HIGH confidence from existing working code)
- rapidfuzz WRatio: deterministic scorer, no external state required (HIGH confidence from PROJECT.md specification)

---

*Architecture research for: mcp-telegram refactoring — name resolution, formatting, cursor pagination*
*Researched: 2026-03-11*
