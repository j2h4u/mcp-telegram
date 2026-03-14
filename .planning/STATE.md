---
gsd_state_version: 1.0
milestone: v1.3
milestone_name: Medium Implementation
current_phase: 18
current_phase_name: Surface Posture Rollout Proof
current_plan: 2
status: in-progress
stopped_at: "Completed 18-02-PLAN.md (Phase 18, Plan 02: Rollout Proof and UAT Checklist)"
last_updated: "2026-03-14T21:30:00.000Z"
last_activity: 2026-03-14
progress:
  total_phases: 5
  completed_phases: 4
  total_plans: 15
  completed_plans: 14
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-14)

**Core value:** LLM can work with Telegram using natural names - zero cold-start friction, no ID lookup boilerplate before every real task
**Current focus:** Phase 17 `Direct Read/Search Workflows` is complete including runtime gap-closure Plan 17-04; next work can move to Phase 18 rollout posture once verification/closeout is consumed.

## Current Position

Current Phase: 18
Current Phase Name: Surface Posture Rollout Proof
Total Phases: 18
Current Plan: 2
Total Plans in Phase: 4
Status: In Progress
Last Activity: 2026-03-14
Last Activity Description: Completed Plan 18-02 repo-local contract proofs, analytics refresh, and rollout UAT checklist
Progress: [██████████] 100%

Phase: 18 of 18 (`Surface Posture Rollout Proof`)
Plan: 2 of 4 in current phase
Status: In Progress (awaiting Plan 03 container rebuild and runtime verification)
Last activity: 2026-03-14 - Completed Plan 18-02 with 12 new test functions, privacy audit verified, 18-UAT.md created with rollout checklist
Progress: [████████░░] 50%

## Performance Metrics

**Velocity:**
- Total plans completed: 18
- Average duration: 4.5 min
- Total execution time: 77 min

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
| Phase 17 P03 | 9m | 3 tasks | 7 files |
| Phase 17 P04 | 46 min | 3 tasks | 3 files |
| Phase 18 P01 | 15 | 3 tasks | 4 files |

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
- [Phase 17]: Kept SearchMessages on the existing dialog/query contract and treated signed numeric dialog strings as the exact-target fast path.
- [Phase 17]: Moved hit-window grouping and hit markers into formatter/capability helpers so the adapter only handles MCP framing and telemetry.
- [Phase 17]: Serialize shared cache bootstrap with a lock file and dedicated connection so parallel MCP sessions do not contend on constructor-time schema or journal setup.
- [Phase 17]: Keep the cache lock fix bounded to `cache.py`; direct read/search contract and tool adapters stay unchanged while closing the runtime gap.
- [Phase 18]: Posture as code-level constant (TOOL_POSTURE dict) + reflected teaching via prefix tags = single unified source across planning/code/tests
- [Phase 18]: Plan 02 proves posture contract via repo-local brownfield tests (12 new assertions), analytics semantics (telemetry invariant to posture), and 18-UAT.md checklist for reproducible runtime verification. No behavioral changes; Phase 17 direct workflows remain default path.

### Pending Todos

- 2 pending in `.planning/todos/pending`
- `2026-03-13-refactor-mcp-tool-surface-around-capability-oriented-best-practices.md`
- `2026-03-13-carry-deferred-v1-1-cleanup-and-forum-validation.md`

### Blockers/Concerns

- No blockers. Phase 18 Plan 02 complete; next step is Plan 03 container rebuild and runtime verification.

## Session Continuity

Last session: 2026-03-14T21:30:00.000Z
Stopped at: Completed 18-02-PLAN.md (Phase 18, Plan 02: Rollout Proof and UAT Checklist)
Resume file: None
