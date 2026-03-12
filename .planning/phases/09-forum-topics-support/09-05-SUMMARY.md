---
phase: 09-forum-topics-support
plan: 05
subsystem: api
tags: [telegram, telethon, forum-topics, unread, pytest]
requires:
  - phase: 09-04
    provides: topic refresh recovery and explicit topic-state classification
provides:
  - topic-scoped unread fetches for General and non-General topics
  - topic-derived unread cursors that ignore leaked dialog messages
  - empty unread topic pages with no copied dialog-wide cursor
affects: [forum-topics, unread-pagination, live-validation]
tech-stack:
  added: []
  patterns: [topic-aware unread filtering, emitted-message cursor derivation]
key-files:
  created: []
  modified: [src/mcp_telegram/tools.py, tests/test_tools.py]
key-decisions:
  - "Unread mode keeps `read_inbox_max_id` as the lower bound, but topic filtering decides which fetched messages count toward the page."
  - "General-topic unread pages are explicitly filtered instead of relying on the absence of `reply_to`."
  - "Unread cursors are derived from emitted topic messages only; empty filtered pages carry no cursor."
patterns-established:
  - "Topic-aware unread fetch: `use_topic_scoped_fetch` covers unread requests even when the topic is General."
  - "Cursor safety: topic unread cursors come from the last returned topic message, not the raw unread batch."
requirements-completed: [TOPIC-01, TOPIC-02]
duration: 2h 34m
completed: 2026-03-12
---

# Phase 9 Plan 5: Topic-Scoped Unread Summary

**Unread topic pages now stay inside the requested forum topic, including General, with topic-scoped cursors and empty-page handling**

## Performance

- **Duration:** 2h 34m
- **Started:** 2026-03-12T13:14:57Z
- **Completed:** 2026-03-12T15:49:00Z
- **Tasks:** 3
- **Files modified:** 2

## Accomplishments

- Added live-representative regressions for General-topic unread pages, unread cursor scoping, adjacent-topic leak filtering, and empty filtered pages.
- Updated `ListMessages(topic=..., unread=True)` so unread mode uses topic-aware fetching for both General and non-General topics.
- Rebuilt and restarted the `mcp-telegram` container, then verified the deployed runtime still exposes the topic-scoped unread branch and helper logic.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add live-representative regressions for topic-scoped unread behavior**
   - `ab8d2f3` (`test`) - failing unread topic scoping regressions
2. **Task 2: Implement topic-aware unread fetch rules for General and non-General topics**
   - `0d0ab09` (`fix`) - topic-aware unread filtering before page/cursor calculation
3. **Task 3: Stabilize unread cursors and empty-page behavior under topic filtering**
   - `b206c67` (`test`) - empty-page cursor regression and contract lock-in
   - `docs metadata commit` (`docs`) - summary/state/roadmap closure after deployed-runtime verification

## Files Created/Modified

- `src/mcp_telegram/tools.py` - topic-scoped unread branching, filtered topic fetch path, and emitted-message cursor selection
- `tests/test_tools.py` - regressions for General unread scoping, cursor derivation, leak filtering, and empty-page behavior

## Decisions Made

- Unread topic requests now go through `use_topic_scoped_fetch`, even when the topic is General.
- The topic fetch helper continues paging raw unread batches until it emits enough matching topic messages or exhausts the unread slice.
- `next_cursor` is derived from the filtered topic result, so dialog-wide unread leaks cannot influence pagination.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The executor again stalled after landing the useful commits and before writing closure artifacts. The plan was closed manually by verifying the existing commits, rerunning the focused tests, rebuilding the container, and checking the deployed Python package in place.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- `09-06` can now focus entirely on the debug CLI and rebuilt-runtime validation checklist.
- The remaining open work in Phase 9 is operator tooling and final live-verification closure, not topic unread correctness.

## Self-Check: PASSED

- Verified task commits `ab8d2f3`, `0d0ab09`, and `b206c67` exist in git history.
- Verified `uv run pytest tests/test_tools.py -k "topic_unread or unread_filter" -v` passes locally.
- Verified the rebuilt `mcp-telegram` container starts and the deployed runtime includes `use_topic_scoped_fetch`, `cursor_source_messages`, and `_fetch_topic_messages` in the unread topic path.
