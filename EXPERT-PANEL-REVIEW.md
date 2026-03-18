# Expert Panel Code Review — MCP Telegram Server

Date: 2026-03-15

## Panel Composition

- **System Architect** — long-term health, module boundaries, dependency graph
- **Kaizen Master** — simplicity, YAGNI, duplication elimination
- **QA Engineer** — consistency, correctness, edge cases
- **Team Lead** — maintainability, onboarding cost, practical priorities

---

## System Architect

### Assessment
The module split is reasonable (tools, capabilities, resolver, cache, formatter, pagination, analytics, errors, server, telegram). However, `capabilities.py` at ~1900 lines is doing too much — it's the "everything that isn't a tool definition" dumping ground. The `tools.py` → `capabilities.py` relationship is awkward: tools.py defines args + thin wrappers, capabilities.py contains all logic, but the boundary is inconsistent. `GetUsageStats` and `ListUnreadMessages` have their entire logic inlined in tools.py, bypassing capabilities entirely.

### Risks
- [x] `capabilities.py` will keep growing as new tools are added — it's already the largest file by 2x
- [x] `GetUsageStats` doesn't use the `_track_tool_telemetry` decorator (inconsistency), doesn't return `ToolResult`
- [x] Legacy cursor functions (`encode_cursor`, `decode_cursor`) in pagination.py — `encode_cursor` is called in capabilities.py to populate `next_cursor` field on `HistoryReadExecution`, but that field is never read by tools.py. `decode_cursor` is not called in src/ at all. Both are dead code from the old cursor system.
- [x] `_DDL = _ENTITY_TABLE_DDL` in cache.py is an unused alias
- [x] `connected_client()` in tools.py creates+destroys a connection per tool call; `ListUnreadMessages` does multiple API calls inside a single connection, but each other tool creates its own — **verified: all tools use `connected_client()` consistently, no actual inconsistency**

### Recommendations
- [x] Split capabilities.py into focused modules (domain_models, dialog_resolution, forum_topics, message_processing, capability_history, capability_search, capability_list_topics, budget_and_priority)
- [ ] Move `ListUnreadMessages` logic to a capability module (like other tools) — **deferred**: kept in tools.py intentionally, planned for separate extraction when unread workflow stabilizes
- [x] Make `GetUsageStats` return `ToolResult` and use `_track_tool_telemetry` consistently — or explicitly document why it's exempt
- [x] Move `format_usage_summary()` from tools.py to analytics.py (it formats analytics data, not messages)
- [x] Remove dead cursor code: `encode_cursor`/`decode_cursor` from pagination.py, `next_cursor` field from `HistoryReadExecution`, the `encode_cursor` call in capabilities.py
- [x] Remove `_DDL` alias in cache.py

---

## Kaizen Master

### Assessment
The codebase is cleaner than expected for "several refactorings." The main smell is inconsistency, not complexity. Some patterns were applied to most tools but not all. The `TOOL_POSTURE` dict is metadata that could live on the tool class itself but doesn't — it's a parallel registry that must be kept in sync manually.

### Risks
- [x] `TOOL_POSTURE` and `TOOL_REGISTRY` are two separate dicts that must list the same tools — fragile duplication
- [x] `_resolve_dialog` wrapper in tools.py exists solely to swap the import name — confusing indirection
- [x] `format_usage_summary()` in tools.py belongs in analytics.py — it's orphaned in tools.py among tool handlers
- [x] `errors.py` has `invalid_cursor_text()` — never called anywhere in src/, dead code from old cursor system
- [x] `cli.py` at repo root duplicates `_topic_row_text()` — capabilities.py has `topic_row_text()` already

### Recommendations
- [x] Merge `TOOL_POSTURE` into `TOOL_REGISTRY` or add posture as a class attribute on ToolArgs subclasses
- [x] Move `format_usage_summary()` to analytics.py
- [x] Remove dead `invalid_cursor_text()` from errors.py
- [x] Remove `_topic_row_text()` duplicate from cli.py, import `topic_row_text` from capabilities (or its successor module)
- [x] Drop the `_resolve_dialog` indirection in tools.py — import and call `resolve_dialog` directly from resolver

---

## QA Engineer

### Assessment
Test coverage looks solid (7128 lines of tests for 5273 lines of source). The `singledispatch` + decorator stack (`@tool_runner.register` / `@_track_tool_telemetry`) is clever but fragile — the decorator order matters and the `__wrapped__` trick is non-obvious. If someone adds a new tool and gets the decorator order wrong, it silently breaks dispatch.

### Risks
- [x] `GetUsageStats` runner returns `Sequence[TextContent | ...]` directly, not `ToolResult` — it bypasses `_track_tool_telemetry` unwrapping and `ToolResult.content` extraction. Return type inconsistent with all other tools.
- [x] `verify_tool_registry()` checks for runner existence but doesn't verify the telemetry decorator is applied
- [x] In `ListUnreadMessages`, hardcoded Russian strings (`"Нет непрочитанных сообщений"`) aren't in errors.py like all other user-facing text — inconsistency
- [x] `Optional[str]` import in analytics.py while rest of codebase uses `str | None` — style inconsistency

### Recommendations
- [x] Normalize `GetUsageStats` to return `ToolResult` and use `_track_tool_telemetry` (with telemetry self-recording skipped via flag if intentional)
- [x] Move hardcoded Russian text from `ListUnreadMessages` into errors.py
- [x] Replace `Optional[str]` with `str | None` in analytics.py for consistency
- [x] Add a comment or assertion in `verify_tool_registry` about expected decorator stack

---

## Team Lead

### Assessment
The code is maintainable and well-structured for its size. The "add a new tool" comment block in tools.py is helpful. The main maintainability concern is capabilities.py — at 1900 lines, new contributors will struggle to navigate it. The inconsistencies (GetUsageStats pattern, hardcoded strings, dual registries) are the kind of things that accumulate during iterative development and are worth cleaning up in a focused pass.

### Risks
- [x] `_cache_dialog_entry` being imported from resolver.py into tools.py (private function cross-module import) is a code smell — it's private but used externally
- [x] `connected_client()` definition comment says "so tests can patch create_client in this module" — test-driven design leak into production code

### Recommendations
- [x] Make `_cache_dialog_entry` public (rename to `cache_dialog_entry`) since it's imported cross-module
- [x] Prioritize consistency fixes (GetUsageStats, error strings, posture/registry merge) — small wins with high clarity payoff

---

## Panel Conflicts & Resolutions

| Topic | Position A | Position B | Resolution |
|-------|-----------|-----------|------------|
| Split capabilities.py now? | Architect: split into focused modules | Kaizen: don't split until it actively hurts | **User decided: split now** as part of cleanup |
| Where to put `format_usage_summary`? | Architect: analytics.py (formats analytics data) | QA: formatter.py (it's a formatting function) | **analytics.py** — keeps formatter.py focused on message formatting |
| `_cache_dialog_entry` location | Team Lead: move to cache.py | Architect: keep in resolver.py, it knows dialog structure | **Keep in resolver.py** but make it public (rename to `cache_dialog_entry`) |

---

## Investigation Results (Post-Panel)

### Dead code confirmed
- [x] `decode_cursor()` in pagination.py — not called in src/, only in tests
- [x] `encode_cursor()` in pagination.py — called in capabilities.py to set `next_cursor` on `HistoryReadExecution`, but that field is never read by any consumer (tools.py ignores it, uses `navigation` instead)
- [x] `next_cursor` field on `HistoryReadExecution` dataclass — set but never consumed
- [x] `invalid_cursor_text()` in errors.py — defined but never called
- [x] `_DDL = _ENTITY_TABLE_DDL` alias in cache.py — unused

### cli.py (debug CLI) status
- Created for development debugging (topic catalog inspection)
- User confirms: not used in production workflow (only docker container rebuild + external access)
- Has duplicate `_topic_row_text()` that exists in capabilities as `topic_row_text()`
- Decision: keep but clean up (remove duplicate, fix imports)

### GetUsageStats telemetry skip
- Added in phase 06-telemetry-foundation
- Original commit: "implement GetUsageStats tool and format_usage_summary()"
- The tool was designed so LLMs working with the server could analyze usage patterns and give recommendations
- Skipping self-telemetry appears unintentional — it's the only tool without `_track_tool_telemetry` and without `ToolResult` return type
- Fix: normalize to match all other tools, add telemetry recording (no reason to exclude it)
