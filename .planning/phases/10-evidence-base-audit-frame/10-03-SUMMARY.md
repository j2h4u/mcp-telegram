---
phase: 10-evidence-base-audit-frame
plan: 03
subsystem: docs
tags: [mcp, audit, rubric, research, documentation]
requires:
  - phase: 10-01
    provides: "Retained evidence matrix and source-tier hierarchy for later phases"
  - phase: 10-02
    provides: "Brownfield baseline for current-surface behavior, workflow burden, and invariants"
provides:
  - "Reusable audit rubric with five required dimensions and strong/mixed/weak judgment bands"
  - "Phase 11 instructions covering tool-level and workflow-level audit scope"
  - "Practical handoff rules for consuming Phase 10 outputs in Phases 11-13"
affects: [phase-11-current-surface-comparative-audit, phase-12-redesign-options-pareto-recommendation, phase-13-implementation-sequencing-decision-memo]
tech-stack:
  added: []
  patterns: ["Non-numeric audit judgments", "Evidence-paired tool and workflow evaluation"]
key-files:
  created:
    - .planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md
    - .planning/phases/10-evidence-base-audit-frame/10-03-SUMMARY.md
  modified:
    - .planning/STATE.md
    - .planning/ROADMAP.md
key-decisions:
  - "Keep the audit frame non-numeric and use strong/mixed/weak bands with named evidence."
  - "Require Phase 11 to audit both individual tools and end-to-end workflows."
  - "Treat the evidence log and brownfield baseline as mandatory inputs for Phases 11-13 rather than methodology to be rebuilt."
patterns-established:
  - "Each audit dimension names both the evidence to cite and the project-specific meaning of each judgment band."
  - "Later research phases must pair best-practice sources with concrete shipped `mcp-telegram` behaviors."
requirements-completed: [EVID-01]
duration: 4min
completed: 2026-03-13
---

# Phase 10 Plan 03: Audit Frame Summary

**Reusable MCP audit rubric with five project-specific dimensions, strong/mixed/weak judgment bands, and explicit later-phase handoff rules**

## Performance

- **Duration:** 4 min
- **Started:** 2026-03-13T11:51:31Z
- **Completed:** 2026-03-13T11:55:55Z
- **Tasks:** 3
- **Files modified:** 4

## Accomplishments

- Created `.planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md` as the reusable Phase 10 rubric artifact.
- Defined the required five audit dimensions with project-specific evidence expectations and non-numeric `strong`, `mixed`, and `weak` judgment bands.
- Added explicit audit-scope and handoff instructions so Phases 11-13 consume the evidence log and brownfield baseline directly.

## Task Commits

Each task was committed atomically:

1. **Task 1: Define the rubric and judgment bands** - `00968b5` (feat)
2. **Task 2: Add tool-level and workflow-level audit instructions** - `93dc4af` (feat)
3. **Task 3: Write the handoff rules for Phases 11-13** - `62a19fe` (feat)

## Final Rubric Dimensions

- `task-shape fit`
- `metadata/schema clarity`
- `continuation burden`
- `ambiguity recovery`
- `structured-output expectations`

## Judgment-Band Definitions

- `strong`: The current surface supports the task or workflow with clear next steps and deliberate evidence-backed design.
- `mixed`: The current surface works, but the model still absorbs noticeable inference or continuation burden.
- `weak`: The current surface leaves material ambiguity, recovery burden, or hidden mechanics in the model's path.

## Workflow Coverage Expectations

- Phase 11 must audit each current public tool: `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`, and `SearchMessages`.
- Phase 11 must also audit the main user workflows for discovery, reading, search, topic handling, and recovery/error flows.
- Major findings must pair named evidence from the evidence log with concrete `mcp-telegram` behavior instead of generic best-practice prose.

## Phase Handoff Rules

- Phase 11 cites the evidence log and brownfield baseline in every major finding.
- Phase 12 preserves or explicitly challenges baseline invariants when comparing redesign options.
- Phase 13 uses the evidence log and audit frame as direct inputs to the decision memo instead of redoing the audit method.

## Files Created/Modified

- `.planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md` - Reusable rubric, audit instructions, and phase handoff rules.
- `.planning/phases/10-evidence-base-audit-frame/10-03-SUMMARY.md` - Execution summary for Plan 10-03.
- `.planning/STATE.md` - Phase/progress metadata updated after completion.
- `.planning/ROADMAP.md` - Phase 10 plan-progress row updated after completion.

## Decisions Made

- Kept the rubric non-numeric to avoid false precision in a research-only milestone.
- Treated discovery freshness limits and generic `Tool <name> failed` wrapping as explicit audit concerns rather than hidden caveats.
- Forced later phases to audit workflows alongside individual tools so discovery, pagination, and recovery burden remain visible.

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Phase 11 now has a fixed rubric, evidence posture, and audit-scope checklist for the current MCP surface.
- Phases 12 and 13 inherit explicit reuse rules, reducing the chance of drift back into generic methodology work.

## Self-Check: PASSED

- Found `.planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md`
- Found `.planning/phases/10-evidence-base-audit-frame/10-03-SUMMARY.md`
- Found task commits `00968b5`, `93dc4af`, and `62a19fe` in git history
