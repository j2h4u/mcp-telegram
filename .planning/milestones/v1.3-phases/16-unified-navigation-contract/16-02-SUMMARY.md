---
phase: 16-unified-navigation-contract
plan: 02
subsystem: api
tags: [telegram, navigation, pagination, schema, pytest, mcp]
requires:
  - phase: 15-capability-seams
    provides: history-read execution seams below the tool adapter
  - phase: 16-unified-navigation-contract
    provides: shared opaque navigation tokens and capability-level continuation metadata
provides:
  - ListMessages shared public navigation input and footer wording
  - oldest-first and newest-first history entry points through one navigation field
  - reflected ListMessages schema proof at the MCP server boundary
affects: [16-03, ListMessages, SearchMessages, server reflection]
tech-stack:
  added: []
  patterns: [single-field navigation contract, direction-aware history tokens, reflection-pinned MCP schema]
key-files:
  created: []
  modified:
    - src/mcp_telegram/pagination.py
    - src/mcp_telegram/capabilities.py
    - src/mcp_telegram/tools.py
    - tests/test_capabilities.py
    - tests/test_tools.py
    - tests/test_server.py
key-decisions:
  - "Use one string navigation field for ListMessages, with newest/oldest first-page keywords and opaque next_navigation continuation tokens."
  - "Encode history direction into shared navigation tokens so oldest-first pagination can continue through the same public field without reintroducing from_beginning."
patterns-established:
  - "ListMessages public continuation now hangs entirely off navigation/next_navigation while capability validation keeps dialog/topic mismatch failures action-oriented."
  - "Local reflection proof belongs in server-boundary tests plus cli.py list-tools output, not only in adapter-level schema assertions."
requirements-completed: [NAV-01, NAV-02]
duration: 10 min
completed: 2026-03-14
---

# Phase 16 Plan 02: Unified Navigation Contract Summary

**ListMessages shared navigation surface with newest/oldest entry modes, topic-safe continuation tokens, and reflected MCP schema proof**

## Performance

- **Duration:** 10 min
- **Started:** 2026-03-14T00:08:00Z
- **Completed:** 2026-03-14T00:17:59Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Migrated [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py) so `ListMessages` now teaches `navigation` / `next_navigation` instead of `cursor` / `from_beginning`.
- Updated [src/mcp_telegram/capabilities.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/capabilities.py) and [src/mcp_telegram/pagination.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py) so history tokens carry direction and keep topic-scoped mismatch checks action-oriented.
- Refreshed [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py), [tests/test_capabilities.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_capabilities.py), and [tests/test_server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_server.py) to pin the new contract and verify local reflection through `uv run cli.py list-tools`.

## Task Commits

Each task was committed atomically:

1. **Task 1: Replace `ListMessages` split navigation terms with the shared vocabulary** - `f28fa23` (feat)
2. **Task 2: Prove the reflected `ListMessages` schema and regression surface locally** - `cd1cc68` (test)

## Files Created/Modified
- `src/mcp_telegram/pagination.py` - Added optional history direction to shared navigation tokens.
- `src/mcp_telegram/capabilities.py` - Parsed one public history navigation field and validated direction-aware continuation reuse.
- `src/mcp_telegram/tools.py` - Reflected the new `ListMessages` schema and emitted `next_navigation` in read responses.
- `tests/test_capabilities.py` - Covered navigation parsing and topic-scoped history continuation under the new contract.
- `tests/test_tools.py` - Replaced legacy cursor/from_beginning assertions with navigation/next_navigation behavior checks.
- `tests/test_server.py` - Pinned the reflected `ListMessages` MCP schema to `navigation` and rejected legacy field names.

## Decisions Made
- Used explicit `newest` and `oldest` keywords for first-page reads so callers can choose history direction without a second boolean field.
- Kept telemetry's `has_cursor` column bounded for now by treating only opaque continuation tokens as `True`; first-page keywords remain `False` until the wider telemetry cleanup in Plan 16-03.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Return navigation failures as user-facing text instead of treating them like successful history results**
- **Found during:** Task 1
- **Issue:** After moving `ListMessages` onto shared navigation input, the adapter still handled `NavigationFailure` incorrectly and tried to format it as a successful history response.
- **Fix:** Extended the adapter failure branch in [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py) to return `NavigationFailure.text` directly.
- **Files modified:** `src/mcp_telegram/tools.py`
- **Verification:** `uv run pytest tests/test_tools.py -k "list_messages or from_beginning or cursor or topic or sender" -q`
- **Committed in:** `f28fa23`

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** The auto-fix was required for correctness of the new public contract. No scope expansion beyond ListMessages navigation migration.

## Issues Encountered
- `uv run cli.py list-tools` could not use the default cache path inside the sandbox, so the local reflection check was rerun successfully with `UV_CACHE_DIR=/tmp/.uv-cache`.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- `SearchMessages` can now adopt the same `navigation` / `next_navigation` vocabulary on top of the direction-aware shared token family added here.
- Local schema reflection is pinned for `ListMessages`; the live runtime rebuild/restart proof remains for Plan 16-03.

## Self-Check: PASSED

- Found `.planning/phases/16-unified-navigation-contract/16-02-SUMMARY.md`
- Found commit `f28fa23`
- Found commit `cd1cc68`
