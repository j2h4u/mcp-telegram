# Phase 5: Cache & Error Hardening - Research

**Researched:** 2026-03-11
**Domain:** SQLite TTL enforcement, entity upsert in search flow, cursor error handling in tools.py
**Confidence:** HIGH

## Summary

Phase 5 closes three tech debt items identified in the v1.0 audit. All three are small, surgical
changes to existing code with zero new dependencies. The codebase is fully understood from
reading the source files directly.

**CACH-01 (TTL enforcement):** The `EntityCache.get()` method already implements TTL logic — it
returns `None` if `time.time() - updated_at > ttl_seconds`. The gap is that `list_messages` calls
`resolve(args.dialog, cache.all_names())` but `all_names()` is documented as "no TTL filtering —
caller decides." The resolver never applies TTL; it passes all cached names to rapidfuzz regardless
of age. The fix is to call `cache.get(entity_id, ttl)` during resolution or to add a
`all_names_with_ttl(ttl_seconds)` method that filters before returning. The TTL values per
requirement: 30 days for users (`2_592_000` seconds), 7 days for groups/channels (`604_800`
seconds). Currently, `all_names()` returns `{entity_id: name}` — a TTL-aware version needs to
filter by type and apply the correct TTL per entity type. The `entities` table has `type` and
`updated_at` columns available for this query.

**CACH-02 (search entity upsert):** `search_messages` fetches hits via `iter_messages(search=...)`
but never upserts sender entities from those messages into the cache. `list_messages` already has
the correct upsert loop (lines 228-241 in tools.py). The fix is to add the same loop to
`search_messages` after the `hits` list is assembled. The context messages from `get_messages` can
also be upserted, but the requirement specifically says "sender entities" from search results —
hits are sufficient to satisfy CACH-02.

**TOOL-03 (cursor error handling):** `decode_cursor` raises `ValueError` on cross-dialog cursor
and will raise `json.JSONDecodeError` or `binascii.Error` on a malformed/corrupt base64 token.
In `list_messages`, the call `iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)` is
not wrapped in any try/except. A bad cursor propagates as an unhandled exception up through the
MCP framework, resulting in a generic RuntimeError visible to the LLM. The fix is to wrap
`decode_cursor` in a try/except that catches `(ValueError, Exception)` and returns a
`TextContent` with a readable message like `"Invalid cursor: <reason>"`.

The test baseline is **52** tests (confirmed by running `pytest tests/ -q`). All 52 must remain
green. New failing stubs in Wave 0 will bring the count higher; Wave 1 makes them pass.

**Primary recommendation:** Three independent, surgical changes — add TTL-filtered `all_names`
variant to `EntityCache`, add sender upsert loop to `search_messages`, wrap `decode_cursor` call
in `list_messages` with a try/except returning user-readable error.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CACH-01 | Entity metadata persisted in SQLite; TTL 30d users, 7d groups/channels; stale entries not returned during resolution | `EntityCache.get()` already implements TTL comparison; gap is `all_names()` ignores TTL. Fix: add `all_names_ttl()` method that queries with per-type TTL filters, or add a `filtered_names(user_ttl, group_ttl)` method. Resolver caller in `list_messages`, `search_messages`, `get_user_info` must switch to TTL-filtered variant. |
| CACH-02 | Cache populated lazily from API responses; upsert on every entity-bearing response | `list_messages` already upserts sender entities correctly. Gap: `search_messages` has no upsert loop. Fix: copy the existing upsert loop from `list_messages` lines 228-241 into `search_messages` after hits are assembled. |
| TOOL-03 | `ListMessages` cursor pagination; cursor errors return friendly messages | `decode_cursor` raises `ValueError` (cross-dialog) or decoding exceptions (malformed). Currently unwrapped in `list_messages`. Fix: wrap with try/except and return `TextContent` with human-readable error. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| sqlite3 | stdlib | EntityCache storage | Already the cache backend |
| pytest + pytest-asyncio | ≥9.0.2 / ≥1.3.0 | Test framework | Already installed, asyncio_mode=auto |
| unittest.mock AsyncMock/MagicMock | stdlib | Mock Telethon client in tests | Consistent with all existing test patterns |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| time | stdlib | TTL calculation in EntityCache | Already imported in cache.py |

**Installation:** No new packages required.

## Architecture Patterns

### Recommended Project Structure

No new files. All changes are confined to:
- `src/mcp_telegram/cache.py` — new TTL-filtered `all_names` variant
- `src/mcp_telegram/tools.py` — sender upsert in `search_messages`, cursor error wrap in `list_messages`
- `tests/test_cache.py` — new TTL test(s) for the new method
- `tests/test_tools.py` — new tests for search upsert and cursor error message

### Pattern 1: TTL-filtered all_names method on EntityCache

**What:** A new `EntityCache` method that returns only non-stale entities, applying different TTLs
per entity type.

**When to use:** Called by `list_messages`, `search_messages`, `get_user_info` instead of `all_names()`.

**Design decision — method signature options:**

Option A — single method with hardcoded project TTLs:
```python
def all_names_fresh(self) -> dict[int, str]:
    """Return {id: name} for entities within their type-specific TTL.
    Users: 30d (2_592_000s), groups/channels: 7d (604_800s).
    """
    now = int(time.time())
    rows = self._conn.execute(
        """SELECT id, name FROM entities
           WHERE (type = 'user' AND updated_at >= ?)
              OR (type != 'user' AND updated_at >= ?)""",
        (now - 2_592_000, now - 604_800),
    ).fetchall()
    return {row[0]: row[1] for row in rows}
```

Option B — parameterized TTLs (more testable):
```python
def all_names_with_ttl(self, user_ttl: int, group_ttl: int) -> dict[int, str]:
    now = int(time.time())
    rows = self._conn.execute(
        """SELECT id, name FROM entities
           WHERE (type = 'user' AND updated_at >= ?)
              OR (type != 'user' AND updated_at >= ?)""",
        (now - user_ttl, now - group_ttl),
    ).fetchall()
    return {row[0]: row[1] for row in rows}
```

**Recommendation:** Option B is more testable (test can pass short TTLs). Define the project
constants `USER_TTL = 2_592_000` and `GROUP_TTL = 604_800` at module level in `tools.py` or
`cache.py` for use at call sites.

**Callers to update:**
- `list_messages`: `resolve(args.dialog, cache.all_names())` → `resolve(args.dialog, cache.all_names_with_ttl(USER_TTL, GROUP_TTL))`
- Same for sender resolution in `list_messages`
- `search_messages`: dialog resolution
- `get_user_info`: user resolution

**Important:** `all_names()` (without TTL) is still called internally by resolver tests and
`list_dialogs` (which populates the cache on every run). Do not remove `all_names()`.

### Pattern 2: Sender upsert in search_messages

**What:** After fetching `hits`, iterate and upsert each message's sender entity.

**When to use:** Immediately after the `hits` list is assembled in `search_messages`.

**Implementation — copy from list_messages lines 228-241:**
```python
# Source: tools.py lines 228-241 (list_messages)
for msg in hits:
    sender = getattr(msg, "sender", None)
    if sender is not None:
        sender_name = " ".join(
            filter(None, [
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ])
        ) or getattr(sender, "title", "") or str(msg.sender_id)
        sender_type = "user" if getattr(sender, "first_name", None) else "group"
        cache.upsert(
            msg.sender_id, sender_type, sender_name,
            getattr(sender, "username", None)
        )
```

**Note:** This must be inside the `async with connected_client() as client:` block since `hits`
is assembled there. Position after hits assembly, before reaction names loop.

### Pattern 3: Cursor error handling in list_messages

**What:** Wrap `decode_cursor` call in a try/except to return a readable error instead of
propagating a generic RuntimeError.

**When to use:** The `args.cursor` branch in `list_messages` (Step 2, around line 205).

**Current code (tools.py ~line 204-205):**
```python
if args.cursor:
    iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)
```

**Fixed code:**
```python
if args.cursor:
    try:
        iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)
    except (ValueError, Exception) as exc:
        return [TextContent(type="text", text=f"Invalid cursor: {exc}")]
```

**Or more targeted** (recommended — only catches expected exceptions):
```python
if args.cursor:
    try:
        iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)
    except Exception as exc:
        return [TextContent(type="text", text=f"Invalid cursor: {exc}")]
```

The `ValueError` from cross-dialog cursor is the expected case. `json.JSONDecodeError` and
`binascii.Error` are subclasses of `ValueError` and `Exception` respectively. Catching `Exception`
is fine here — the function returns immediately, so no resource leaks.

**Important:** This early return must happen **before** `async with connected_client()` opens a
connection. Looking at `list_messages`, the cursor decode happens at line ~205 (Step 2, before
`async with connected_client()`), so no connection is open yet when the error fires. The fix
preserves this property.

### Anti-Patterns to Avoid

- **Removing `all_names()` method:** It is still needed; `list_dialogs` and tests use it.
  Add a new method, do not replace.
- **Upsert from context messages in search:** The requirement says "sender entities" from search
  results (hits). Context messages are optional. Keep it simple — upsert hits only.
- **Catching Exception broadly and swallowing it silently:** Return a TextContent with the error
  message. The LLM needs to see why the cursor was rejected.
- **Placing cursor error check after `async with connected_client()`:** That would open a
  network connection unnecessarily. The check is already before the client block; keep it there.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| TTL-aware name lookup | Custom age check in tools.py | New `EntityCache.all_names_with_ttl()` method | Keeps TTL logic inside the cache class where it belongs; testable in isolation |
| Sender entity upsert in search | New upsert mechanism | Copy existing upsert loop from `list_messages` | Identical data shape, identical Telethon attributes |
| Decode error message formatting | Custom error serialization | `f"Invalid cursor: {exc}"` | Exception message from `decode_cursor` is already descriptive |

## Common Pitfalls

### Pitfall 1: all_names() TTL query uses wrong column comparison
**What goes wrong:** Query `updated_at >= now - ttl` should use `>=` not `>`. With `>`, entries
inserted exactly at the boundary are excluded (edge case, but correct semantics require `>=`).
**Why it happens:** Off-by-one in timestamp comparison.
**How to avoid:** Use `updated_at >= now - ttl_seconds`.
**Warning signs:** Test with TTL equal to exact age fails unexpectedly.

### Pitfall 2: Monkeypatching time in cache tests requires module-level attribute
**What goes wrong:** `monkeypatch.setattr(cache_module, "time", ...)` patches the `time` module
reference in `mcp_telegram.cache`. If `cache.py` used `from time import time` instead of
`import time`, the monkeypatch would not work.
**Why it happens:** Python import semantics — monkeypatching module attribute only intercepts
references through the module, not direct function references.
**How to avoid:** `cache.py` already uses `import time` (confirmed from reading source). The
existing test pattern in `test_cache.py` (lines 36-38) uses the exact correct approach.
**Pattern (from test_cache.py lines 35-38):**
```python
monkeypatch.setattr(cache_module, "time", type("_T", (), {"time": staticmethod(lambda: original_time() + 1000)})())
```
Use this same approach for the new TTL method test.

### Pitfall 3: search_messages upsert must be inside async with block
**What goes wrong:** The `hits` list is assembled inside `async with connected_client() as client:`
(line 340 in tools.py). The upsert loop must also be inside this block since `hits` is only
available there. Moving it outside would cause a NameError.
**Why it happens:** Python scope — list comprehension inside `async with` block is local to that
context.
**How to avoid:** Place the upsert loop immediately after `hits` is assembled, still inside the
`async with` block.

### Pitfall 4: decode_cursor exceptions include binascii.Error for bad base64
**What goes wrong:** `decode_cursor` calls `base64.urlsafe_b64decode(token.encode())` — a token
with invalid base64 characters raises `binascii.Error` (not `ValueError`). If only `ValueError`
is caught, a corrupt token still propagates as an unhandled error.
**Why it happens:** `binascii.Error` is a subclass of `ValueError` in Python 3 (confirmed — it
inherits from `ValueError`). So catching `ValueError` is actually sufficient. But catching
`Exception` is also fine and more defensive.
**How to avoid:** Use `except Exception as exc:` to be safe. Or verify that `binascii.Error` is
indeed a subclass of `ValueError` (it is in CPython 3.x).

### Pitfall 5: TTL constants — where to define them
**What goes wrong:** If `USER_TTL = 2_592_000` is defined in `cache.py` but `tools.py` imports
from `cache` without importing the constants, call sites in `tools.py` will need to hardcode the
values, making future changes error-prone.
**Why it happens:** No single source of truth for TTL values.
**How to avoid:** Define `USER_TTL` and `GROUP_TTL` constants at module level in `cache.py` (where
the TTL logic lives) and import them in `tools.py`.

## Code Examples

### Existing upsert loop in list_messages (confirmed pattern, HIGH confidence)

```python
# Source: tools.py lines 228-241
for msg in messages:
    sender = getattr(msg, "sender", None)
    if sender is not None:
        sender_name = " ".join(
            filter(None, [
                getattr(sender, "first_name", None),
                getattr(sender, "last_name", None),
            ])
        ) or getattr(sender, "title", "") or str(msg.sender_id)
        sender_type = "user" if getattr(sender, "first_name", None) else "group"
        cache.upsert(
            msg.sender_id, sender_type, sender_name,
            getattr(sender, "username", None)
        )
```

### Existing decode_cursor call site (confirmed, HIGH confidence)

```python
# Source: tools.py lines 204-205
if args.cursor:
    iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)
```

### Existing TTL test pattern (confirmed, HIGH confidence)

```python
# Source: tests/test_cache.py lines 35-38
original_time = time.time
monkeypatch.setattr(cache_module, "time", type("_T", (), {"time": staticmethod(lambda: original_time() + 1000)})())
result = cache.get(101, ttl_seconds=500)
assert result is None
```

### decode_cursor raises ValueError on cross-dialog (confirmed, HIGH confidence)

```python
# Source: src/mcp_telegram/pagination.py lines 18-21
if data["dialog_id"] != expected_dialog_id:
    msg = f"Cursor belongs to dialog {data['dialog_id']}, not {expected_dialog_id}"
    raise ValueError(msg)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `all_names()` returns all entities regardless of age | `all_names_with_ttl(user_ttl, group_ttl)` filters by type-specific TTL | Phase 5 (this phase) | CACH-01 fully satisfied |
| `search_messages` does not upsert sender entities | Upsert loop added after hits assembly | Phase 5 (this phase) | CACH-02 fully satisfied |
| Bad cursor causes unhandled RuntimeError | `decode_cursor` wrapped; returns user-readable `TextContent` | Phase 5 (this phase) | TOOL-03 fully satisfied |

**Deprecated/outdated:**
- Calling `cache.all_names()` in `list_messages`, `search_messages`, `get_user_info`: replace with
  `cache.all_names_with_ttl(USER_TTL, GROUP_TTL)`. The `all_names()` method itself stays (used by
  `list_dialogs` which repopulates the cache on every call, making TTL less relevant there).

## Open Questions

1. **Should `list_dialogs` also use TTL-filtered resolution?**
   - What we know: `list_dialogs` calls `cache.upsert()` for every entity it sees, so its cache
     entries are always fresh after a `ListDialogs` call. It does not call `resolve()`.
   - What's unclear: Not a question — `list_dialogs` does not use resolution at all. No change needed.
   - Recommendation: Leave `list_dialogs` as-is. Only `list_messages`, `search_messages`,
     `get_user_info` call `resolve()` and need the TTL-filtered variant.

2. **Should context messages in search also be upserted?**
   - What we know: CACH-02 says "upsert on every entity-bearing response." Context messages from
     `get_messages` do carry sender entities.
   - What's unclear: The success criterion (ROADMAP.md) says "`search_messages` upserts sender
     entities into the cache after a search" — this implies hits, not necessarily context.
   - Recommendation: Upsert hits only (satisfies the stated success criterion). Upsert of context
     message senders is a nice-to-have but not required for Phase 5. If planner wants to include
     it, the same loop pattern applies over `context_msgs.values()`.

3. **Method name: `all_names_with_ttl` vs `all_names_fresh` vs `filtered_names`?**
   - What we know: `all_names()` is the existing method. A new variant is needed.
   - Recommendation: `all_names_with_ttl(user_ttl: int, group_ttl: int) -> dict[int, str]`
     is explicit about what it does and is easily testable with arbitrary TTL values.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x + pytest-asyncio 1.3.x |
| Config file | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode="auto" |
| Quick run command | `~/.local/bin/uv run pytest tests/test_cache.py tests/test_tools.py -x -q` |
| Full suite command | `~/.local/bin/uv run pytest tests/ -q` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| CACH-01 | `all_names_with_ttl()` excludes entities past their TTL | unit | `~/.local/bin/uv run pytest tests/test_cache.py -k "ttl" -x` | ❌ Wave 0 |
| CACH-01 | `list_messages` resolver only sees non-stale entities | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "stale" -x` | ❌ Wave 0 |
| CACH-02 | `search_messages` upserts sender entities from hits | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_upsert" -x` | ❌ Wave 0 |
| TOOL-03 | `list_messages` with invalid cursor returns readable error | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "cursor_error" -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `~/.local/bin/uv run pytest tests/test_cache.py tests/test_tools.py -x -q`
- **Per wave merge:** `~/.local/bin/uv run pytest tests/ -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_cache.py` — new test: `test_all_names_with_ttl_excludes_stale` — covers CACH-01; use monkeypatch pattern from existing `test_ttl_expiry`
- [ ] `tests/test_cache.py` — new test: `test_all_names_with_ttl_user_vs_group_different_ttl` — verifies user and group TTLs are applied independently
- [ ] `tests/test_tools.py` — new test: `test_list_messages_stale_entity_excluded` — verifies TTL-filtered resolver call in `list_messages`
- [ ] `tests/test_tools.py` — new test: `test_search_messages_upserts_sender` — verifies `cache.upsert` called for hit message sender (covers CACH-02)
- [ ] `tests/test_tools.py` — new test: `test_list_messages_invalid_cursor_returns_error` — verifies friendly TextContent on bad cursor (covers TOOL-03)

*(Existing test infrastructure fully covers all test types needed — no framework installs required)*

## Sources

### Primary (HIGH confidence)
- `src/mcp_telegram/cache.py` — full source; `all_names()` method, `get()` TTL implementation, schema with `type` and `updated_at` columns
- `src/mcp_telegram/tools.py` — full source; `list_messages` upsert loop (lines 228-241), cursor call site (lines 204-205), `search_messages` entity resolution
- `src/mcp_telegram/pagination.py` — full source; `decode_cursor` exception types confirmed
- `tests/test_cache.py` — monkeypatch pattern for time confirmed (lines 35-38)
- `tests/conftest.py` — mock_cache fixture, make_mock_message factory confirmed
- `pyproject.toml` — pytest asyncio_mode=auto confirmed

### Secondary (MEDIUM confidence)
- None

### Tertiary (LOW confidence)
- None (all findings derived from direct source reading)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — zero new dependencies; all changes are to existing files
- Architecture: HIGH — patterns are copies/extensions of existing code in the same files
- Pitfalls: HIGH — derived from direct code reading, not speculation; monkeypatch pattern confirmed working from existing tests

**Research date:** 2026-03-11
**Valid until:** 2026-06-11 (stable internal code; no external library changes affect this)
