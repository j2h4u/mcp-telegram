# Phase 4: SearchMessages Context Window - Research

**Researched:** 2026-03-11
**Domain:** Telethon search + context fetch, Python async patterns
**Confidence:** HIGH

## Summary

Phase 4 is a focused surgical change to `search_messages` in `tools.py`. The context window feature
was listed as complete in Phase 2's planning documents but was never actually implemented — the
current code returns raw search hits with no surrounding messages. The gap is unambiguous when
reading `tools.py`: `search_messages` calls `iter_messages` with `search=` once and immediately
calls `format_messages(hits, reply_map={})`. There is no second fetch for context, no reaction
names, no hit-vs-context visual distinction.

The implementation path is well-defined: after fetching search hits, for each hit call
`client.get_messages(entity_id, ids=range_around_hit)` to retrieve up to 3 messages before and
after by ID. Deduplicate across hits (hits near each other share context), assemble per-hit groups,
format each group with a separator and hit marker, then concatenate. The existing
`format_messages` function does not need modification — the formatting layer is correct; only the
data assembly layer in `search_messages` needs to change.

The test count is currently **48** (not 42 as stated in the roadmap — the roadmap text predates
Phase 3 completion). The success criterion "All 42 existing tests remain green" should be
interpreted as "all existing tests before Phase 4 starts remain green."

**Primary recommendation:** Implement context fetch as a per-hit `get_messages` call batched by
unique IDs, merge with deduplication, then format each hit group with a `>>> HIT <<<` separator
to make hits visually distinct.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TOOL-06 | `SearchMessages` accepts dialog by name, returns each result with ±3 messages of surrounding context | Telethon `get_messages(ids=list)` confirmed for batch context fetch; deduplication logic documented in Architecture Patterns section |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| telethon | ≥1.23.0 (pinned in pyproject) | Telegram MTProto client | Already the project's only Telegram client |
| pytest + pytest-asyncio | ≥9.0.2 / ≥1.3.0 | Test framework | Already installed, asyncio_mode=auto configured |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| unittest.mock AsyncMock/MagicMock | stdlib | Mock Telethon client in tests | Consistent with all existing test_tools.py patterns |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `get_messages(ids=list)` | `iter_messages(min_id=X, max_id=Y)` | iter_messages range is simpler but fetches many extras; get_messages by ID list is precise and already used in list_messages for reply_map |

**Installation:** No new packages required.

## Architecture Patterns

### How Context Fetch Works in Telethon

`client.get_messages(entity, ids=list_of_ints)` fetches specific messages by ID. This is already
used in `list_messages` for `reply_map` population (line 251-253 in tools.py). The same pattern
applies here.

For a hit with `msg.id = N`, context IDs are `[N-3, N-2, N-1, N+1, N+2, N+3]`. IDs that don't
exist return `None` — Telethon fills missing slots with `None` when fetching a list of IDs. The
caller must filter those out.

### Recommended Project Structure

No new files. Changes are confined to `tools.py` (`search_messages` function) and `test_tools.py`
(new test cases for TOOL-06 within the `--- TOOL-06 ---` section).

### Pattern 1: Per-hit context fetch with deduplication

**What:** For each hit, compute context IDs N±3, collect all unique IDs across all hits, fetch
in one or few batched `get_messages` calls, then build per-hit groups.

**When to use:** When hits may be close together (context windows overlap). Batching avoids
redundant API calls.

**Example:**

```python
# Inside async with connected_client() as client:
hits = [msg async for msg in client.iter_messages(
    entity_id, search=args.query, limit=args.limit, add_offset=page_offset
)]

# Compute context IDs — all unique, excluding hit IDs themselves
context_ids_needed: set[int] = set()
for hit in hits:
    for offset in range(-3, 4):
        if offset != 0:
            context_ids_needed.add(hit.id + offset)
# Remove IDs already in hits (they are hits, not context)
hit_ids = {h.id for h in hits}
context_ids_needed -= hit_ids

# Fetch all context messages in batch
context_msgs: dict[int, object] = {}
if context_ids_needed:
    fetched = await client.get_messages(entity_id, ids=list(context_ids_needed))
    fetched_list = fetched if isinstance(fetched, list) else [fetched]
    context_msgs = {m.id: m for m in fetched_list if m is not None}

# Build per-hit groups: list of (hit, [before...], [after...])
groups = []
for hit in hits:
    before = [context_msgs[hit.id - i] for i in range(3, 0, -1)
              if (hit.id - i) in context_msgs]
    after = [context_msgs[hit.id + i] for i in range(1, 4)
             if (hit.id + i) in context_msgs]
    groups.append((hit, before, after))
```

### Pattern 2: Per-hit group formatting with hit marker

**What:** Format each group as context_before + hit + context_after, with a visual separator
between groups and the hit line prefixed to distinguish it from context.

**When to use:** Every search result rendering.

**Example output structure:**

```
--- hit 1 ---
10:05 Иван: [context message before]
>>> 10:10 Иван: the hit message <<<
10:15 Иван: [context message after]

--- hit 2 ---
...
```

The simplest implementation: call `format_messages([*before, hit, *after], reply_map={})` for
each group — this gives correct date headers and session breaks. Then prefix the hit line in the
resulting text. Alternatively, format all messages and mark the hit line by its known time+sender
prefix. However, the cleanest approach is to **format each group separately and wrap with a
hit-group header**.

### Pattern 3: Reaction names for search results (parity with ListMessages)

**What:** Build `reaction_names_map` for search hits (not context messages) using the same
`GetMessageReactionsListRequest` loop already in `list_messages`.

**When to use:** Always, to satisfy success criterion 3 (parity with ListMessages).

**Reference:** The exact loop (lines 256-287 in tools.py) can be copied directly into
`search_messages`. The loop only needs to run over `hits`, not context messages.

### Anti-Patterns to Avoid

- **Fetching context one message at a time:** `get_messages` accepts a list; batch all unique IDs
  in a single call (or small number of calls if IDs list is very large).
- **Calling `format_messages` on the flat list of all hits+context unsorted:** The formatter
  expects messages in reverse-chronological order (newest first, same as `iter_messages` output).
  Build each per-hit group in chronological order [before..., hit, after...] and pass to
  `format_messages` which `reversed()` internally, so pass oldest-first. Wait — check the actual
  formatter: it calls `for msg in reversed(messages)` (line 41 in formatter.py), meaning it
  expects the input list in **newest-first** order (as iter_messages returns). To get
  chronological display, pass `[*after_reversed, hit, *before_reversed]` or sort by `msg.date`
  descending before calling `format_messages`. Simplest: sort each group by `msg.id` descending
  (higher ID = newer in Telegram).
- **Modifying `format_messages` signature:** Do not add hit-marker logic to the formatter; keep
  formatting pure. Apply the hit prefix to the assembled text after `format_messages` returns.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Fetch messages by ID | Custom search/range iteration | `client.get_messages(entity, ids=list)` | Already used in list_messages reply_map; handles missing IDs with None |
| Date headers / session breaks | Custom text assembly | `format_messages()` from formatter.py | Already correct, tested, handles timezone |
| Reaction name lookup | New mechanism | Exact loop from list_messages (copy) | Identical requirements, identical Telethon API |

## Common Pitfalls

### Pitfall 1: Telethon returns None for non-existent message IDs
**What goes wrong:** `get_messages(entity, ids=[N-3, N-2, ...])` returns a list where some
positions are `None` if those IDs don't exist (e.g., first message in chat has no ID-3).
**Why it happens:** Telegram API returns sparse results for ID lists.
**How to avoid:** Always filter `[m for m in fetched_list if m is not None]` before building
`context_msgs`.
**Warning signs:** `AttributeError: 'NoneType' object has no attribute 'id'` in formatting.

### Pitfall 2: formatter.py expects newest-first input
**What goes wrong:** Passing context group in chronological (oldest-first) order produces reversed
output — `reversed(messages)` in formatter line 41 will display it backward.
**Why it happens:** `iter_messages` with `reverse=False` (default) returns newest-first; the
formatter was designed around that.
**How to avoid:** Sort the per-hit group by `msg.id` **descending** before passing to
`format_messages`. `sorted([*before, hit, *after], key=lambda m: m.id, reverse=True)`.
**Warning signs:** Messages appear in wrong chronological order in output.

### Pitfall 3: Existing test `test_search_messages_context` will need updating
**What goes wrong:** The current test (line 201-215 in test_tools.py) only asserts `"the hit"` in
output. It does not mock `get_messages` for context fetch. Once context fetch is added, the test
will call `mock_client.get_messages(...)` which is an `AsyncMock` by default — it will return a
`MagicMock`, not a list, causing `isinstance(fetched, list)` to be False, and the code will try
to iterate a MagicMock. The test will not crash but the mock needs to return an empty list or
proper context messages.
**How to avoid:** In the Wave 0 plan, update `test_search_messages_context` to add
`mock_client.get_messages = AsyncMock(return_value=[])` (no context in this test's scenario is
fine — it just needs to not error). New tests asserting context window behavior will set up
richer mocks.
**Warning signs:** `TypeError` or `AttributeError` in `search_messages` during test run after
implementation.

### Pitfall 4: Context IDs may overlap between adjacent hits
**What goes wrong:** If hit A has ID=50 and hit B has ID=52, context of A includes 53 and context
of B includes 49 — they share IDs 51-53. Without deduplication, the same message is fetched
twice and appears in multiple groups.
**How to avoid:** Build `context_ids_needed` as a set, subtract `hit_ids`, fetch once.

### Pitfall 5: Test count mismatch in success criteria
**What goes wrong:** Roadmap says "All 42 existing tests remain green" but there are now 48 tests.
**Why it happens:** The roadmap was written before Phase 3 added 6 more tests.
**How to avoid:** The actual baseline is 48. Plan Wave 0 as: new failing tests bring total above
48; Wave 1 makes them pass while keeping all 48 original tests green.

## Code Examples

### Existing `get_messages` usage in list_messages (confirmed pattern, HIGH confidence)

```python
# Source: tools.py lines 250-253
replied = await client.get_messages(entity_id, ids=reply_ids)
replied_list = replied if isinstance(replied, list) else [replied]
reply_map = {m.id: m for m in replied_list if m}
```

This is the exact pattern to reuse for context fetch. Note `if m` filters None entries.

### Existing reaction_names_map loop (copy this into search_messages)

```python
# Source: tools.py lines 256-287
reaction_names_map: dict[int, dict[str, list[str]]] = {}
for msg in messages:  # <-- replace `messages` with `hits` for search context
    rxns = getattr(msg, "reactions", None)
    ...
```

### Formatter input order verification

```python
# Source: formatter.py line 41
for msg in reversed(messages):  # expects newest-first input
```

To display chronologically (oldest at top), pass list sorted by id descending:
```python
group = sorted([*before, hit, *after], key=lambda m: m.id, reverse=True)
text = format_messages(group, reply_map={}, reaction_names_map=reaction_names_map)
```

### Mock pattern for `get_messages` in tests

```python
# Consistent with Phase 02/03 patterns in test_tools.py
mock_client.get_messages = AsyncMock(return_value=[ctx_msg_before, ctx_msg_after])
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `search_messages` returns flat hits only | `search_messages` returns ±3 context per hit | Phase 4 (this phase) | TOOL-06 gap closed |
| No reaction names in search results | reaction_names_map passed to format_messages | Phase 4 (this phase) | Parity with ListMessages |

**Deprecated/outdated:**
- `format_messages(hits, reply_map={})` call in search_messages: replace with per-group call with
  reaction_names_map populated.

## Open Questions

1. **Visual hit marker format**
   - What we know: success criterion says "visually distinguishable" but does not specify exact format
   - What's unclear: `--- hit N ---` header vs `>>>` prefix on the hit line vs both
   - Recommendation: Use `--- hit N/M ---` group header (consistent with date/session-break
     format using `---`) plus `[HIT] ` prefix on the hit message line. This gives two levels of
     distinction without modifying formatter.py.

2. **Maximum context IDs per batch**
   - What we know: `get_messages` accepts a list; Telegram API has limits per request
   - What's unclear: exact limit for a single `get_messages` call with a large ID list
   - Recommendation: For typical `limit=20` hits × 6 context IDs each = max 120 unique context IDs.
     Telethon handles batching internally for large ID lists. No manual chunking needed in Phase 4.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x + pytest-asyncio 1.3.x |
| Config file | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode="auto" |
| Quick run command | `~/.local/bin/uv run pytest tests/test_tools.py -x -q` |
| Full suite command | `~/.local/bin/uv run pytest tests/ -q` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| TOOL-06 | SearchMessages returns ±3 context around each hit | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_context" -x` | ❌ Wave 0 |
| TOOL-06 | Hit messages are visually distinct from context messages | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_hit_marker" -x` | ❌ Wave 0 |
| TOOL-06 | Reaction names passed to format_messages for search results | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_reaction" -x` | ❌ Wave 0 |
| TOOL-06 | Existing search test updated to handle get_messages mock | unit | `~/.local/bin/uv run pytest tests/test_tools.py::test_search_messages_context -x` | ✅ (needs update) |

### Sampling Rate
- **Per task commit:** `~/.local/bin/uv run pytest tests/test_tools.py -x -q`
- **Per wave merge:** `~/.local/bin/uv run pytest tests/ -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] New test: `test_search_messages_context_window` — asserts 3 messages before hit appear in output
- [ ] New test: `test_search_messages_context_after_hit` — asserts 3 messages after hit appear in output
- [ ] New test: `test_search_messages_hit_marker` — asserts hit message line is visually distinct
- [ ] New test: `test_search_messages_reaction_names_fetched` — asserts `GetMessageReactionsListRequest` is called for hits with reactions ≤ threshold
- [ ] Update: `test_search_messages_context` — add `mock_client.get_messages = AsyncMock(return_value=[])` to handle context fetch

## Sources

### Primary (HIGH confidence)
- `tools.py` lines 250-253 — `get_messages(entity, ids=list)` pattern with None-filtering
- `tools.py` lines 256-287 — `reaction_names_map` loop (copy target)
- `formatter.py` line 41 — `reversed(messages)` confirms newest-first input contract
- `tests/test_tools.py` lines 198-261 — existing TOOL-06/TOOL-07 tests (baseline)

### Secondary (MEDIUM confidence)
- Telethon documentation behavior: `get_messages` returns None for non-existent IDs — inferred
  from the existing `if m` filter in the project's own code and consistent with Telethon's
  documented sparse-result behavior for ID lists.

### Tertiary (LOW confidence)
- None

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new dependencies, all existing libraries
- Architecture: HIGH — pattern directly derived from existing code in the same file
- Pitfalls: HIGH — pitfalls derived from direct code reading, not speculation

**Research date:** 2026-03-11
**Valid until:** 2026-06-11 (stable library, internal code)
