---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: Medium Implementation
current_phase: 17
current_phase_name: Direct Read/Search Workflows
current_plan: 3
status: ready_to_execute
stopped_at: Completed 17-02-PLAN.md
last_updated: "2026-03-14T10:28:27Z"
last_activity: 2026-03-14
progress:
  total_phases: 5
  completed_phases: 3
  total_plans: 11
  completed_plans: 10
  percent: 91
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-14)

**Core value:** LLM can work with Telegram using natural names - zero cold-start friction, no ID lookup boilerplate before every real task
**Current focus:** Phase 17 `Direct Read/Search Workflows` for milestone `v1.3 Medium Implementation`

## Current Position

Current Phase: 17
Current Phase Name: Direct Read/Search Workflows
Total Phases: 18
Current Plan: 3
Total Plans in Phase: 3
Status: Ready to execute
Last Activity: 2026-03-14
Last Activity Description: Completed Plan 17-02 direct ListMessages workflow, schema reflection proof, and forum-fidelity regressions
Progress: [█████████░] 91%

Phase: 17 of 18 (`Direct Read/Search Workflows`)
Plan: 3 of 3 in current phase
Status: Ready to execute
Last activity: 2026-03-14 - Completed Plan 17-02 direct ListMessages workflow, schema reflection proof, and forum-fidelity regressions
Progress: [█████████░] 91%

## Performance Metrics

**Velocity:**
- Total plans completed: 16
- Average duration: 4.3 min
- Total execution time: 68 min

**By Milestone:**

| Milestone | Plans | Total | Avg/Plan |
|-----------|-------|-------|----------|
| v1.2 | 11 | 35 min | 3.2 min |
| Phase 14 | 2 | 14 min | 7.0 min |
| Phase 15 P01 | 12m39s | 3 tasks | 4 files |
| Phase 15 P02 | 2m | 3 tasks | 4 files |
| Phase 15 P03 | 2m28s | 3 tasks | 4 files |
| Phase 16 P01 | 14m | 2 tasks | 4 files |
| Phase 16 P02 | 10m | 2 tasks | 6 files |
| Phase 16 P3 | 26 min | 3 tasks | 7 files |
| Phase 17 P01 | 8 min | 2 tasks | 4 files |
| Phase 17 P02 | 11 min | 2 tasks | 4 files |

## Accumulated Context

### Decisions

- v1.3 follows the bounded Medium path from Phase 13; no speculative Maximal scope enters by default.
- Public-schema changes must pass brownfield tests, reflected local schemas, and restarted-runtime verification.
- Backward-compatibility shims are out unless a concrete client constraint forces them back in.
- Helper-tool posture decisions come after primary read/search workflows are simplified.
- [Phase 14]: Plan 14-01 adds red server.call_tool contract tests first so the escaped boundary can change without losing stage distinctions.
- [Phase 14]: Unknown-tool failures remain outside the escaped boundary wrapper; Phase 14 stays scoped to escaped internal tool failures.
- [Phase 14]: Keep the fix bounded to server.py with one helper instead of introducing a new cross-repo exception framework.
- [Phase 14]: Handle tool_args and tool_runner failures in separate branches so validation and runtime stages return different actionable guidance.
- [Phase 15]: Represent dialog and forum-topic seams as explicit typed outcomes without introducing a new service framework.
- [Phase 15]: Keep topic metadata cache rows dict-backed, then wrap them in seam result objects so the extraction stays bounded and inspectable.
- [Phase 15]: Allow tool adapters to inject topic loaders and stale-anchor refresh helpers into the seam to preserve existing brownfield tests and recovery behavior.
- [Phase 15]: Keep ListTopics and ListMessages responsible only for MCP args, telemetry, and final text assembly.
- [Phase 15]: Move history-read cursor handling, sender resolution, topic recovery, and enrichment hooks into capabilities.py so SearchMessages can reuse them later.
- [Phase 15]: Prove adapter thinness explicitly with tests that patch the capability execution entrypoints instead of only asserting public behavior.
- [Phase 15]: Keep SearchMessages output shaping local while moving shared resolution, enrichment, and offset handling into capabilities.py.
- [Phase 15]: Prove CAP-01 with capability-level and adapter-level tests instead of brittle tool-body heuristics.
- [Phase 15]: Require rebuilt-container verification to confirm the live image exposes the final shared search seam.
- [Phase 16]: Plan 16-01 keeps the unified navigation contract internal to capabilities while ListMessages and SearchMessages retain legacy cursor/offset adapters.
- [Phase 16]: Plan 16-01 scopes shared history tokens by dialog/topic and shared search tokens by dialog/query so mismatched reuse fails before Telegram paging runs.
- [Phase 16]: Use one string navigation field for ListMessages, with newest/oldest first-page keywords and opaque next_navigation continuation tokens.
- [Phase 16]: Encode history direction into shared navigation tokens so oldest-first pagination can continue through the same public field without reintroducing from_beginning.
- [Phase 16]: SearchMessages now uses the same navigation and next_navigation vocabulary as ListMessages instead of offset and next_offset. — Closes NAV-01 with one shared public continuation contract across read and search flows.
- [Phase 16]: Live runtime proof remains mandatory for reflected schema changes, so Phase 16 closes only after the long-lived container is rebuilt and checked in-container. — Repo-local reflection can drift from the serving container; rebuilt-runtime verification is the release gate for schema work.
- [Phase 17]: Exact dialog and topic selectors stay internal and opt-in so name-based ambiguity handling remains unchanged until later Phase 17 plans expose public fields.
- [Phase 17]: Exact topic resolution prefers cached metadata and falls back to one topic-by-id refresh, preserving deleted-topic tombstones and the existing fetch/recovery path once a target is known.
- [Phase 17]: ListMessages should expose exact dialog/topic selectors directly, but mixed fuzzy and exact selector inputs must fail validation instead of silently choosing one path.
- [Phase 17]: Direct forum reads must stay on the shared capability seam so deleted-topic, inaccessible-topic, General, unread, and navigation behavior remain aligned with the brownfield path.

### Pending Todos

- 2 pending in `.planning/todos/pending`
- `2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices.md`
- `2026-03-13-carry-deferred-v1-1-cleanup-and-forum-validation.md`

### Blockers/Concerns

- No blockers. Phase 17 is in progress with Plan 03 next.

## Session Continuity

Last session: 2026-03-14T10:28:27Z
Stopped at: Completed 17-02-PLAN.md
Resume file: None
