---
status: complete
phase: 17-direct-read-search-workflows
source:
  - 17-01-SUMMARY.md
  - 17-02-SUMMARY.md
  - 17-03-SUMMARY.md
  - 17-04-SUMMARY.md
started: 2026-03-14T14:09:03Z
updated: 2026-03-14T14:14:27Z
---

## Current Test

[testing complete]

## Tests

### 1. Reflected direct read/search contract
expected: Inspect the tool surface in the runtime you actually use, or run `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`. `ListMessages` should show the optional `exact_dialog_id` and `exact_topic_id` fields alongside the fuzzy selectors, with `dialog` no longer required. `SearchMessages` should keep the same public shape (`dialog`, `query`, `limit`, `navigation`) while its docs still teach signed numeric dialog IDs for direct disambiguation.
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
expected: Run two independent MCP client sessions against the live `mcp-telegram` container and call read-oriented tools concurrently. Shared SQLite-backed cache setup should stay available under concurrent access instead of failing during connection or cache initialization, and the direct read/search flows above should still work afterward.
result: pass

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0

## Gaps
