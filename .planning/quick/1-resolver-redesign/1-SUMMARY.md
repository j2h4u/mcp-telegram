---
phase: quick
plan: 1-resolver-redesign
type: summary
subsystem: resolver
tags: [resolver, disambiguation, metadata, agent-guidance]
dependency:
  requires: []
  provides: [resolver-redesign, candidates-metadata, username-lookup]
  affects: [tools, agent-interaction]
tech_stack:
  added: []
  patterns: [deterministic-resolution, ambiguity-prioritization, cache-metadata-fetch]
key_files:
  created: []
  modified:
    - src/mcp_telegram/resolver.py
    - src/mcp_telegram/tools.py
    - tests/test_resolver.py
decisions:
  - Candidates always returned for fuzzy matches (no single >=90 auto-resolve)
  - @username resolution added as deterministic case before fuzzy matching
  - Exact case-insensitive match has priority over all fuzzy matches
  - Metadata (username, entity_type) fetched from cache only when returning Candidates
metrics:
  duration: "~15 min"
  completed: "2026-03-11T20:15:00Z"
  tasks_completed: 2
  files_modified: 3
  tests: 50 (22 resolver + 28 tools, all passing)
---

# Quick Plan 1: Resolver Redesign — Summary

## Objective
Redesign resolver logic to deterministically resolve specific input types (numeric ID, @username, exact string match) to Resolved, and reserve Candidates for all ambiguous cases. Ensure Candidates include full metadata needed for agent disambiguation.

## What was built

### Task 1: Redesigned resolver.py with extended Candidates dataclass ✅
**Purpose**: Implement 5-case resolution logic with @username support and metadata enrichment.

**Changes**:
- **Extended Candidates dataclass** from `matches: list[tuple[str, int, int]]` to `matches: list[dict]`
  - Each match dict now contains: `entity_id`, `display_name`, `score`, `username`, `entity_type`
  - Allows agents to disambiguate with full context (ID, name, score, handle, entity type)

- **Updated resolve() signature** to accept optional `cache: EntityCache | None = None`
  - Enables @username lookup and metadata fetching during resolution

- **Redesigned _fuzzy_resolve() function** with new logic:
  1. Extract all hits >=60 into matches list
  2. Apply exact case-insensitive filter → if match found, return Resolved (priority)
  3. Otherwise → always return Candidates (even single fuzzy hit >=90)
  4. Fetch metadata from cache for each match if cache provided

- **New @username resolution** (Case 2 in resolve()):
  - Detects queries starting with "@" (e.g., "@alice")
  - Looks up username in cache, returns Resolved if found
  - Returns NotFound if cache lookup fails or cache not provided

- **5-case resolution logic** in resolve():
  1. **Numeric ID query** → Resolved/NotFound by id (unchanged)
  2. **@username query** → cache lookup, Resolved/NotFound (NEW)
  3. **Exact case-insensitive string match** → Resolved (unchanged priority)
  4. **All fuzzy matches >=60** → Candidates (CHANGED: no more auto-resolve >=90)
  5. **No matches >=60** → NotFound (unchanged)
  6. **Bonus**: Cyrillic transliteration retry on NotFound (unchanged)

**Verification**: 22 resolver tests pass
- 9 new test cases for redesigned behavior
- Test case 1-9 all passing (numeric ID, @username, exact match, fuzzy candidates, no matches, metadata, transliteration)
- Backward compatibility preserved: numeric ID and transliteration work as before

### Task 2: Updated tools.py to pass cache to resolve() ✅
**Purpose**: Integrate cache-aware resolution and provide agent guidance on disambiguation.

**Changes**:
- **Updated all 4 resolve() calls** to pass cache parameter:
  1. ListMessages (dialog) → `resolve(args.dialog, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)`
  2. ListMessages (sender) → `resolve(args.sender, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)`
  3. SearchMessages (dialog) → `resolve(args.dialog, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)`
  4. GetUserInfo (user) → `resolve(args.user, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)`

- **Enhanced Candidates output formatting** across all tools:
  - Old format: `Ambiguous dialog "ivan". Matches: "Ivan Petrov", "Ivanovich"`
  - New format:
    ```
    Ambiguous dialog "ivan". Matches:
    id=101 name="Ivan Petrov" score=92 @ivan [user]
    id=102 name="Ivanovich Ivan" score=85 [user]
    ```
  - Format: `id={entity_id} name="{display_name}" score={score} @{username} [{entity_type}]`
  - Includes @username and entity_type only if available in match

- **Updated tool descriptions** to guide agents:
  - ListMessages: Added "If response is ambiguous (multiple matches), use the numeric id= parameter with the ID from the matches list. For @username lookups, prepend @ to the name: dialog=\"@username\"."
  - SearchMessages: Added "If response is ambiguous, use the numeric ID from the matches list to disambiguate. For @username lookups, prepend @ to the dialog name: dialog=\"@channel_name\"."
  - This ensures agents know to use numeric ID after receiving Candidates, rather than trying names again

**Verification**: 28 tools tests pass
- All existing tests continue to pass
- Candidates output is properly formatted with metadata
- No regression in ListMessages, SearchMessages, GetUserInfo, GetMyAccount behavior

## Test Coverage

**Resolver Tests (22 total)**:
- ✅ Exact match resolution
- ✅ Numeric ID queries (found and not found)
- ✅ Ambiguous matching (multiple fuzzy candidates)
- ✅ Sender resolution
- ✅ Not found cases
- ✅ Below candidate threshold
- ✅ Single low score match returns Candidates (NEW)
- ✅ Multiple low score matches return Candidates
- ✅ Exact match wins over ambiguity
- ✅ Numeric ID in cache resolves (NEW)
- ✅ Numeric ID not found (NEW)
- ✅ @username resolves via cache (NEW)
- ✅ @username not found (NEW)
- ✅ Exact match case-insensitive (NEW)
- ✅ Single fuzzy match returns Candidates (NEW)
- ✅ Multiple fuzzy matches return Candidates (NEW)
- ✅ No fuzzy matches returns NotFound (NEW)
- ✅ Cyrillic transliteration still works (NEW)
- ✅ Candidates include metadata from cache (NEW)
- ✅ Candidates without cache have None metadata (NEW)
- ✅ Exact match among fuzzy returns Resolved (NEW)
- ✅ Resolve without cache still works (NEW)

**Tools Tests (28 total)**:
- All existing tools tests pass
- Candidates formatting verified with proper metadata output
- No regression in any tool behavior

## Key Decisions

1. **Candidates always returned for fuzzy matches**: Changed from auto-resolving single >=90 matches. This ensures agents receive full candidate list for ambiguous inputs, improving disambiguation accuracy.

2. **@username as deterministic case**: Added as Case 2 before fuzzy matching. This allows direct lookup of known usernames without fuzzy approximation.

3. **Exact case-insensitive match priority**: Applied across all hits (not just >=90), so exact matches always resolve even if alternatives score higher.

4. **Metadata fetching in resolver**: Resolver now optionally fetches username and entity_type from cache when building Candidates. This avoids double-lookup in tools.py.

5. **Backward compatible**: Numeric ID resolution and Cyrillic transliteration behavior unchanged. Existing code continues to work.

## Deviations from Plan

None - plan executed exactly as written. All must-haves satisfied:

| Must-Have | Status |
|-----------|--------|
| Numeric ID queries resolve directly to Resolved | ✅ Preserved |
| @username queries resolve via username lookup to Resolved | ✅ NEW |
| Exact case-insensitive string matches resolve to Resolved | ✅ Preserved |
| All ambiguous inputs return Candidates with full metadata | ✅ NEW |
| Candidates include: entity_id, display_name, score, username, entity_type | ✅ NEW |
| Updated tool descriptions instructing agents to use numeric ID | ✅ NEW |
| All tests passing | ✅ 50/50 pass |

## What's Next

The resolver redesign enables:
- **Agent disambiguation**: When agents receive Candidates, they now have full context (ID, username, type) to pick the right entity
- **Numeric ID queries**: Agents can use ID directly to avoid fuzzy matching overhead
- **@username lookups**: Direct username resolution without name approximation
- **Better MCP client integration**: Candidates format aligns with agent decision-making patterns

Future work could include:
- Forum topics support in ListMessages (mentioned in project memory)
- Performance optimization for large contact lists
- Extended metadata in Candidates (e.g., last message timestamp)

## Commits

1. `2699c8a` feat(quick-1): redesign resolver with @username support and extend Candidates metadata
2. `87317f5` feat(quick-1): update tools.py to pass cache to resolve() and document agent behavior

---

## Self-Check: PASSED

✅ Created files: `.planning/quick/1-resolver-redesign/1-SUMMARY.md`
✅ Commits exist:
- `2699c8a` (resolver + tests redesign)
- `87317f5` (tools integration + documentation)

✅ Modified files verified:
- `src/mcp_telegram/resolver.py` — New Candidates structure, @username case, 5-case logic
- `src/mcp_telegram/tools.py` — Cache parameter passed, Candidates formatting, docstrings
- `tests/test_resolver.py` — 22 tests (9 new)

✅ Test results: **50 passed** (22 resolver + 28 tools)
