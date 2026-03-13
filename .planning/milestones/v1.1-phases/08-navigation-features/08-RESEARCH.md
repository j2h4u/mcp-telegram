# Phase 8: Navigation Features - Research

**Researched:** 2026-03-12
**Domain:** Message iteration API design, pagination cursor mechanics, Telegram archived dialog handling
**Confidence:** HIGH

## Summary

Phase 8 implements two navigation enhancements: reverse message iteration (oldest-first) and archived dialog visibility. Both features leverage existing Telethon capabilities (`reverse=True` parameter, `iter_dialogs()` folder filtering) and current pagination infrastructure (cursor encoding/decoding). The main architectural change is a parameter addition to `ListMessages` (`from_beginning: bool`) and a parameter modification to `ListDialogs` (change semantic from `archived: bool` to `exclude_archived: bool` with different default). Cursor pagination is fully compatible with reverse iteration—the existing encoder/decoder logic remains unchanged.

**Primary recommendation:**
1. Add `from_beginning: bool = False` parameter to `ListMessages`; when true, use `reverse=True, min_id=1` in iter_messages kwargs
2. Change `ListDialogs` parameter semantics: rename `archived: bool = False` to `exclude_archived: bool = False` (inverted logic); fetch all dialogs by default
3. Test pagination boundaries (first/last page, reverse iteration cursor validity) as critical path

## User Constraints

No CONTEXT.md exists. Research proceeds without upstream constraints.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| NAV-01 | `ListMessages` gains `from_beginning: bool` parameter; when true, fetches oldest messages first (reverse=True, min_id=1) | Telethon `reverse` parameter fully supports oldest→recent iteration; cursor pagination works with reverse iteration |
| NAV-02 | `ListDialogs` returns archived and non-archived dialogs by default; `exclude_archived: bool = False` parameter allows filtering | Telethon `iter_dialogs()` accepts `archived` parameter but uses different semantics (None=mixed, True=archived only, False=unarchived only); need parameter inversion in API |

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| Telethon | 1.23.0+ | Telegram MTProto client library | Industry-standard Python Telegram API wrapper; features reverse pagination natively |
| pydantic | 2.0.0+ | Request/response schema validation | Project standard for tool argument validation |
| pytest-asyncio | 1.3.0+ | Async test execution framework | Required for testing async tool handlers |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| rapidfuzz | 3.14.3+ | Fuzzy entity resolution | Already used for dialog/sender name matching |

## Architecture Patterns

### Pagination with Reverse Iteration

**What:** Cursor-based pagination supports both forward (newest→oldest) and reverse (oldest→newest) iteration.

**Implementation detail:**
- `encode_cursor(message_id, dialog_id)` → opaque base64 token
- `decode_cursor(token, dialog_id)` → message_id
- Token contains dialog_id validation to prevent cross-dialog errors
- `max_id` used for forward pagination (returns messages with ID < max_id)
- `min_id=1` used at start of reverse iteration (returns messages with ID > 1)

**When to use:**
- Forward (default): `reverse=False, max_id=decoded_cursor` → newest first, page backward
- Reverse: `reverse=True, min_id=1` (or `min_id=decoded_cursor`) → oldest first, page forward

**Example:**
```python
# Phase 1: forward pagination (newest first)
async for msg in client.iter_messages(entity_id, reverse=False, limit=100):
    # messages are newest first, IDs descend
    pass

# Phase 2: reverse pagination (oldest first)
async for msg in client.iter_messages(entity_id, reverse=True, min_id=1, limit=100):
    # messages are oldest first, IDs ascend
    pass
```

Source: [Telethon iter_messages documentation](https://docs.telethon.dev/en/stable/modules/client.html)

### Current Pagination Flow (tools.py, lines 268–387)

```python
# Step 1: Resolve dialog name → entity_id
entity_id = resolve(args.dialog, ...)

# Step 2: Build iter_kwargs (forward iteration)
iter_kwargs = {
    "entity": entity_id,
    "limit": args.limit,
    "reverse": False,  # ← currently hardcoded
}
if args.cursor:
    iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)

# Step 3: Fetch messages
messages = [msg async for msg in client.iter_messages(**iter_kwargs)]

# Step 4: Format (reverses message list internally)
text = format_messages(messages, reply_map=reply_map, ...)

# Step 5: Generate cursor for next page
if len(messages) == args.limit and messages:
    next_cursor = encode_cursor(messages[-1].id, entity_id)
```

Source: [mcp-telegram/src/mcp_telegram/tools.py, lines 268-387](https://github.com/j2h4u/mcp-telegram/blob/main/src/mcp_telegram/tools.py#L268-L387)

### Reverse Iteration - Required Changes

To support `from_beginning=True`:

```python
# New parameter in ListMessages class
class ListMessages(ToolArgs):
    from_beginning: bool = False  # NEW

# In list_messages handler:
iter_kwargs = {
    "entity": entity_id,
    "limit": args.limit,
    "reverse": args.from_beginning,  # ← toggle based on parameter
}
if args.from_beginning:
    # Reverse iteration: start from message ID 1, page forward
    iter_kwargs["min_id"] = 1 if not args.cursor else decode_cursor(args.cursor, entity_id)
else:
    # Forward iteration: start from present, page backward
    if args.cursor:
        iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)

# Cursor generation works identically for both directions
if len(messages) == args.limit and messages:
    next_cursor = encode_cursor(messages[-1].id, entity_id)
```

**Critical:** formatter.py line 41 already reverses message list (`for msg in reversed(messages)`), so output will display correctly regardless of iteration direction. This is the key reason reverse pagination is low-risk.

### Archived Dialogs - API Semantic Change

**Current behavior (v1.0):**
- `ListDialogs(archived=False)` → shows only non-archived dialogs
- `ListDialogs(archived=True)` → shows only archived dialogs
- No way to show both simultaneously (NAV-02 requirement)

**Required behavior (v1.1, NAV-02):**
- `ListDialogs()` → shows both archived AND non-archived (default)
- `ListDialogs(exclude_archived=True)` → shows only non-archived
- `ListDialogs(exclude_archived=False)` → shows only archived

**Implementation strategy:**
1. Rename parameter: `archived: bool` → `exclude_archived: bool`
2. Invert logic in handler:
   - When `exclude_archived=False` (default): don't filter, show all (`archived=None` to iter_dialogs)
   - When `exclude_archived=True`: show only non-archived (`archived=False` to iter_dialogs)

**Telethon's iter_dialogs() semantics (reference):**
- `archived=None` (default) → fetches current folder only (mixed archived/unarchived)
- `archived=True` → archive folder only
- `archived=False` → main (non-archived) folder only
- Archived dialogs identified by `dialog.folder_id is not None` in Telegram's data model

Source: [Telethon GetDialogsRequest documentation](https://tl.telethon.dev/methods/messages/get_dialogs.html)

### API Signature Change (tools.py, line 143)

```python
# Current (v1.0)
class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp."""
    archived: bool = False
    ignore_pinned: bool = False

# Required (v1.1)
class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels with type and last message timestamp.
    Returns both archived and non-archived by default."""
    exclude_archived: bool = False  # Changed parameter name + inverted default
    ignore_pinned: bool = False
```

And in handler (tools.py, line 164-165):
```python
# Current
async for dialog in client.iter_dialogs(
    archived=args.archived, ignore_pinned=args.ignore_pinned
):

# Required: need smart mapping
archived_param = None if not args.exclude_archived else False
async for dialog in client.iter_dialogs(
    archived=archived_param, ignore_pinned=args.ignore_pinned
):
```

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Message cursor generation | Custom token encoding (UUID, timestamp) | Existing `encode_cursor(message_id, dialog_id)` | Already battle-tested, prevents cross-dialog corruption |
| Telegram message ID ordering semantics | Custom message sorting logic | Telethon's `reverse` parameter + formatter's `reversed()` | Telethon handles Telegram API quirks (ID gaps, deleted messages); formatter handles display order |
| Pagination state management | Homemade cursor validation | Existing `decode_cursor()` with dialog_id verification | Validates dialog match, catches bugs early |
| Archive folder detection | Custom folder logic | Telethon's `archived` parameter to iter_dialogs | Handles Telegram's folder abstraction correctly |

## Common Pitfalls

### Pitfall 1: Confusing Message ID Direction with Display Order

**What goes wrong:** Developer assumes `reverse=True` means "show oldest-first in output", but iter_messages still yields messages in ID ascending order; output needs separate formatter reversal.

**Why it happens:** Telethon's `reverse` parameter controls iteration direction (ID ascending vs descending), not output order. Formatter always receives messages in iteration order.

**How to avoid:**
- Keep formatter's `for msg in reversed(messages)` when using `reverse=False` (default)
- When adding `reverse=True` support, verify formatter still reverses correctly (it does, unconditionally)
- Test with mixed-ID messages: [1, 5, 3, 10] iteration → format should display chronologically regardless of iteration order

**Warning signs:**
- Output shows messages out of chronological order
- Pagination cursor points to wrong message after switching `from_beginning`

### Pitfall 2: Cursor Validity Across Iteration Directions

**What goes wrong:** Cursor generated during reverse iteration used in forward iteration (or vice versa), causing "no results" or skipped messages.

**Why it happens:** Cursor is just a message ID; it doesn't encode iteration direction. `decode_cursor()` returns the ID, but the caller must use it with the correct parameter (`max_id` vs `min_id`).

**How to avoid:**
- When `from_beginning=True`, use `min_id=decoded_cursor` (or `min_id=1` at start)
- When `from_beginning=False`, use `max_id=decoded_cursor`
- Test pagination state machine: generate cursor in reverse mode → switch to forward mode → verify output
- Add assertion: `if args.from_beginning and args.cursor: assert use min_id`

**Warning signs:**
- Next page returns 0 messages when limit=100 would normally return 100
- Switching `from_beginning` mid-conversation breaks pagination

### Pitfall 3: Confusing `exclude_archived` Parameter Default

**What goes wrong:** User thinks `ListDialogs()` with no args shows only non-archived (old behavior), but it now shows both archived and non-archived.

**Why it happens:** Parameter renamed and inverted (exclude=False means "don't exclude" = show all). Breaking change in API semantics.

**How to avoid:**
- Update docstring: "Returns both archived and non-archived by default"
- In tests, explicitly test: `ListDialogs()` returns archived entries
- LLM context (SYSTEM message) should document new default
- Deprecation: could support `archived=` for backward compatibility with clear error message

**Warning signs:**
- Tests expect old behavior (`ListDialogs()` → non-archived only)
- LLM asks "why are archived chats showing up?" after upgrade

### Pitfall 4: min_id=1 vs min_id=0 at Reverse Pagination Start

**What goes wrong:** Using `min_id=0` instead of `min_id=1` causes Telethon to interpret it as "no lower bound" or causes off-by-one errors.

**Why it happens:** Telegram message IDs are 1-indexed; ID 0 is invalid and may trigger special behavior.

**How to avoid:**
- Always use `min_id=1` for "start from oldest" in reverse pagination
- Document this magic constant with a comment
- Test: reverse pagination without cursor should return oldest messages first

**Warning signs:**
- Reverse pagination without cursor skips message ID 1
- Behavior differs between first page and subsequent pages

### Pitfall 5: Reusing Cached Entities from Hidden Archived Dialogs

**What goes wrong:** User archives a dialog, then later searches for a message from that contact; entity cache still has the cached name, but it's not visible in ListDialogs, causing "contact not found" UX confusion.

**Why it happens:** NAV-02 requires showing archived dialogs in ListDialogs to prevent false-negative name resolution. Entity cache may be stale if archived dialog wasn't in previous ListDialogs call.

**How to avoid:**
- ListDialogs (new behavior) always iterates both archived and non-archived → populates cache fully
- Verify: cache.upsert() called for entities from both archived and non-archived dialogs
- Test scenario: create dialog, archive it, run ListDialogs, verify archived dialog in output and in cache

**Warning signs:**
- "Contact not found" error despite entity being in cache
- Entity appears in cache but not in ListDialogs output

## Code Examples

Verified patterns from official sources:

### Reverse Pagination - Oldest First

```python
# Source: Telethon 1.42.0 iter_messages, Phase 8 ListMessages implementation

# Support from_beginning parameter
class ListMessages(ToolArgs):
    dialog: str
    limit: int = 100
    cursor: str | None = None
    sender: str | None = None
    unread: bool = False
    from_beginning: bool = False  # NEW

# In handler
iter_kwargs = {
    "entity": entity_id,
    "limit": args.limit,
    "reverse": args.from_beginning,  # Toggle iteration direction
}

if args.from_beginning:
    # Reverse iteration: oldest messages first
    if args.cursor:
        iter_kwargs["min_id"] = decode_cursor(args.cursor, entity_id)
    else:
        iter_kwargs["min_id"] = 1  # Start from oldest
else:
    # Forward iteration: newest messages first (default)
    if args.cursor:
        iter_kwargs["max_id"] = decode_cursor(args.cursor, entity_id)

messages = [msg async for msg in client.iter_messages(**iter_kwargs)]
```

### Archived Dialogs - Show All by Default

```python
# Source: Telethon 1.42.0 iter_dialogs, Phase 8 ListDialogs implementation

class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels...

    Returns both archived and non-archived dialogs by default.
    Set exclude_archived=True to show only non-archived.
    """
    exclude_archived: bool = False  # NEW (renamed from archived, inverted)
    ignore_pinned: bool = False

# In handler
# Map parameter to Telethon's archived parameter
# None = mixed (current folder + archives), False = main folder, True = archive folder
if args.exclude_archived:
    # Show only non-archived
    telethon_archived_param = False
else:
    # Show both archived and non-archived (default)
    telethon_archived_param = None

async for dialog in client.iter_dialogs(
    archived=telethon_archived_param,
    ignore_pinned=args.ignore_pinned
):
    # ... existing cache/output logic
```

### Formatter Compatibility (No Changes)

```python
# Source: mcp-telegram/src/mcp_telegram/formatter.py, line 41

def format_messages(messages, reply_map, reaction_names_map=None, tz=None):
    # ... existing code ...

    # Reverses message list REGARDLESS of iteration direction
    for msg in reversed(messages):
        # ... format each message ...

    # Result: messages display chronologically (oldest first) regardless of
    # whether iter_messages used reverse=True or reverse=False
```

This is **the key insight**: formatter's unconditional reversal means Phase 8 is purely parametric—no formatter changes needed.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Newest-first only | `from_beginning` parameter for oldest-first | v1.1 (Phase 8) | Enables reading chat history from start; LLM can traverse backward |
| Optional archived visibility | Default archived visibility with opt-out | v1.1 (Phase 8) | Prevents "contact not found" false negatives for archived chats |
| `archived: bool` parameter | `exclude_archived: bool` inverted semantics | v1.1 (Phase 8) | Breaking API change; requires migration in LLM instructions |

**Deprecated/outdated:**
- Fetching only non-archived dialogs by default — now shows all to prevent entity resolution errors

## Open Questions

1. **Should `from_beginning` ignore `cursor` parameter?**
   - What we know: NAV-01 spec says "ignores any cursor" but current code respects cursor
   - What's unclear: Is this intentional or does spec want to reset pagination?
   - Recommendation: Implement as "cursor overrides default, but from_beginning selects iteration direction" — safer semantics, allows resuming reverse pagination

2. **Backward compatibility for `archived` parameter?**
   - What we know: Parameter name + semantics changing from `archived: bool` to `exclude_archived: bool`
   - What's unclear: Should old `archived=True` calls still work?
   - Recommendation: No backward compat (v1.1 is minor bump); document breaking change in release notes

3. **Telethon version compatibility for `reverse` parameter?**
   - What we know: reverse parameter exists in 1.23.0+ (project requires 1.23.0+)
   - What's unclear: Any version-specific behavior quirks?
   - Recommendation: No special handling needed; minimum version covers it

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2+ with pytest-asyncio 1.3.0+ |
| Config file | pyproject.toml (asyncio_mode = "auto") |
| Quick run command | `pytest tests/test_tools.py::test_list_messages_reverse_pagination -v` |
| Full suite command | `pytest tests/ -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| NAV-01 | ListMessages accepts `from_beginning: bool` parameter | unit | `pytest tests/test_tools.py -k reverse -v` | ❌ Wave 0 |
| NAV-01 | from_beginning=True fetches oldest messages first (reverse=True, min_id=1) | integration | `pytest tests/test_tools.py::test_list_messages_from_beginning -v` | ❌ Wave 0 |
| NAV-01 | Cursor pagination works with reverse iteration (both forward and backward from_beginning modes tested) | integration | `pytest tests/test_tools.py::test_list_messages_reverse_pagination_cursor -v` | ❌ Wave 0 |
| NAV-02 | ListDialogs returns archived and non-archived by default | unit | `pytest tests/test_tools.py::test_list_dialogs_archived_default -v` | ❌ Wave 0 |
| NAV-02 | exclude_archived parameter filters correctly | unit | `pytest tests/test_tools.py::test_list_dialogs_exclude_archived -v` | ❌ Wave 0 |
| NAV-02 | Archived chats visible in entity cache | integration | `pytest tests/test_cache.py -k archive -v` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `pytest tests/test_tools.py -v` (quick suite for tools)
- **Per wave merge:** `pytest tests/ -v` (full suite)
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/test_tools.py::test_list_messages_from_beginning` — tests `from_beginning=True` parameter, verifies messages returned oldest-first
- [ ] `tests/test_tools.py::test_list_messages_reverse_pagination_cursor` — tests cursor generation/decoding with reverse iteration
- [ ] `tests/test_tools.py::test_list_dialogs_archived_default` — tests default behavior shows both archived and non-archived
- [ ] `tests/test_tools.py::test_list_dialogs_exclude_archived` — tests `exclude_archived=True` filters out archived dialogs
- [ ] `tests/test_pagination.py` — add tests for bidirectional cursor validity (cursor from reverse mode used in forward mode)
- [ ] `conftest.py` enhancement — add fixture `make_mock_dialog` for archived dialog mocking (similar to `make_mock_message`)

*(Existing test infrastructure covers ListDialogs/ListMessages structure; gaps are scenario-specific for navigation features)*

## Sources

### Primary (HIGH confidence)
- [Telethon iter_messages documentation](https://docs.telethon.dev/en/stable/modules/client.html) - reverse parameter, min_id/max_id semantics
- [Telethon GetDialogsRequest](https://tl.telethon.dev/methods/messages/get_dialogs.html) - archived folder handling
- [mcp-telegram source: tools.py](https://github.com/j2h4u/mcp-telegram/blob/main/src/mcp_telegram/tools.py) - current pagination implementation
- [mcp-telegram source: pagination.py](https://github.com/j2h4u/mcp-telegram/blob/main/src/mcp_telegram/pagination.py) - cursor encoding/decoding
- [mcp-telegram source: formatter.py](https://github.com/j2h4u/mcp-telegram/blob/main/src/mcp_telegram/formatter.py) - message display logic
- [REQUIREMENTS.md NAV-01, NAV-02](https://github.com/j2h4u/mcp-telegram/blob/main/.planning/REQUIREMENTS.md#navigation) - phase requirements

### Secondary (MEDIUM confidence)
- [Telethon API offsets documentation](https://core.telegram.org/api/offsets) - general pagination principles

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - Telethon 1.23.0+ fully supports reverse parameter; existing code uses pagination correctly
- Architecture: HIGH - Pagination cursor logic already proven in Phase 1-7; reverse iteration is additive parameter
- Pitfalls: MEDIUM - Reverse iteration is novel for this codebase; pitfalls identified by code review + Telethon issue history

**Research date:** 2026-03-12
**Valid until:** 2026-04-12 (30 days — Telethon stable; Telegram API rarely changes pagination semantics)
**Key assumption:** Telethon's `reverse` parameter and `iter_dialogs(archived=)` behavior remain stable across minor versions 1.23.0-1.42.0
