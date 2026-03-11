---
phase: quick
plan: 1-resolver-redesign
type: execute
wave: 1
depends_on: []
files_modified:
  - src/mcp_telegram/resolver.py
  - src/mcp_telegram/tools.py
autonomous: true
requirements: []
user_setup: []

must_haves:
  truths:
    - "Numeric ID queries resolve directly to Resolved"
    - "@username queries resolve via username lookup to Resolved"
    - "Exact case-insensitive string matches resolve to Resolved"
    - "All ambiguous inputs (fuzzy matches >=60, no exact match) return Candidates with full metadata"
    - "Candidates include: entity_id, display_name, score, username (if available), entity_type (if available)"
  artifacts:
    - path: "src/mcp_telegram/resolver.py"
      provides: "Updated resolve() function with deterministic logic and username lookup"
    - path: "src/mcp_telegram/tools.py"
      provides: "Updated tool descriptions instructing agents to use numeric ID after receiving Candidates"
  key_links:
    - from: "resolver.py:resolve()"
      to: "cache.py:EntityCache.all_names_with_ttl()"
      via: "choices dict {entity_id: name}"
      pattern: "resolve(query, cache.all_names_with_ttl(...))"
    - from: "tools.py:list_messages()/search_messages()"
      to: "resolver.py:Candidates"
      via: "returning candidate matches with numeric IDs for agent disambiguation"
      pattern: "if isinstance(result, Candidates):.*result.matches"
---

<objective>
Redesign resolver logic to deterministically resolve specific input types (numeric ID, @username, exact string match) to Resolved, and reserve Candidates for all ambiguous cases. Ensure Candidates include full metadata needed for agent disambiguation.

Purpose: Agents can reliably resolve exact matches and lookup by numeric ID without hunting through multiple candidates. Deterministic resolution improves task success rate.

Output:
- Updated resolver.py with 5-case resolution logic
- Updated tool descriptions directing agents to use numeric ID after disambiguation
- Candidates dataclass extended with username and entity_type fields
- All tests passing
</objective>

<execution_context>
@/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/resolver.py
@/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/cache.py
@/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py
@/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_resolver.py
</execution_context>

<context>
## Current Resolver Behavior
- Numeric ID query (isdigit) → Resolved/NotFound
- Single fuzzy match >=90 → Resolved (problematic: can auto-resolve on typos)
- Multiple fuzzy matches >=90 + exact match among them → Resolved
- Fuzzy matches 60-89 → Candidates
- Cyrillic → retry with transliteration if first attempt fails

## Target Behavior
1. **Numeric ID query** → Resolved/NotFound (existing, keep)
2. **@username query** (starts with @) → lookup by username in cache, Resolved/NotFound (NEW)
3. **Exact case-insensitive string match** → Resolved (keep from current lines 75-79)
4. **All fuzzy matches** (>= 60) → always Candidates (NEW: don't auto-resolve single fuzzy hit >=90)
5. **Candidates dataclass** → extend with username and entity_type (NEW)

## Cache Context
`EntityCache` already stores username and entity_type in the database (cache.py lines 32-44, 46-65):
- upsert(entity_id, entity_type, name, username)
- get(entity_id, ttl_seconds) returns dict with keys: id, type, name, username

For Candidates, we need to retrieve these from cache after resolving entity_id:
- After resolve(), if Candidates returned, caller (tools.py) will need to fetch metadata from cache to populate username/type
- OR: resolver.py can take cache as an optional parameter and fetch metadata itself

**Decision (executor discretion):** Since resolve() is pure name-matching logic, keep it decoupled from cache. Instead, update Candidates dataclass to include username and entity_type fields (optional), and update tools.py to populate them after getting Candidates.
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Redesign resolver.py with new resolution cases and extend Candidates dataclass</name>
  <files>
    src/mcp_telegram/resolver.py
    tests/test_resolver.py
  </files>
  <behavior>
    Test case 1: Numeric ID query "12345" exists in cache → Resolved(entity_id=12345, display_name=name)
    Test case 2: Numeric ID query "99999" not in cache → NotFound(query="99999")
    Test case 3: @username query "@alice" exists in cache → Resolved(entity_id=xxx, display_name="Alice Name")
    Test case 4: @username query "@notfound" not in cache → NotFound(query="@notfound")
    Test case 5: Exact case-insensitive match "Bob" among [{"Bob", score=100}, {"Bobby", score=95}] → Resolved(entity_id=bob_id, display_name="Bob")
    Test case 6: Single fuzzy match score=92 → Candidates (NOT Resolved)
    Test case 7: Multiple fuzzy matches all >=60, no exact match → Candidates with all matches
    Test case 8: No matches >=60 → NotFound
    Test case 9: Cyrillic query with transliteration fallback still works (existing behavior preserved)
  </behavior>
  <action>
**Step 1: Extend Candidates dataclass**
Update resolver.py Candidates class (line 29-31) to include optional username and entity_type:
```python
@dataclass(frozen=True)
class Candidates:
    query: str
    matches: list[dict]  # Changed from tuple to dict for clarity
    # Each dict: {entity_id: int, display_name: str, score: int, username: str|None, entity_type: str|None}
```

**Step 2: Update resolve() signature to accept cache**
Update resolve() function (line 89) to accept optional EntityCache parameter:
```python
def resolve(query: str, choices: dict[int, str], cache: EntityCache | None = None) -> ResolveResult:
```
This allows resolver to fetch metadata (username, entity_type) when building Candidates.

**Step 3: Add @username resolution**
Before fuzzy matching (after numeric check, line 98-102), add:
```python
if query.startswith("@") and cache:
    username_query = query[1:]  # Strip @
    # Search cache for entity with matching username
    rows = cache._conn.execute(
        "SELECT id, name FROM entities WHERE username = ?",
        (username_query,)
    ).fetchone()
    if rows:
        entity_id, name = rows
        return Resolved(entity_id=entity_id, display_name=name)
    return NotFound(query=query)
```

**Step 4: Redesign fuzzy resolution**
Replace _fuzzy_resolve() logic (lines 53-86) to:
- Extract all hits >=60 into matches list
- Apply exact case-insensitive filter (existing logic lines 75-79)
- If exact match found → return Resolved
- Otherwise → always return Candidates (even if single fuzzy hit >=90)

Updated _fuzzy_resolve() should return: ResolveResult directly (not accumulate above_auto separately)

Pseudo-code:
```python
def _fuzzy_resolve(query: str, choices: dict[int, str], cache: EntityCache | None = None) -> ResolveResult:
    name_to_id = {name: eid for eid, name in choices.items()}
    hits = process.extract(query, name_to_id.keys(), scorer=fuzz.WRatio, ...)  # >=CANDIDATE_THRESHOLD
    if not hits:
        return NotFound(query=query)

    # Check for exact case-insensitive match
    query_lower = query.lower().strip()
    for name, score, _idx in hits:
        if name.lower().strip() == query_lower:
            return Resolved(entity_id=name_to_id[name], display_name=name)

    # No exact match → return all hits as Candidates
    matches = []
    for name, score, _idx in hits:
        entity_id = name_to_id[name]
        entity_info = {
            "entity_id": entity_id,
            "display_name": name,
            "score": int(score),
            "username": None,
            "entity_type": None,
        }
        # Fetch metadata from cache if available
        if cache:
            cached = cache.get(entity_id, ttl_seconds=0)  # ttl_seconds=0 to get any cached entry
            if cached:
                entity_info["username"] = cached.get("username")
                entity_info["entity_type"] = cached.get("type")
        matches.append(entity_info)

    return Candidates(query=query, matches=matches)
```

**Step 5: Write comprehensive tests**
In tests/test_resolver.py, create tests for all 9 behavior cases above.
- Use mock cache for @username tests
- Verify Candidates structure includes username and entity_type fields
- Verify single fuzzy hit (score=92) returns Candidates not Resolved
- Verify exact match always returns Resolved even if lower score fuzzy alternatives exist

**Step 6: Verify backward compatibility**
- Numeric ID resolution: unchanged
- Cyrillic transliteration: still retried on NotFound
- Existing fuzzy logic: preserved for exact matching
  </action>
  <verify>
    <automated>cd /home/j2h4u/repos/j2h4u/mcp-telegram && python -m pytest tests/test_resolver.py -v</automated>
  </verify>
  <done>
    - resolver.py resolve() accepts optional cache parameter
    - @username queries resolve via cache lookup
    - Exact string matches return Resolved
    - All fuzzy matches return Candidates (even single hit >=90)
    - Candidates include entity_id, display_name, score, username, entity_type
    - All 9 test cases pass
    - Numeric ID and transliteration behavior unchanged
  </done>
</task>

<task type="auto">
  <name>Task 2: Update tools.py to pass cache to resolve() and document agent behavior in tool descriptions</name>
  <files>
    src/mcp_telegram/tools.py
  </files>
  <action>
**Step 1: Update resolve() calls to pass cache**
In tools.py, find all 3 calls to resolve():
1. ListMessages line 185: `resolve(args.dialog, cache.all_names_with_ttl(...))`
2. ListMessages line 212: `resolve(args.sender, cache.all_names_with_ttl(...))`
3. SearchMessages line 327: `resolve(args.dialog, cache.all_names_with_ttl(...))`

Update each to pass cache as third parameter:
```python
result = resolve(args.dialog, cache.all_names_with_ttl(USER_TTL, GROUP_TTL), cache)
```

**Step 2: Update Candidates handling to format with metadata**
When returning Candidates (lines 189-190, 215-217, 330-332), update format to include username and entity_type from matches:
```python
if isinstance(result, Candidates):
    match_lines = []
    for match in result.matches:
        line = f'id={match["entity_id"]} name="{match["display_name"]}" score={match["score"]}'
        if match.get("username"):
            line += f' @{match["username"]}'
        if match.get("entity_type"):
            line += f' [{match["entity_type"]}]'
        match_lines.append(line)
    return [TextContent(type="text", text=f'Ambiguous "{args.dialog}". Matches:\n' + "\n".join(match_lines))]
```

**Step 3: Update tool descriptions**
Update docstrings for ListMessages and SearchMessages to instruct agents on disambiguation:

For ListMessages (line 160-167):
```python
"""
List messages in a dialog by name. Returns messages newest-first in human-readable format
(HH:mm FirstName: text) with date headers and session breaks.

Use cursor= with the next_cursor token from a previous response to page back in time.
Use sender= to filter messages from a specific person (name string, resolved via fuzzy match).
Use unread=True to show only messages you haven't read yet.

If response is ambiguous (multiple matches), use the numeric id= parameter with the ID from the matches list.
For @username lookups, prepend @ to the name: dialog="@username".
"""
```

For SearchMessages (line 306-311):
```python
"""
Search messages in a dialog by text query. Returns matching messages newest to oldest.

Use offset= with the next_offset value from a previous response to get the next page.

If response is ambiguous, use the numeric ID from the matches list to disambiguate.
For @username lookups, prepend @ to the dialog name: dialog="@channel_name".
"""
```

**Step 4: Run full integration test**
Verify that ListMessages and SearchMessages can:
- Accept @username queries and resolve them
- Return Candidates with full metadata when ambiguous
- Accept numeric ID for disambiguation
  </action>
  <verify>
    <automated>cd /home/j2h4u/repos/j2h4u/mcp-telegram && python -m pytest tests/test_tools.py::test_list_messages -v && python -m pytest tests/test_tools.py::test_search_messages -v</automated>
  </verify>
  <done>
    - All resolve() calls in tools.py pass cache parameter
    - Candidates output includes entity_id, username, entity_type
    - ListMessages and SearchMessages docstrings explain @username and ID disambiguation
    - Tools accept numeric ID queries and resolve them directly
    - Integration tests pass
  </done>
</task>

</tasks>

<verification>
Run full test suite to verify resolver redesign:

```bash
cd /home/j2h4u/repos/j2h4u/mcp-telegram
python -m pytest tests/test_resolver.py tests/test_tools.py -v --tb=short
```

Verify manually:
1. Call ListMessages with ambiguous name → receive Candidates with numeric IDs
2. Call ListMessages with numeric ID → directly resolve (no ambiguity)
3. Call ListMessages with @username → resolve via username lookup
4. Call SearchMessages with @channel → resolve directly
</verification>

<success_criteria>
- All test cases for resolver redesign pass
- resolve() accepts optional cache parameter
- Numeric ID queries: Resolved/NotFound (unchanged)
- @username queries: Resolved via username lookup (NEW)
- Exact string match: Resolved (unchanged)
- Fuzzy matches: always Candidates (CHANGED from auto-resolve >=90)
- Candidates include full metadata: entity_id, display_name, score, username, entity_type
- Tool descriptions guide agents on ID disambiguation after receiving Candidates
- No regression in existing behavior (numeric ID, transliteration)
</success_criteria>

<output>
After completion, create `.planning/quick/1-resolver-redesign/1-SUMMARY.md`
</output>
