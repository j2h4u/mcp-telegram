# Phase 19: Dialog Metadata Enrichment - Context

**Gathered:** 2026-03-20
**Status:** Ready for planning

<domain>
## Phase Boundary

ListDialogs surfaces `members=N` for groups/channels and `created=YYYY-MM-DD` for groups/channels. Private chats omit both fields. Code is already implemented — phase scope is test coverage and commit.

</domain>

<decisions>
## Implementation Decisions

### Implementation status
- META-01 and META-02 are already coded in `src/mcp_telegram/tools/discovery.py:51-56`
- `participants_count` read via `getattr(entity, "participants_count", None)` — appended only when not None
- `created` read via `getattr(entity, "date", None)` — formatted as `%Y-%m-%d`, appended only when not None
- Private chats naturally omit both (User entities lack these attributes)

### Test coverage needed
- No existing tests verify `members=` or `created=` in ListDialogs output
- Tests should cover: group with members/created, channel with members/created, private chat without, null entity, null participants_count, null date

### Tool description
- Claude's Discretion: whether to update ListDialogs docstring to mention members/created fields

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Requirements
- `.planning/REQUIREMENTS.md` — META-01 and META-02 definitions (lines 53-54)

### Implementation (already done)
- `src/mcp_telegram/tools/discovery.py` — ListDialogs with members/created fields (lines 51-56)
- `src/mcp_telegram/capability_unread.py` — participants_count usage pattern (line 79)

### Existing test patterns
- `tests/test_tools.py` — ListDialogs test fixtures and mock patterns (test_list_dialogs_type_field, test_list_dialogs_null_date)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `tests/test_tools.py` mock_client/mock_cache fixtures — standard pattern for all tool tests
- `classify_dialog()` from `dialog_target.py` — already returns user/group/channel types
- Existing `test_list_dialogs_type_field` test — demonstrates how to mock dialog entities with type/date fields

### Established Patterns
- `getattr(entity, "attr", None)` with None guard — consistent safe access pattern
- Output format: `key=value` space-separated on single line per dialog
- Tests use `monkeypatch` on `connected_client` context manager

### Integration Points
- `discovery.py:list_dialogs()` — the function under test
- `capability_unread.py` — already uses `participants_count` for group filtering (validates the attribute exists on entities)

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope

</deferred>

---

*Phase: 19-dialog-metadata-enrichment*
*Context gathered: 2026-03-20*
