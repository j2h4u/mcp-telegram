---
gsd_state_version: 1.0
milestone: v1.2
milestone_name: MCP Surface Research
current_phase: 13
current_phase_name: implementation sequencing & decision memo
current_plan: Not started
status: verifying
stopped_at: Completed 12-03-PLAN.md
last_updated: "2026-03-13T15:51:06.010Z"
last_activity: 2026-03-13
progress:
  total_phases: 4
  completed_phases: 3
  total_plans: 9
  completed_plans: 9
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-13)

**Core value:** LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup boilerplate before every real task
**Current focus:** Phase 12 complete; ready for Phase 13 implementation-sequencing planning

## Current Position

Current Phase: 13
Current Phase Name: implementation sequencing & decision memo
Total Phases: 4
Current Plan: Not started
Total Plans in Phase: 3
Status: Ready for verification
Last Activity: 2026-03-13
Last Activity Description: Phase 12 complete, transitioned to Phase 13
Progress: [██████████] 100%

## Performance Metrics

| Plan | Duration | Tasks | Files |
| --- | --- | --- | --- |
| Phase 10 P01 | 3min | 4 tasks | 2 files |
| Phase 10 P03 | 3min | 3 tasks | 4 files |
| Phase 11 P02 | 4min | 3 tasks | 5 files |
| Phase 11 P01 | 4min | 3 tasks | 5 files |
| Phase 11-current-surface-comparative-audit P03 | 2min | 3 tasks | 5 files |
| Phase 12 P01 | 1min | 2 tasks | 2 files |
| Phase 12 P02 | 6min | 3 tasks | 2 files |
| Phase 12 P03 | 4min | 2 tasks | 5 files |

## Accumulated Context

### Decisions

- v1.2 is research-only; this roadmap intentionally contains no implementation phases.
- Every v1.2 requirement maps to exactly one phase, starting at Phase 10 after shipped Phase 9.
- The milestone must end in a decision-ready memo grounded in both external best practices and the current codebase reality.
- [Phase 10]: Keep the evidence base narrow and retain only sources that later phases will cite directly.
- [Phase 10]: Treat official MCP and Anthropic docs as normative external guidance, and reflection/code/tests as brownfield authority.
- [Phase 10]: Represent Supporting official and Context only tiers explicitly even when no sources are retained.
- [Phase 10]: Keep the audit frame non-numeric and use strong/mixed/weak bands with named evidence.
- [Phase 10]: Require Phase 11 to audit both individual tools and end-to-end workflows.
- [Phase 10]: Treat the evidence log and brownfield baseline as mandatory inputs for Phases 11-13 rather than methodology to be rebuilt.
- [Phase 11]: Audit workflows as the model experiences them, not only as handler-local behavior.
- [Phase 11]: Treat recovery quality and generic server-boundary failure collapse as separate audit objects.
- [Phase 11]: Express low-level mechanics as a preserve/reduce/remove leak inventory for Phase 12.
- [Phase 11]: Treat the reflected seven-tool runtime inventory from 2026-03-13 as authoritative over stale inherited notes.
- [Phase 11]: Write one structured subsection per tool so contract shape, evidence, preserved strengths, and main leak stay explicit.
- [Phase 11]: Normalize leak labels in the artifact so later workflow and redesign phases can reuse the same categories directly.
- [Phase 11]: End Phase 11 with one standalone comparative audit rather than a loose summary of earlier notes.
- [Phase 11]: Use one synthesis matrix spanning tool-level and workflow-level areas so Phase 12 can compare redesign options directly.
- [Phase 11]: Keep the Phase 12 handoff comparative rather than prescriptive; this plan names pressure but does not choose a redesign path.
- [Phase 12]: Require future option comparisons to use shared dimensions and invariant-aware keep/reshape/merge/demote/remove/rename actions.
- [Phase 12]: Freeze Phase 12 against the reflected seven-tool Phase 11 baseline instead of reopening discovery.
- [Phase 12]: Treat the minimal path as contract cleanup of the existing seven-tool topology rather than a hidden no-op.
- [Phase 12]: Treat the medium path as the capability-oriented Pareto-candidate range that reduces helper-step burden without a full surface rewrite.
- [Phase 12]: Treat the maximal path as the upper-bound stress test for tool-merging, role changes, and result-shape changes rather than the default recommendation.
- [Phase 12]: Choose the Medium Path as the Pareto recommendation because it removes a large share of model burden with the smallest safe change set.
- [Phase 12]: Reject the Minimal Path as too low-impact because it leaves helper-step choreography mostly intact.
- [Phase 12]: Reject the Maximal Path for the next milestone because it overshoots acceptable reflected-contract and runtime risk.
- [Phase 13 prep]: Treat the Medium Path as a migration stage toward a later Maximal redesign, not as the final contract shape.
- [Phase 13 prep]: Do not preserve backward compatibility by default; prefer cleaner contract and sequencing choices over compatibility shims unless a later phase explicitly reintroduces them.

### Pending Todos

- 2 pending in `.planning/todos/pending`
- `2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices.md`
- `2026-03-13-carry-deferred-v1-1-cleanup-and-forum-validation.md`

### Blockers/Concerns

- None.

## Session Continuity

**Last Date:** 2026-03-13T15:44:23.372Z
**Stopped At:** Completed 12-03-PLAN.md
**Resume File:** None
