---
status: diagnosed
phase: 17-direct-read-search-workflows
source:
  - 17-01-SUMMARY.md
  - 17-02-SUMMARY.md
  - 17-03-SUMMARY.md
started: 2026-03-14T10:51:04Z
updated: 2026-03-14T11:39:15Z
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
  root_cause: "`EntityCache.__init__()` reruns mutating SQLite startup work on every MCP process (`PRAGMA journal_mode=WAL`, table/index DDL, `PRAGMA optimize`) against the shared `entity_cache.db`, while active read workflows can hold write/schema locks on the same file through lazily initialized topic and reaction caches."
  artifacts:
    - path: "src/mcp_telegram/cache.py"
      issue: "EntityCache constructor performs write-capable startup and maintenance work on every process open instead of making shared-cache open read-safe."
    - path: "src/mcp_telegram/tools.py"
      issue: "get_entity_cache() is cached only per process, so parallel MCP sessions re-enter the constructor against the same SQLite file."
    - path: "src/mcp_telegram/capabilities.py"
      issue: "Read-oriented message/topic flows lazily initialize additional SQLite-backed caches on the same database connection and can hold locks during concurrent startup."
  missing:
    - "Make entity cache open cheap and read-safe across concurrent MCP processes."
    - "Move or guard one-time SQLite bootstrap and `PRAGMA optimize` so they do not run in every process constructor."
    - "Add a deterministic parallel MCP-session regression or runtime verification for shared-cache startup."
  debug_session: ".planning/debug/concurrent-cache-init-lock.md"
