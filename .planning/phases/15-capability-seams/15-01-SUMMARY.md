---
phase: 15-capability-seams
plan: 01
subsystem: api
tags: [telegram, telethon, capabilities, topics, dialogs, pytest]
requires:
  - phase: 14-boundary-recovery
    provides: actionable tool-boundary recovery so internal seam extraction can stay debuggable
provides:
  - explicit dialog-target seam with typed resolved and actionable failure outcomes
  - explicit forum-topic seam for catalog loading, topic resolution, and stale-anchor recovery
  - seam-focused tests independent of current public tool names
affects: [phase-16-unified-navigation-contract, phase-17-direct-read-search-workflows, read-adapters, topic-adapters]
tech-stack:
  added: []
  patterns: [typed capability outcomes, dict-backed topic catalogs, injectable seam hooks for brownfield preservation]
key-files:
  created:
    - src/mcp_telegram/capabilities.py
    - tests/test_capabilities.py
  modified:
    - src/mcp_telegram/tools.py
    - tests/test_tools.py
key-decisions:
  - "Represent dialog and forum-topic seams as explicit typed outcomes without introducing a new service framework."
  - "Keep topic metadata cache rows dict-backed, then wrap them in seam result objects so the extraction stays bounded and inspectable."
  - "Allow tool adapters to inject topic loaders and stale-anchor refresh helpers into the seam to preserve existing brownfield tests and recovery behavior."
patterns-established:
  - "Dialog-target pattern: resolve once, then return either a typed resolved target or a typed actionable failure with final user text."
  - "Forum-topic pattern: one capability path owns catalog loading, topic resolution, and stale-anchor retry while public tools stay schema-stable."
requirements-completed: [CAP-01]
duration: 12m 39s
completed: 2026-03-13
---

# Phase 15 Plan 01: Capability Contract Anchors Summary

**Dialog-target and forum-topic capability seams with typed outcomes, stale-anchor recovery, and direct seam tests behind unchanged public MCP tool names**

## Performance

- **Duration:** 12m 39s
- **Started:** 2026-03-13T22:59:43Z
- **Completed:** 2026-03-13T23:12:17Z
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments

- Added `src/mcp_telegram/capabilities.py` as the bounded seam module for dialog resolution, forum-topic loading, topic resolution, and topic fetch recovery.
- Routed `ListTopics`, `ListMessages`, and `SearchMessages` through typed dialog-target outcomes while keeping their public names and schemas unchanged.
- Added seam-focused tests in `tests/test_capabilities.py` and a public-surface guard in `tests/test_tools.py`, then rebuilt and restarted the live runtime successfully.

## Task Commits

Each task was committed atomically:

1. **Task 1: Introduce bounded capability primitives for dialog targets and forum topics** - `b78c5c9` (feat)
2. **Task 2: Add seam-focused tests and preserve brownfield behavior** - `6e7aad4` (test)
3. **Task 3: Rebuild and restart the runtime to prove the foundation plan is live** - `be662f8` (chore)

**Plan metadata:** pending

## Files Created/Modified

- `src/mcp_telegram/capabilities.py` - New seam module for dialog-target and forum-topic behavior.
- `src/mcp_telegram/tools.py` - Public MCP adapters now consume seam outcomes instead of owning the dialog/topic resolution flow directly.
- `tests/test_capabilities.py` - Direct seam contract tests for dialog outcomes, topic outcomes, and stale-anchor refresh.
- `tests/test_tools.py` - Brownfield regression and public-name guard proving the extraction did not change the public tool surface.

## Decisions Made

- Used dataclass seam outcomes for dialog and topic resolution because they make the new boundaries inspectable without forcing a broader architecture change.
- Left topic metadata cache records as dict-like rows so existing cache logic and brownfield tests stayed stable while the seam module wrapped those rows in clearer capability results.
- Added injectable loader/fetch/refresh hooks to the seam helpers so `tools.py` remains the brownfield patch point for tests and later adapter thinning.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Restored brownfield monkeypatch seams through the new capability layer**
- **Found during:** Task 2 (Add seam-focused tests and preserve brownfield behavior)
- **Issue:** The first seam extraction called topic loaders and stale-anchor refresh helpers directly inside `capabilities.py`, which bypassed the existing `tools.py` monkeypatch points and broke topic-heavy regression tests.
- **Fix:** Added injectable `load_topics`, `fetch_topic_messages_fn`, and `refresh_topic_by_id_fn` hooks in the seam helpers, then passed the existing tool-level aliases from `tools.py`.
- **Files modified:** `src/mcp_telegram/capabilities.py`, `src/mcp_telegram/tools.py`, `tests/test_capabilities.py`, `tests/test_tools.py`
- **Verification:** `uv run pytest tests/test_capabilities.py -q && uv run pytest tests/test_tools.py -k "list_topics or list_messages or search_messages" -q`
- **Committed in:** `6e7aad4` (part of task commit)

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** The fix was required to preserve the existing brownfield recovery contract through the new seam. No scope creep beyond keeping the extraction behaviorally stable.

## Issues Encountered

- Parallel `git add` calls created transient `.git/index.lock` files twice during staging. The lock cleared after the competing git process exited, and staging was retried sequentially.
- The first in-container import probe ran before the compose rebuild completed, so it hit the stale image once. Re-running the probe after the restart confirmed the new runtime.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 15 now has an explicit capability contract for dialog-target and forum-topic behavior, so later plans can thin adapters instead of extracting more hidden helpers first.
- The live `mcp-telegram` container has been rebuilt and restarted with the new module present, reducing rollout ambiguity for later reflected-surface work.

## Self-Check

PASSED
- Found `.planning/phases/15-capability-seams/15-01-SUMMARY.md`
- Found task commits `b78c5c9`, `6e7aad4`, and `be662f8` in git history

---
*Phase: 15-capability-seams*
*Completed: 2026-03-13*
