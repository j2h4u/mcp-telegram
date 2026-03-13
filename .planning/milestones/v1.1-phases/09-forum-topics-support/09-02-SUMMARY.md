---
phase: 09-forum-topics-support
plan: 02
subsystem: api
tags: [telegram, telethon, forum-topics, pagination, pytest]
requires:
  - phase: 09-forum-topics-support
    provides: dialog-scoped topic metadata cache and topic catalog helpers
provides:
  - dialog-scoped topic resolution in ListMessages
  - topic-thread retrieval via reply_to for non-General topics
  - topic-aware output headers and explicit sender/unread interactions
affects: [09-03, list-messages, topic-filtering]
tech-stack:
  added: []
  patterns: [dialog-first topic resolution, tool-level topic headers, local sender filtering after thread fetch]
key-files:
  created: []
  modified:
    - src/mcp_telegram/tools.py
    - tests/test_tools.py
    - tests/conftest.py
key-decisions:
  - "ListMessages resolves dialogs first, then runs topic matching against that dialog's cached topic catalog only."
  - "Non-General topic fetches use iter_messages(reply_to=top_message_id) and keep the topic label in the tool output prefix."
  - "topic+sender filters locally after reply_to retrieval; topic+unread remains supported by combining reply_to with min_id."
patterns-established:
  - "Topic-filtered ListMessages keeps formatter.py unchanged and prepends [topic: ...] in the handler."
  - "Cursor generation for local topic+sender filtering follows the fetched page boundary, not just the filtered subset."
requirements-completed: [TOPIC-01, TOPIC-03]
duration: 4m 44s
completed: 2026-03-12
---

# Phase 9 Plan 2: ListMessages topic support Summary

**Dialog-scoped topic resolution with reply_to thread retrieval, topic output headers, and explicit sender/unread behavior**

## Performance

- **Duration:** 4m 44s
- **Started:** 2026-03-12T00:46:24Z
- **Completed:** 2026-03-12T00:51:08Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- Added `topic: str | None` to `ListMessages` and resolved topic names only inside the already-resolved dialog.
- Routed non-General topics through `iter_messages(..., reply_to=top_message_id)` and prepended `[topic: ...]` to the response body.
- Made topic interactions with `sender` and `unread` explicit through tests and handler logic.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add topic parameter and dialog-scoped topic resolution to ListMessages**
   - `b6aed76` (`test`) RED tests for dialog-scoped topic resolution, missing topics, and ambiguity
   - `b35cb14` (`feat`) `ListMessages.topic` plus dialog-scoped topic lookup
2. **Task 2: Implement topic-aware message retrieval and output header**
   - `05b38c9` (`test`) RED tests for topic headers and topic pagination behavior
   - `33486a7` (`feat`) thread-scoped retrieval with `reply_to` and topic header output
3. **Task 3: Define and test interaction rules for sender and unread filters**
   - `b8dc60b` (`test`) RED tests for topic+sender and topic+unread behavior
   - `bf41bf2` (`feat`) local topic sender filtering and explicit unread compatibility

## Files Created/Modified

- `src/mcp_telegram/tools.py` - Added topic schema support, dialog-local topic resolution, reply-to retrieval, topic headers, and explicit topic filter interactions.
- `tests/test_tools.py` - Added topic resolution, header, pagination, sender, and unread behavior tests.
- `tests/conftest.py` - Added shared fixtures for topic metadata rows and forum reply headers.

## Decisions Made

- Resolved topics inside `list_messages` instead of teaching the global resolver about dialog/topic tuples.
- Kept topic header rendering in `list_messages` to avoid expanding the formatter surface for one handler-specific concern.
- Treated `topic + sender` as a local post-fetch filter when `reply_to` is active, while preserving `topic + unread` server-side.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The unread interaction RED test initially modeled `client(request)` incorrectly with `AsyncMock.__call__`; adjusted the test harness to use the mock object's awaited return path.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `ListMessages(topic=...)` now resolves topics safely within one dialog, fetches non-General threads directly, and exposes explicit behavior for related filters.
- Phase `09-03` can focus on deleted-topic handling, live-forum validation, and any real-world thread edge cases without reopening the main handler contract.

## Self-Check: PASSED

- Verified summary file exists at `.planning/phases/09-forum-topics-support/09-02-SUMMARY.md`.
- Verified task commits `b6aed76`, `b35cb14`, `05b38c9`, `33486a7`, `b8dc60b`, and `bf41bf2` exist in git history.
