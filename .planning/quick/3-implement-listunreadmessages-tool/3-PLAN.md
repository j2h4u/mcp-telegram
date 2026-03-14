---
phase: quick
plan: 3
type: execute
wave: 1
depends_on: []
files_modified:
  - src/mcp_telegram/tools.py
  - src/mcp_telegram/capabilities.py
  - src/mcp_telegram/formatter.py
  - tests/test_tools.py
autonomous: true
requirements: []
user_setup: []

must_haves:
  truths:
    - "User can request unread messages grouped by chat with unread/mention counts"
    - "Unread mentions surface at the top of results"
    - "DMs rank above groups in results"
    - "Messages are limited by budget, trimmed proportionally, with '[и ещё N]' marker"
    - "Channels (scope=all only) show count but no messages"
    - "Tool integrates into TOOL_REGISTRY and server.py enumerate"
  artifacts:
    - path: "src/mcp_telegram/tools.py"
      provides: "ListUnreadMessages ToolArgs class, list_unread_messages runner, TOOL_REGISTRY update"
      exports: ["ListUnreadMessages", "list_unread_messages"]
    - path: "src/mcp_telegram/capabilities.py"
      provides: "allocate_message_budget_proportional() capability for trimming logic"
      exports: ["allocate_message_budget_proportional"]
    - path: "src/mcp_telegram/formatter.py"
      provides: "format_unread_messages_grouped() helper for per-chat message formatting"
      exports: ["format_unread_messages_grouped"]
    - path: "tests/test_tools.py"
      provides: "test_list_unread_messages_* tests covering scope, budget allocation, channel handling"
  key_links:
    - from: "src/mcp_telegram/tools.py"
      to: "src/mcp_telegram/capabilities.py"
      via: "allocate_message_budget_proportional(unread_counts, limit)"
      pattern: "allocate_message_budget_proportional"
    - from: "src/mcp_telegram/tools.py"
      to: "src/mcp_telegram/formatter.py"
      via: "format_unread_messages_grouped(chat_data, tz)"
      pattern: "format_unread_messages_grouped"
    - from: "src/mcp_telegram/tools.py"
      to: "telethon.iter_dialogs"
      via: "dialog.unread_count, dialog.unread_mentions_count, dialog.entity.participants_count"
      pattern: "iter_dialogs"
---

<objective>
Implement ListUnreadMessages tool for mcp-telegram — allows LLM to fetch unread messages grouped by chat, with smart budget allocation and mention prioritization.

Purpose: Enable morning unread DM workflow without manually enumerating each chat
Output: Tool callable via MCP, tested, integrated into server
</objective>

<execution_context>
@/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py
@/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/capabilities.py
@/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py
@/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py
</execution_context>

<context>
## Design Spec (Finalized)

**Parameters:**
- `scope`: "personal" (default) — DMs + groups ≤ threshold | "all" — everything
- `limit`: global message budget (default 100)
- `group_size_threshold`: member count threshold for "small groups" (default 100)

**Sort order:**
1. Chats with unread_mentions_count > 0 — top
2. DMs above groups
3. Within each tier — by last message time (newest first)

**Budget distribution:**
- Total ≤ limit messages
- If fits — show all
- If over — proportional trim, min 3 per chat, newest shown, "[и ещё N]" marker

**Output format (grouped by chat):**
```
--- Брат (3 непрочитанных, id=432061315) ---
14:22 Дима: Ты когда приедешь?
14:23 Дима: Мама спрашивает
14:25 Дима: [фото]

--- Рабочий чат (12 непрочитанных, 2 упоминания, id=-1001380818387) ---
09:41 Олег: @Макс глянь PR
09:42 Олег: Там несложно
09:50 Вика: @Макс +1, было бы круто сегодня
[и ещё 9]
```

**Channels (scope="all" only):** show only count, no messages
**Empty result:** short message + suggest broader scope
**Muted chats:** included
**No navigation token** — drill-down via ListMessages(exact_dialog_id=...)
**Read receipts:** not set (read-only server)

## Architecture Notes

**Key patterns from existing tools:**
- ToolArgs as Pydantic BaseModel (see ListMessages, SearchMessages)
- @tool_runner.register singledispatch decorator
- @_track_tool_telemetry("ToolName") for analytics
- ToolResult(content=_text_response(text), result_count=N)
- Telethon iter_dialogs iteration with batch cache updates
- format_messages(messages, reply_map, tz) reused for message formatting

**New capability placement:**
- `allocate_message_budget_proportional()` → capabilities.py (shared, testable)
- `format_unread_messages_grouped()` → formatter.py (output formatting)

**Telethon API:**
- dialog.unread_count — total unread messages
- dialog.unread_mentions_count — number of @mentions
- dialog.is_user, dialog.is_group, dialog.is_channel — type check
- dialog.entity.participants_count — group/channel member count (None for DMs)
- dialog.date — last message timestamp
</context>

<tasks>

<task type="auto">
  <name>Task 1: Implement budget allocation and formatting helpers in capabilities.py + formatter.py</name>
  <files>
    src/mcp_telegram/capabilities.py
    src/mcp_telegram/formatter.py
  </files>
  <action>
**In capabilities.py:**

Add function: `allocate_message_budget_proportional(unread_counts: dict[int, int], limit: int, min_per_chat: int = 3) -> dict[int, int]`

Algorithm:
1. Take input dict: {chat_id: unread_count}
2. If sum(unread_counts.values()) ≤ limit → return unread_counts unchanged (no trim needed)
3. If over limit:
   - Allocate min_per_chat to each chat first (reserve budget)
   - Distribute remaining budget proportionally by unread_count
   - Round down, cap each at its unread_count
   - Return allocation dict {chat_id: budget_for_chat}

**In formatter.py:**

Add function: `format_unread_messages_grouped(chats_data: list[dict], tz: ZoneInfo | None = None) -> str`

Input: list of dicts, each containing:
```python
{
  "chat_id": int,
  "display_name": str,
  "unread_count": int,
  "unread_mentions_count": int,
  "messages": list[MessageLike],  # Already sorted by time within chat
  "budget_for_chat": int,         # Allocated budget
  "total_in_chat": int,           # Real total unread in chat
}
```

Logic:
- For each chat, output header: `--- {display_name} ({unread_count} непрочитанных{, N упоминания}{, id={chat_id}) ---`
- Format first `budget_for_chat` messages using format_messages() (reuse existing)
- If `budget_for_chat < total_in_chat`: append `[и ещё {total_in_chat - budget_for_chat}]` line
- For channels: skip message lines, just show header

Return: \n-joined sections for all chats
  </action>
  <verify>
    <automated>python -m pytest tests/test_capabilities.py::test_allocate_budget_no_trim -xvs && python -m pytest tests/test_capabilities.py::test_allocate_budget_proportional_trim -xvs && python -m pytest tests/test_formatter.py::test_format_unread_grouped -xvs</automated>
  </verify>
  <done>
    - `allocate_message_budget_proportional()` implemented, proportional allocation working, min_per_chat respected
    - `format_unread_messages_grouped()` implemented, header format correct, "[и ещё N]" marker shows when budget < total
    - Tests pass (no trim case, proportional trim case, formatter case)
  </done>
</task>

<task type="auto">
  <name>Task 2: Implement ListUnreadMessages ToolArgs + list_unread_messages runner in tools.py</name>
  <files>
    src/mcp_telegram/tools.py
  </files>
  <action>
**Add ToolArgs class:**

```python
class ListUnreadMessages(ToolArgs):
    """Fetch unread messages from personal chats and small groups, sorted by mentions then recency.

    Surfaces @mentions at the top, groups DMs above group chats, and intelligently allocates
    a per-chat message budget to prevent flooding when many chats have unread messages.

    Use scope="personal" (default) to see only DMs and small groups (≤ group_size_threshold members).
    Use scope="all" to include large groups and channels (shows counts only, no messages).
    Use limit to control total messages (default 100, minimum across all chats).
    """

    scope: Literal["personal", "all"] = Field(
        default="personal",
        description="'personal' (DMs + small groups) or 'all' (everything)"
    )
    limit: int = Field(
        default=100,
        ge=50,
        le=500,
        description="Total message budget across all chats (50-500)"
    )
    group_size_threshold: int = Field(
        default=100,
        ge=10,
        description="Group member count above which to hide messages (scope=personal only)"
    )
```

**Add runner function:**

Implement: `async def list_unread_messages(args: ListUnreadMessages) -> ToolResult`

Steps:
1. Connect client: `async with connected_client() as client:`
2. Iterate dialogs, collect unread chats:
   - Skip archived (include like ListDialogs does)
   - Filter by scope: "personal" → skip channels, skip groups with participants_count > threshold
   - Track: chat_id, name, unread_count, unread_mentions_count, type, last_message_date
3. Sort by: (unread_mentions_count > 0 DESC, is_user DESC, last_message_date DESC)
4. For each chat (in order), fetch unread messages:
   - Use client.iter_messages(entity, unread=True, limit=large_number) to get candidates
   - Cache dialog entry via _cache_dialog_entry()
5. Call allocate_message_budget_proportional(unread_counts_dict, args.limit)
6. Trim messages per allocation, build chats_data list (see formatter.py input spec)
7. Call format_unread_messages_grouped(chats_data, tz=...)
8. Return ToolResult(content=_text_response(result_text), result_count=total_messages_shown)
9. If no unread chats: return ToolResult(content=_text_response("No unread messages. Try scope=\"all\" to see everything."))

**Add to TOOL_REGISTRY:**
- Update line 1701 (after GetUserInfo) to include: `"ListUnreadMessages": ListUnreadMessages,`

**Add to TOOL_POSTURE:**
- Add entry: `"ListUnreadMessages": "primary"` (alongside ListMessages)

**Decorator chain (before function definition):**
```python
@tool_runner.register
@_track_tool_telemetry("ListUnreadMessages")
async def list_unread_messages(args: ListUnreadMessages) -> ToolResult:
```
  </action>
  <verify>
    <automated>python -c "from mcp_telegram.tools import ListUnreadMessages, list_unread_messages, TOOL_REGISTRY; assert 'ListUnreadMessages' in TOOL_REGISTRY; assert TOOL_REGISTRY['ListUnreadMessages'] == ListUnreadMessages" && python -m pytest tests/test_tools.py::test_list_unread_personal_scope -xvs && python -m pytest tests/test_tools.py::test_list_unread_mentions_top -xvs</automated>
  </verify>
  <done>
    - ListUnreadMessages class defined with correct parameters and docstring
    - list_unread_messages runner implemented, handles scope filtering, sort order, budget allocation
    - Added to TOOL_REGISTRY and TOOL_POSTURE
    - Import chain correct (no circular deps)
    - Tests pass (scope filtering, mention sorting, budget allocation in real runner)
  </done>
</task>

<task type="auto">
  <name>Task 3: Add comprehensive test coverage for ListUnreadMessages</name>
  <files>
    tests/test_tools.py
    tests/test_capabilities.py
  </files>
  <action>
**In tests/test_capabilities.py (or create if doesn't exist):**

Add tests:
- `test_allocate_budget_no_trim()` — input ≤ limit, returns unchanged
- `test_allocate_budget_proportional_trim()` — proportional allocation, each ≥ min_per_chat, sum ≤ limit
- `test_allocate_budget_min_per_chat_respected()` — even small counts get min allocation

**In tests/test_tools.py:**

Add tests:
- `test_list_unread_personal_scope_filters_groups()` — groups over threshold hidden
- `test_list_unread_mentions_surface_top()` — unread_mentions_count > 0 chats appear first
- `test_list_unread_dms_above_groups()` — within mentions/no-mentions tiers, DMs before groups
- `test_list_unread_channels_scope_all_count_only()` — scope="all", channels show count, no messages
- `test_list_unread_empty_suggests_scope()` — no unread, returns helpful empty state
- `test_list_unread_budget_allocation()` — respects limit, shows "[и ещё N]" when trimmed
- `test_list_unread_result_format()` — output includes chat name, unread count, message format

**Test helpers:**

Reuse existing patterns from test_tools.py:
- `_make_dialog()` to create mock dialogs
- `_async_iter()` to yield dialogs
- Mock client.iter_dialogs, client.iter_messages
- Mock cache via monkeypatch

Mocks should include:
- Dialogs with varied unread_count, unread_mentions_count, type (user/group/channel)
- Messages with id, date, sender, message text
- Entity with participants_count and username
  </action>
  <verify>
    <automated>python -m pytest tests/test_tools.py::test_list_unread_personal_scope_filters_groups -xvs && python -m pytest tests/test_tools.py::test_list_unread_mentions_surface_top -xvs && python -m pytest tests/test_tools.py::test_list_unread_budget_allocation -xvs && python -m pytest tests/test_capabilities.py::test_allocate_budget_proportional_trim -xvs</automated>
  </verify>
  <done>
    - All tests pass (scope filtering, mention ordering, budget allocation, channel handling, output format)
    - Test coverage ≥ 90% for new functions
    - Edge cases covered (empty unread, single chat, multi-chat with varied unread counts)
  </done>
</task>

</tasks>

<verification>
After all tasks complete, verify:

1. **Import verification:**
   ```bash
   python -c "from mcp_telegram.tools import ListUnreadMessages; print('✓ ListUnreadMessages importable')"
   python -c "from mcp_telegram.tools import TOOL_REGISTRY; assert 'ListUnreadMessages' in TOOL_REGISTRY; print('✓ TOOL_REGISTRY updated')"
   python -c "from mcp_telegram.capabilities import allocate_message_budget_proportional; print('✓ allocate_message_budget_proportional importable')"
   python -c "from mcp_telegram.formatter import format_unread_messages_grouped; print('✓ format_unread_messages_grouped importable')"
   ```

2. **Tool registry validation:**
   ```bash
   python -c "from mcp_telegram.tools import verify_tool_registry; verify_tool_registry(); print('✓ Tool registry valid')"
   ```

3. **Full test suite:**
   ```bash
   cd /home/j2h4u/repos/j2h4u/mcp-telegram && python -m pytest tests/test_tools.py tests/test_capabilities.py -v --tb=short
   ```

4. **Server integration check:**
   ```bash
   grep -n "ListUnreadMessages" src/mcp_telegram/server.py
   ```
   (Server.py enumerate should include ListUnreadMessages tool definition. If missing, add it following the pattern of existing tools.)

5. **Manual end-to-end:**
   - Build docker image: `cd /opt/docker/mcp-telegram && docker compose up -d --build`
   - Check healthcheck: `curl -s http://localhost:3100/health | jq .status`
   - Call tool via MCP client (if available): test ListUnreadMessages with scope="personal" and "all"
</verification>

<success_criteria>
- ListUnreadMessages ToolArgs defined with scope, limit, group_size_threshold parameters
- list_unread_messages runner implemented, registered, telemetry tracked
- allocate_message_budget_proportional() in capabilities.py, handles proportional allocation + min_per_chat
- format_unread_messages_grouped() in formatter.py, outputs grouped chats with "[и ещё N]" marker
- Tests pass: budget allocation, scope filtering, mention sorting, DM/group ordering, output format
- TOOL_REGISTRY includes ListUnreadMessages
- TOOL_POSTURE includes ListUnreadMessages: "primary"
- Server.py enumerate includes tool definition (if using mcp.server enumerate pattern)
- No circular imports, no new external dependencies
</success_criteria>

<output>
After completion, create `.planning/quick/3-implement-listunreadmessages-tool/3-SUMMARY.md` with:
- What was implemented (tool class, runner, helpers, tests)
- Test coverage summary
- Key decisions (budget allocation logic, sort order implementation)
- Any deviations from spec or blockers encountered
- Next steps (deploy, morning workflow testing)
</output>
