---
phase: 09-forum-topics-support
plan: 04
subsystem: api
tags: [telegram, telethon, forum-topics, topic-refresh, pytest]
requires:
  - phase: 09-01
    provides: topic metadata cache and by-id topic refresh helpers
  - phase: 09-02
    provides: dialog-scoped topic resolution and topic-prefixed ListMessages output
  - phase: 09-03
    provides: deleted-topic tombstones, inaccessible-topic messaging, and topic-safe paging
provides:
  - one-shot by-id topic refresh before surfacing TOPIC_ID_INVALID failures
  - stale-anchor retry with refreshed top_message_id
  - differentiated deleted vs inaccessible topic responses after refresh
affects: [forum-topics, topic-unread, live-validation]
tech-stack:
  added: []
  patterns: [bounded topic refresh retry, explicit post-refresh topic-state classification]
key-files:
  created: []
  modified: [src/mcp_telegram/tools.py, tests/test_tools.py]
key-decisions:
  - "TOPIC_ID_INVALID is treated as a recoverable topic-state mismatch once before it becomes a user-visible failure."
  - "A refreshed deleted topic returns the tombstone message; an active topic that still fails keeps the RPC-driven inaccessible message."
  - "Topic failures remain topic-scoped and never degrade to unfiltered dialog history."
patterns-established:
  - "Bounded topic refresh: one by-id refresh plus one retry for stale top_message_id mismatches."
  - "Post-refresh classification: deleted and inaccessible topics split after the refresh result is known."
requirements-completed: [TOPIC-01, TOPIC-02]
duration: 23 min
completed: 2026-03-12
---

# Phase 9 Plan 4: Topic Refresh Recovery Summary

**One-shot topic refresh and stale-anchor retry that distinguishes deleted topics from still-inaccessible threads**

## Performance

- **Duration:** 23 min
- **Started:** 2026-03-12T12:45:45Z
- **Completed:** 2026-03-12T13:08:40Z
- **Tasks:** 3
- **Files modified:** 2

## Accomplishments

- Added regressions for stale topic anchors, deleted-after-refresh topics, and still-inaccessible topic retries.
- Hardened `ListMessages(topic=...)` to refresh a topic by ID once, retry with a refreshed `top_message_id`, and classify the result explicitly.
- Rebuilt and restarted the long-lived `mcp-telegram` container, then verified the running container exposes the new `list_messages` refresh/classification path.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add failing regressions for stale-anchor recovery and differentiated failures**
   - `b5feefe` (`test`) - failing topic refresh regressions
2. **Task 2: Implement one-shot by-id refresh and bounded retry for topic thread fetches**
   - `0b65feb` (`fix`) - by-id refresh, stale-anchor retry, and post-refresh classification
3. **Task 3: Lock the user-visible diagnostic contract and verify the deployed runtime**
   - `docs metadata commit` (`docs`) - summary/state/roadmap closure after runtime rebuild verification

## Files Created/Modified

- `src/mcp_telegram/tools.py` - bounded topic refresh helper usage and stale-anchor retry within `ListMessages`
- `tests/test_tools.py` - user-facing regressions covering stale, deleted, and inaccessible topic outcomes

## Decisions Made

- `TOPIC_ID_INVALID` now triggers one by-id refresh before the tool decides whether the topic is deleted, re-anchored, or still inaccessible.
- A refreshed `top_message_id` retries silently, so callers only see the successful topic result rather than an implementation detail.
- If refresh confirms the topic still exists but thread fetch still fails, the tool returns explicit inaccessible-topic text instead of dropping to dialog-wide history.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- The initial executor landed the code commits but was interrupted before writing closure artifacts. The code and runtime rollout were spot-checked locally and the plan metadata was completed manually from the committed state.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Wave 5 can now build on the refreshed topic metadata path without inheriting the undifferentiated `TOPIC_ID_INVALID` failure.
- Remaining live closure work is now focused on `topic + unread` scoping and the operator-facing debug path for final forum validation.

## Self-Check: PASSED

- Verified task commits `b5feefe` and `0b65feb` exist in git history.
- Verified the targeted topic refresh regression suite passes locally.
- Verified the rebuilt `mcp-telegram` container starts and the deployed `list_messages` code references the new topic refresh/classification path.
