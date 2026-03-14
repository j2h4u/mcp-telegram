---
status: complete
phase: 16-unified-navigation-contract
source:
  - 16-01-SUMMARY.md
  - 16-02-SUMMARY.md
  - 16-03-SUMMARY.md
started: 2026-03-14T08:59:21Z
updated: 2026-03-14T09:09:05Z
---

## Current Test

[testing complete]

## Tests

### 1. ListMessages latest-page shared navigation
expected: Call ListMessages with a real dialog and no navigation field (or navigation="newest"). The response should show the newest messages in the readable transcript format and, if another page exists, the footer should expose next_navigation. It should not teach next_cursor or from_beginning anymore.
result: pass

### 2. ListMessages oldest-first entry
expected: Call ListMessages with the same dialog and navigation="oldest". The response should start from the oldest available messages for that dialog and continue through next_navigation rather than a separate from_beginning flow.
result: pass

### 3. ListMessages invalid navigation failure
expected: Call ListMessages with an obviously invalid navigation string such as navigation="not-a-real-token". The tool should fail cleanly with actionable guidance instead of crashing or returning a misleading transcript.
result: pass

### 4. SearchMessages first page shared navigation
expected: Call SearchMessages with a real dialog and query, without navigation. The response should return matching hits with local context and, if more matches exist, expose next_navigation. It should not expose offset or next_offset.
result: pass

### 5. SearchMessages continuation
expected: Reuse the next_navigation token from the previous SearchMessages response with the same dialog and query. The tool should continue to the next search page instead of repeating the same results.
result: pass

### 6. SearchMessages mismatched navigation rejection
expected: Reuse a SearchMessages next_navigation token with a different query or try to pass it to ListMessages. The tool should reject the mismatched navigation state with a clear, action-oriented message.
result: pass

## Summary

total: 6
passed: 6
issues: 0
pending: 0
skipped: 0

## Gaps

None.
