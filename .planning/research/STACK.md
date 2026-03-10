# Stack Research

**Domain:** MCP server for Telegram (Telethon-based, read-only, Python)
**Researched:** 2026-03-11
**Confidence:** HIGH (all APIs verified against official docs or PyPI)

## Recommended Stack

### Core Technologies (Existing — Keep)

| Technology | Version | Purpose | Why Recommended |
|------------|---------|---------|-----------------|
| Telethon | >=1.23.0 (stable: 1.42.0) | MTProto Telegram client | Only mature Python MTProto library; already in use |
| MCP SDK (`mcp`) | >=1.1.0 (current: 1.26.0) | MCP protocol implementation | Official SDK; low-level `Server` class already wired |
| Pydantic v2 | >=2.0.0 | Tool schema + validation | Already used for ToolArgs + model_json_schema() |
| pydantic-settings | >=2.6.0 | Env var config | Already used for TelegramSettings |
| Python | 3.11+ | Runtime | Required by pyproject.toml |

### New Dependencies (Add for This Milestone)

| Library | Version | Purpose | Why Recommended |
|---------|---------|---------|-----------------|
| rapidfuzz | >=3.0.0 (current: 3.14.3) | Fuzzy name matching | C extension, 10-100x faster than fuzzywuzzy; WRatio scorer built-in; no GPL license issues |
| transliterate | >=1.8.1 (latest: 1.8.1) | Cyrillic ↔ Latin | Bidirectional; language packs; `translit()` with `reversed=True` for Cyr→Latin |

### Supporting Libraries (Existing — No Changes)

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| xdg-base-dirs | >=6.0.0 | XDG session path | Session file placement |
| typer | >=0.15.0 | CLI entrypoint | `mcp-telegram` command |
| `base64` (stdlib) | stdlib | Cursor token encoding | Opaque pagination tokens |
| `json` (stdlib) | stdlib | Cursor token payload | Serialize message_id into token |

## API Reference: rapidfuzz

### Imports

```python
from rapidfuzz import fuzz, process, utils
```

### WRatio — direct comparison

```python
# Signature
fuzz.WRatio(s1: str, s2: str, *, processor=None, score_cutoff: float = 0) -> float

# Returns 0-100. Returns 0.0 if similarity < score_cutoff.
# processor=utils.default_process strips non-alphanumeric and lowercases.

score = fuzz.WRatio("Иван Петров", "Ivan Petrov", processor=utils.default_process)
```

WRatio internally combines `ratio`, `partial_ratio`, `token_sort_ratio`, and `token_set_ratio`, choosing the best-weighted result. It handles word-order swaps, partial matches, and case differences — making it correct for display names that may be reordered or abbreviated.

### process.extractOne — best match from a list

```python
# Signature
process.extractOne(
    query: str,
    choices,                     # list or dict of strings
    *,
    scorer=fuzz.WRatio,          # default
    processor=None,
    score_cutoff: float = None,  # returns None if no match meets threshold
    score_hint: float = None,
    scorer_kwargs=None,
) -> tuple[str, float, int] | None
# Returns (match, score, index) or None

result = process.extractOne(
    "Ваня",
    dialog_names,
    scorer=fuzz.WRatio,
    processor=utils.default_process,
    score_cutoff=60.0,
)
if result is None:
    # no match above threshold
```

### process.extract — ranked list

```python
# Signature
process.extract(
    query: str,
    choices,
    *,
    scorer=fuzz.WRatio,
    processor=None,
    limit: int = 5,
    score_cutoff: float = None,
    scorer_kwargs=None,
) -> list[tuple[str, float, int]]
# Returns list of (match, score, index) sorted by score descending

# Use for the ambiguity list (60-89 band) where multiple matches exist
candidates = process.extract(
    query,
    dialog_names,
    scorer=fuzz.WRatio,
    processor=utils.default_process,
    limit=5,
    score_cutoff=60.0,
)
```

### Threshold pattern (from PROJECT.md)

```python
THRESHOLD_AUTO   = 90   # single match → resolve automatically
THRESHOLD_AMBIG  = 60   # 60-89 → return ambiguity list to LLM
# below 60 → not found
```

## API Reference: transliterate

### Imports

```python
from transliterate import translit, get_available_language_codes
```

### translit() signature

```python
translit(value: str, language_code: str = None, reversed: bool = False) -> str
```

- `language_code='ru'` — Russian (Cyrillic)
- `reversed=False` — Latin → Cyrillic (default direction)
- `reversed=True` — Cyrillic → Latin

### Usage for fuzzy matching

The resolver needs to compare a user query (may be Latin) against dialog names (may be Cyrillic), and vice versa. Strategy: generate both transliterated variants and take the best WRatio score.

```python
# Latin query against Cyrillic names: transliterate query to Cyrillic first
query_as_cyr = translit(query, 'ru')         # "Ivan" → "Иван"

# Cyrillic query against Latin names: transliterate query to Latin
query_as_lat = translit(query, 'ru', reversed=True)  # "Иван" → "Ivan"
```

Important: omitting `language_code` when `reversed=True` lets the library auto-detect script. This produces cleaner output for auto-detection scenarios but is less predictable. Always pass `'ru'` explicitly for Russian when the language is known.

Available language codes: `get_available_language_codes()` — includes `'ru'`, `'uk'` (Ukrainian), `'bg'` (Bulgarian), `'el'`, `'hy'`, `'ka'`.

## API Reference: Telethon

### get_me()

```python
user: types.User = await client.get_me()

# Relevant attributes:
user.id            # int — Telegram user ID
user.username      # str | None — username without @
user.first_name    # str | None
user.last_name     # str | None
user.phone         # str | None
user.bot           # bool
```

Used for `GetMe` tool. Already used in `telegram.py` (line 39) for sign-in confirmation.

### get_entity()

```python
entity = await client.get_entity(entity_like)
# entity_like accepts: int ID, "@username", "t.me/username", PeerUser/PeerChat/PeerChannel, User/Chat/Channel objects
# Returns: types.User | types.Chat | types.Channel
```

Key constraint: `get_entity()` can only resolve IDs that are already in the session cache (encountered via iter_dialogs, get_participants, etc.). For ID-based lookup of unseen users, it will fail. For name/username resolution, it makes an API call.

### iter_dialogs() / get_dialogs()

```python
# Existing usage (tools.py line 89)
async for dialog in client.iter_dialogs(archived=bool, ignore_pinned=bool):
    dialog.id           # int
    dialog.name         # str — display name
    dialog.unread_count # int
    dialog.unread_mentions_count  # int
    dialog.date         # datetime — last message timestamp (use for last_message_at)
    dialog.entity       # User | Chat | Channel
    dialog.message      # last message object
```

`dialog.date` is the last message timestamp — this is the `last_message_at` field needed by the milestone. No additional API call required.

For dialog type detection, inspect `dialog.entity`:
```python
isinstance(dialog.entity, types.User)     # private chat
isinstance(dialog.entity, types.Chat)     # small group
isinstance(dialog.entity, types.Channel)  # channel or supergroup
# For Channel: dialog.entity.broadcast == True → channel, False → supergroup
```

### iter_messages() — cursor pagination

```python
async for msg in client.iter_messages(
    entity,
    limit=int,
    max_id=int,        # pagination: messages older than this ID
    min_id=int,        # pagination: messages newer than this ID
    reverse=bool,      # False = newest first (default)
    search=str,        # full-text search (server-side)
    filter=None,       # message filter type
):
    msg.id             # int — internal Telethon ID (used for cursor, not exposed to LLM)
    msg.date           # datetime
    msg.sender_id      # int
    msg.sender         # User | Chat | Channel (if resolved)
    msg.text           # str | None
    msg.reply_to       # MessageReplyHeader | None
    msg.reactions      # MessageReactions | None
    msg.media          # MessageMedia subtype | None
    msg.grouped_id     # int | None — album grouping
```

### GetFullUserRequest — for GetUserInfo tool

```python
from telethon.tl.functions.users import GetFullUserRequest

result = await client(GetFullUserRequest(user_entity))
# result is users.UserFull, containing:
result.full_user.about               # str | None — bio
result.full_user.common_chats_count  # int — shared groups count
result.full_user.id                  # int
# result.users[0] — the User object with basic fields
```

### GetCommonChatsRequest — for listing shared groups

```python
from telethon import functions

result = await client(functions.messages.GetCommonChatsRequest(
    user_id=user_entity,  # InputUser
    max_id=0,             # pagination offset (0 = start)
    limit=100,            # max results
))
# result is messages.Chats (Chats or ChatsSlice)
# result.chats — list of Chat/Channel objects
```

Note: `GetCommonChatsRequest` can only be called by non-bot users and raises `UserIdInvalidError` for invalid inputs.

### get_participants() — for name-based sender resolution in groups

```python
participants = await client.get_participants(
    entity,           # Chat or Channel
    limit=200,        # max results
    search="name",    # filter by name/username server-side
)
# Returns TotalList of User objects
# Use when resolving a sender name within a specific dialog
```

## API Reference: Cursor Pagination

The milestone hides numeric message IDs from LLM output. Pagination uses opaque base64 tokens that encode the internal `message_id` as a JSON payload.

### Encoding

```python
import base64, json

def encode_cursor(message_id: int) -> str:
    payload = json.dumps({"id": message_id}).encode()
    return base64.urlsafe_b64encode(payload).decode()
```

Use `urlsafe_b64encode` (replaces `+`/`/` with `-`/`_`) to avoid issues in JSON tool arguments.

### Decoding

```python
def decode_cursor(cursor: str) -> int:
    payload = base64.urlsafe_b64decode(cursor.encode())
    return json.loads(payload)["id"]
```

### Why opaque

- LLM receives a `next_cursor` string it cannot interpret
- Server decodes it to `max_id` for `iter_messages(max_id=decoded_id)`
- Decouples the wire format from Telegram's internal IDs
- Allows future format changes without breaking tool interface

### Cursor field in tool output

Add `next_cursor: str | None` to the tool response. When the page is full (returned `limit` messages), emit a cursor. When fewer than `limit` returned, emit `null` — no next page.

## API Reference: MCP SDK Transport

The existing server uses the low-level `mcp.server.Server` class (not FastMCP). This matters for transport.

### Current stdio wiring (server.py)

```python
from mcp.server.stdio import stdio_server

async with stdio_server() as (read_stream, write_stream):
    await app.run(read_stream, write_stream, app.create_initialization_options())
```

This must remain intact (PROJECT.md constraint: stdio transport must stay working).

### Streamable-HTTP with low-level Server

The low-level `Server` class does not have a `.run(transport="streamable-http")` shortcut — that shortcut is FastMCP-only. For low-level servers, use `StreamableHTTPSessionManager`:

```python
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

session_manager = StreamableHTTPSessionManager(
    app=app,
    event_store=None,
    json_response=True,
    stateless=True,
)
# Then mount as ASGI app or run standalone with uvicorn
```

**Current project decision (PROJECT.md):** Native HTTP/SSE is out of scope for this milestone. mcp-proxy handles HTTP/SSE externally. stdio stays as-is. Do not add streamable-http to this milestone.

## Installation

```bash
# Add to pyproject.toml dependencies:
# "rapidfuzz>=3.0.0",
# "transliterate>=1.8.1",

# Install locally
pip install rapidfuzz transliterate

# Or with uv
uv add rapidfuzz transliterate
```

## Alternatives Considered

| Recommended | Alternative | When to Use Alternative |
|-------------|-------------|-------------------------|
| rapidfuzz WRatio | fuzzywuzzy | Never — fuzzywuzzy is GPL-licensed and requires python-Levenshtein; rapidfuzz is MIT and 10-100x faster |
| rapidfuzz WRatio | thefuzz | Never — thefuzz is the renamed fuzzywuzzy; same problems |
| transliterate | cyrtranslit | If needing non-Russian Cyrillic scripts (Serbian, Bulgarian, Macedonian) — cyrtranslit has better coverage for some languages |
| transliterate | Unidecode | If needing general Unicode→ASCII (not bidirectional) — Unidecode is one-way only, useless for Latin→Cyrillic |
| base64 urlsafe | raw message_id | Never in new format — message_id must not be exposed to LLM per PROJECT.md |
| stdlib base64+json | third-party cursor libs | Never — stdlib is sufficient, no extra dep needed |
| low-level Server | FastMCP | If starting a new server from scratch — FastMCP is higher-level; migrating existing low-level server is not worthwhile for this milestone |

## What NOT to Use

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `fuzz.ratio` or `fuzz.partial_ratio` alone | Misses word-order variants (e.g., "Petrov Ivan" vs "Ivan Petrov") | `fuzz.WRatio` which combines multiple scorers |
| `process.extract` for single best match | Returns list; more expensive than needed when only best match wanted | `process.extractOne` |
| `translit(text, reversed=True)` without language code | Auto-detection can produce stray characters in some cases | Always pass `'ru'` when language is known |
| `get_entity()` with arbitrary int IDs | Fails if ID not in session cache | Resolve via `iter_dialogs()` first, then use the cached entity |
| `client.get_dialogs()` (non-iterator) | Loads all dialogs into memory at once | `client.iter_dialogs()` which streams |
| Exposing `message.id` in tool output | Violates PROJECT.md design: LLM should not see raw IDs | Encode into opaque base64 cursor |
| `base64.b64encode` | `+` and `/` in output can cause JSON escaping issues | `base64.urlsafe_b64encode` |
| Migrating to FastMCP for this milestone | Requires rewriting server.py wiring with no benefit | Keep low-level `mcp.server.Server` pattern |

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| rapidfuzz 3.x | Python 3.8+ | v3.0 broke API from v2.x (scorer import paths changed); use `from rapidfuzz import fuzz, process, utils` not `from rapidfuzz.fuzz import ...` for process module |
| transliterate 1.8.1 | Python 3.x | Last release 2019; stable; no active development but no known issues |
| mcp 1.26.0 | Python 3.10+ | Low-level Server API unchanged since 1.1.0; stdio_server still works |
| Telethon 1.42.0 | Python 3.8+ | `GetFullUserRequest` is in `telethon.tl.functions.users`; `GetCommonChatsRequest` is in `telethon.tl.functions.messages` (accessed via `telethon.functions` shorthand) |

## Sources

- https://rapidfuzz.github.io/RapidFuzz/Usage/fuzz.html — WRatio signature, processor, score_cutoff (HIGH confidence)
- https://rapidfuzz.github.io/RapidFuzz/Usage/process.html — extractOne/extract signatures (HIGH confidence, version 3.14.3)
- https://pypi.org/project/mcp/ — version 1.26.0, transport options (HIGH confidence)
- https://docs.telethon.dev/en/stable/modules/client.html — get_me, get_entity, iter_dialogs, get_participants signatures (HIGH confidence, v1.42.0)
- https://tl.telethon.dev/methods/users/get_full_user.html — GetFullUserRequest return type (HIGH confidence)
- https://tl.telethon.dev/constructors/user_full.html — UserFull.about, UserFull.common_chats_count (HIGH confidence)
- https://tl.telethon.dev/methods/messages/get_common_chats.html — GetCommonChatsRequest parameters (HIGH confidence)
- https://transliterate.readthedocs.io/en/1.8.1/ — translit() signature, reversed parameter behavior (HIGH confidence)
- Existing codebase: server.py, tools.py, telegram.py — current wiring confirmed by direct read (HIGH confidence)

---
*Stack research for: mcp-telegram milestone — fuzzy resolution, unified format, cursor pagination, GetMe/GetUserInfo*
*Researched: 2026-03-11*
