---
phase: quick
plan: 2
type: execute
wave: 1
depends_on: []
files_modified:
  - src/mcp_telegram/server.py
  - src/mcp_telegram/resolver.py
  - src/mcp_telegram/cache.py
  - src/mcp_telegram/tools.py
  - src/mcp_telegram/pagination.py
autonomous: true
requirements: []
---

<objective>
Fix critical issues from python-code-reviewer audit: error handling, API encapsulation, type detection, and code quality issues.

Purpose: Resolve traceback suppression, private API access, sender type misdetection, and minor code quality debt
Output: All issues fixed, tests pass
</objective>

<execution_context>
@/home/j2h4u/.claude/get-shit-done/workflows/execute-plan.md
</execution_context>

<context>
@.planning/STATE.md
@./MEMORY.md
</context>

<tasks>

<task type="auto">
  <name>Task 1: Fix critical error handling and API encapsulation</name>
  <files>src/mcp_telegram/server.py, src/mcp_telegram/resolver.py, src/mcp_telegram/cache.py</files>
  <action>
Three critical fixes:

1. **server.py:92** — Replace `raise RuntimeError(...) from None` with `from e` to preserve traceback for debugging:
   ```python
   # BEFORE:
   raise RuntimeError(f"Tool {name} failed") from None

   # AFTER:
   raise RuntimeError(f"Tool {name} failed") from e
   ```

2. **resolver.py:136-145** — Replace direct `cache._conn` access with public method call. Add `get_by_username(username: str)` method to `EntityCache`:
   ```python
   # In cache.py, add after all_names_with_ttl():
   def get_by_username(self, username: str) -> tuple[int, str] | None:
       """Return (entity_id, name) for entity with matching username, or None."""
       row = self._conn.execute(
           "SELECT id, name FROM entities WHERE username = ?",
           (username,)
       ).fetchone()
       return row if row else None
   ```

   Then in resolver.py:136-145, replace:
   ```python
   # BEFORE:
   rows = cache._conn.execute(
       "SELECT id, name FROM entities WHERE username = ?",
       (username_query,)
   ).fetchone()
   if rows:
       entity_id, name = rows

   # AFTER:
   result = cache.get_by_username(username_query)
   if result:
       entity_id, name = result
   ```

3. **resolver.py:99** — Fix `ttl_seconds=0` (always invalidates) to proper TTL. Change to `ttl_seconds=300` (5-min cache for metadata lookup during resolution):
   ```python
   # BEFORE (line 99):
   cached = cache.get(entity_id, ttl_seconds=0)

   # AFTER:
   cached = cache.get(entity_id, ttl_seconds=300)  # 5-min TTL for metadata
   ```

Verification: All changes preserve current behavior while fixing encapsulation and error propagation.
  </action>
  <verify>
    <automated>cd /home/j2h4u/repos/j2h4u/mcp-telegram && python -m pytest tests/ -v 2>&1 | grep -E "(PASSED|FAILED|ERROR|test_)" | head -50</automated>
  </verify>
  <done>
    - server.py:92 raises RuntimeError from e (traceback preserved)
    - resolver.py uses cache.get_by_username() instead of direct _conn access
    - cache.py exports public get_by_username() method
    - resolver.py:99 uses ttl_seconds=300 instead of 0
    - All tests pass
  </done>
</task>

<task type="auto">
  <name>Task 2: Fix sender type detection and code quality issues</name>
  <files>src/mcp_telegram/tools.py, src/mcp_telegram/pagination.py</files>
  <action>
Multiple quality fixes in tools.py and pagination.py:

1. **tools.py:257, 390** — Replace fragile `"user" if first_name else "group"` with proper isinstance() checks for Channel/Chat types:
   Extract sender type detection into a helper function (DRY principle):
   ```python
   # Add after get_entity_cache() function:
   def _get_sender_type(sender: t.Any) -> str:
       """Determine sender type from Telethon entity instance."""
       if isinstance(sender, Channel):
           return "channel"
       elif isinstance(sender, Chat):
           return "group"
       else:
           return "user"
   ```

   Then replace both occurrences (lines 257 and 390):
   ```python
   # BEFORE:
   sender_type = "user" if getattr(sender, "first_name", None) else "group"

   # AFTER:
   sender_type = _get_sender_type(sender)
   ```

2. **tools.py:286 vs 422** — Make client RPC calls consistent. Change line 422 from `await client.__call__(...)` to `await client(...)`:
   ```python
   # BEFORE (line 422):
   rl = await client.__call__(GetMessageReactionsListRequest(...))

   # AFTER:
   rl = await client(GetMessageReactionsListRequest(...))
   ```
   This matches the pattern on line 286 and is more Pythonic.

3. **cache.py:46, 72** — Remove unused `entity_id` variable from unpacking:
   ```python
   # Line 46, BEFORE:
   entity_id_db, entity_type, name, username, updated_at = row

   # Line 46, AFTER (use _ for unused):
   _, entity_type, name, username, updated_at = row

   # Similar fix at line 72 if it has the same pattern (check while editing)
   ```

4. **server.py:30** — Fix return type annotation on enumerate_available_tools. Current return type says Generator but should be list-like:
   ```python
   # BEFORE:
   def enumerate_available_tools() -> t.Generator[tuple[str, Tool], t.Any, None]:

   # AFTER:
   def enumerate_available_tools() -> list[tuple[str, Tool]]:
   ```
   And change body to return list instead of yielding:
   ```python
   # Build from loop instead of yielding
   tools_list = []
   for _, tool_args in inspect.getmembers(tools, inspect.isclass):
       if issubclass(tool_args, tools.ToolArgs) and tool_args != tools.ToolArgs:
           logger.debug("Found tool: %s", tool_args)
           description = tools.tool_description(tool_args)
           tools_list.append((description.name, description))
   return tools_list
   ```

5. **server.py:67** — Fix typo `pogress` → `progress`:
   ```python
   # BEFORE:
   async def progress_notification(pogress: str | int, p: float, s: float | None) -> None:

   # AFTER:
   async def progress_notification(progress: str | int, p: float, s: float | None) -> None:
   ```

6. **server.py** — Remove dead stubs (list_prompts, list_resources, list_resource_templates, progress_notification) if they're not MCP-required. Check if they're decorators or just empty implementations, then decide whether to keep or remove. Likely these should be removed since they return empty lists.

7. **pagination.py:18** — Verify json.loads error handling. The decode_cursor() function should catch JSONDecodeError:
   ```python
   # Check current code around line 18 and wrap in try/except if needed:
   def decode_cursor(token: str, expected_dialog_id: int) -> int:
       try:
           data = json.loads(base64.urlsafe_b64decode(token.encode()))
       except (json.JSONDecodeError, ValueError, binascii.Error) as e:
           raise ValueError(f"Invalid cursor token: {e}") from e
       ...
   ```
   But first check if this is already handled in tools.py callers (per STATE.md note).
  </action>
  <verify>
    <automated>cd /home/j2h4u/repos/j2h4u/mcp-telegram && python -m pytest tests/ -xvs 2>&1 | tail -30</automated>
  </verify>
  <done>
    - Sender type detection uses isinstance() instead of first_name heuristic
    - _get_sender_type() helper extracts DRY logic
    - client() calls are consistent throughout tools.py
    - Unused variables removed (entity_id)
    - enumerate_available_tools() return type matches implementation
    - Typo pogress → progress fixed
    - Dead stubs removed or documented
    - pagination.py error handling confirmed/added
    - All tests pass
  </done>
</task>

</tasks>

<verification>
After both tasks complete:
1. Run full test suite: `pytest tests/ -v`
2. Check imports: Verify Channel, Chat are imported in tools.py (already imported from telethon.tl.types)
3. Verify no new linting errors: `ruff check src/mcp_telegram/`
4. Manual review: Spot-check resolver.py and cache.py for correct method names and signatures
</verification>

<success_criteria>
- All pytest tests pass
- No import errors or AttributeError for new methods
- Code conforms to existing project style
- Traceback preserved in exception handling
- Sender type detection handles Channel/Chat/User correctly
- No unused variable warnings
- Return type annotations accurate
</success_criteria>

<output>
After completion, commit changes:
```
git add src/mcp_telegram/*.py
git commit -m "fix: code review fixes (error handling, API encapsulation, type detection)

- server.py:92: Preserve traceback (from e instead of from None)
- resolver.py: Use public cache.get_by_username() instead of direct _conn access
- cache.py: Add get_by_username() public method
- resolver.py:99: Fix ttl_seconds=0 to 300 (5-min cache)
- tools.py: Extract _get_sender_type() helper, use isinstance() checks
- tools.py: Consistent client() calls (not client.__call__())
- cache.py: Remove unused entity_id unpacking
- server.py: Fix enumerate_available_tools() return type
- server.py:67: Fix typo pogress → progress
- pagination.py: Verify/add JSONDecodeError handling"
```
</output>
