---
phase: 09-forum-topics-support
plan: 06
subsystem: testing
tags: [telegram, cli, forum-topics, manual-validation, pytest]
requires:
  - phase: 09-04
    provides: topic refresh recovery and explicit topic-state classification
  - phase: 09-05
    provides: topic-scoped unread filtering and cursor rules
provides:
  - read-only topic debug CLI commands for catalog and by-id inspection
  - rebuilt-runtime validation checklist for final phase closure
  - operator evidence checklist for roadmap criterion 5
affects: [forum-topics, live-validation, operator-debugging]
tech-stack:
  added: []
  patterns: [operator-only debug CLI, host-cli plus in-container runtime verification]
key-files:
  created: [tests/test_cli.py]
  modified: [cli.py, .planning/phases/09-forum-topics-support/09-MANUAL-VALIDATION.md]
key-decisions:
  - "Topic debug commands stay in `cli.py` and do not widen the MCP tool surface."
  - "Live validation is split into host-side debug CLI commands and in-container runtime proof commands."
  - "The validation playbook requires evidence capture for pagination, by-id refresh, unread behavior, and deleted/inaccessible topics."
patterns-established:
  - "Operator-only topic inspection: use `debug-topic-catalog` and `debug-topic-by-id` before guessing at failing topic names."
  - "Runtime proof first: verify deployed code inside the container before trusting live Telegram behavior."
requirements-completed: [TOPIC-01, TOPIC-02]
duration: 3 min
completed: 2026-03-12
---

# Phase 9 Plan 6: Debug CLI And Validation Closure Summary

**Read-only topic debug commands and a rebuilt-runtime validation checklist now provide a credible path to close live forum-topic verification**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-12T15:53:43Z
- **Completed:** 2026-03-12T15:56:43Z
- **Tasks:** 3
- **Files modified:** 3

## Accomplishments

- Added smoke coverage for the new topic debug CLI surface.
- Implemented `debug-topic-catalog` and `debug-topic-by-id` in `cli.py` using existing topic helpers instead of adding new MCP tools.
- Rewrote the manual validation playbook into a concrete rebuilt-runtime closure checklist with exact commands and evidence requirements.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add smoke tests for live topic-debug CLI commands**
   - `f3d5d7d` (`test`) - failing topic debug CLI coverage
2. **Task 2: Implement read-only topic debug commands in cli.py**
   - `1bdca4c` (`feat`) - operator-facing topic catalog and by-id refresh commands
3. **Task 3: Rewrite the manual validation playbook around rebuilt-runtime closure**
   - `8de22fc` (`docs`) - rebuilt-runtime closure checklist and evidence capture guide
   - `docs metadata commit` (`docs`) - summary/state/roadmap closure after verification

## Files Created/Modified

- `tests/test_cli.py` - smoke tests for `debug-topic-catalog` and `debug-topic-by-id`
- `cli.py` - read-only topic debug commands with explicit CLI error boundaries
- `.planning/phases/09-forum-topics-support/09-MANUAL-VALIDATION.md` - final closure checklist for live forum validation

## Decisions Made

- The debug path remains local to `cli.py`, so operators can inspect topic metadata without expanding the MCP API.
- `debug-topic-catalog` exposes raw page boundaries and a normalized catalog summary so operators can validate both pagination and final topic state.
- The manual validation playbook now starts with container proof commands, which prevents stale runtime confusion before Telegram behavior is evaluated.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

- `tests/test_cli.py` did not exist, so the CLI surface had no guardrails before this plan. The new smoke tests now pin the command shape and output contract.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- All Phase 9 execution plans are complete.
- The only remaining Phase 9 gap is manual live validation on a suitable 100+ topic forum using the rewritten checklist.

## Self-Check: PASSED

- Verified `uv run pytest tests/test_cli.py -k "debug_topic" -v` passes.
- Verified `uv run python cli.py debug-topic-catalog --help` works.
- Verified `uv run python cli.py debug-topic-by-id --help` works.
- Verified the manual validation playbook includes rebuilt-runtime commands, exact CLI invocations, and an evidence checklist for roadmap criterion 5.
