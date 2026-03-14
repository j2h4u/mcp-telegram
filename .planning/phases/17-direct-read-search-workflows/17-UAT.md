---
status: complete
phase: 17-direct-read-search-workflows
source:
  - 17-01-SUMMARY.md
  - 17-02-SUMMARY.md
  - 17-03-SUMMARY.md
started: 2026-03-14T10:51:04Z
updated: 2026-03-14T11:16:19Z
---

## Current Test

[testing complete]

## Tests

### 1. Reflected direct read/search contract
expected: Inspect the tool surface in the runtime you actually use, or run `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`. `ListMessages` should show the new optional `exact_dialog_id` and `exact_topic_id` fields alongside the existing fuzzy selectors, with `dialog` no longer required. `SearchMessages` should keep the same public shape (`dialog`, `query`, `limit`, `navigation`) while its docs still teach numeric dialog IDs for direct disambiguation.
result: pass

### 2. ListMessages direct exact-dialog read
expected: Call `ListMessages` with a real `exact_dialog_id` and no `dialog`. The tool should return that dialog's messages directly in the normal readable transcript format and continue to use the shared `navigation` contract instead of forcing fuzzy dialog discovery first.
result: pass

### 3. ListMessages selector-conflict validation
expected: Call `ListMessages` with conflicting selectors, such as both `dialog` and `exact_dialog_id`, or both `topic` and `exact_topic_id`. The tool should fail cleanly with a clear validation message instead of silently choosing one path.
result: pass

### 4. ListMessages direct forum-topic read
expected: Call `ListMessages` with `exact_dialog_id` plus `exact_topic_id` for a known forum topic. The tool should read that topic directly while preserving the existing forum behavior: readable topic context, clean handling for General or missing/inaccessible topics, unread filtering, and normal `next_navigation` continuation.
result: pass

### 5. SearchMessages direct numeric-dialog search
expected: Call `SearchMessages` with a signed numeric dialog id in `dialog` plus a real `query`. The tool should search that dialog directly and return grouped hit-local windows with `--- hit N/M ---` sections and `[HIT]` markers, while preserving the normal search pagination flow.
result: pass

### 6. Parallel MCP session resilience
expected: Run two independent MCP client sessions against the live `mcp-telegram` container and call read-oriented tools concurrently. Shared SQLite-backed cache setup should stay available under concurrent access instead of failing during connection or cache initialization.
result: issue
reported: "Parallel runtime verification hit `sqlite3.OperationalError: database is locked` from `EntityCache.__init__()` while another MCP session was active."
severity: major

## Summary

total: 6
passed: 5
issues: 1
pending: 0
skipped: 0

## Gaps

- truth: "Concurrent MCP sessions can execute read-oriented tools without SQLite lock failures during shared cache initialization."
  status: failed
  reason: "Runtime verification against the live container hit `sqlite3.OperationalError: database is locked` in `EntityCache.__init__()` when parallel MCP client sessions invoked read tools."
  severity: major
  test: 6
  root_cause: ""
  artifacts:
    - path: "src/mcp_telegram/cache.py"
      issue: "Cache connection setup performs SQLite initialization work that is not yet hardened for concurrent multi-process MCP sessions."
  missing:
    - "Reproduce the lock with a deterministic parallel MCP-session check."
    - "Harden SQLite cache initialization so concurrent sessions do not fail with `database is locked`."
  debug_session: ""
