# Roadmap: mcp-telegram

## Milestones

- ✅ **v1.0 Core API** - Phases 1-5 (shipped 2026-03-11)
- ✅ **v1.1 Observability & Completeness** - Phases 6-9 (shipped 2026-03-13)
- ✅ **v1.2 MCP Surface Research** - Phases 10-13 (shipped 2026-03-13)
- 🚧 **v1.3 Medium Implementation** - Phases 14-18 (planned)

## Current Milestone

`v1.3 Medium Implementation` turns the Phase 12-13 Medium-path recommendation into a bounded
implementation milestone. The work stays kaizen-bounded: small verified contract improvements,
no speculative Maximal redesign, no default compatibility shims, and cleanup only when it is
material to landing the Medium path safely.

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions between integers

- [ ] **Phase 14: Boundary Recovery** - Preserve actionable server-boundary failure detail for escaped tool errors.
- [ ] **Phase 15: Capability Seams** - Introduce capability-oriented internal seams behind the public tool adapters.
- [ ] **Phase 16: Unified Navigation Contract** - Replace split read/search continuation concepts with one coherent contract.
- [ ] **Phase 17: Direct Read/Search Workflows** - Reduce helper-first choreography while preserving topic and hit-local fidelity.
- [ ] **Phase 18: Surface Posture & Rollout Proof** - Classify primary vs secondary surfaces and prove the new contract in tests, reflection, and the restarted runtime.

## Phase Details

### Phase 14: Boundary Recovery
**Goal**: Maintainers can diagnose escaped tool failures without losing handler-local recovery direction at the server boundary.
**Depends on**: Phase 13
**Requirements**: ERR-01
**Success Criteria** (what must be TRUE):
  1. Maintainer can trigger an unexpected tool failure and observe actionable recovery detail instead of only generic `Tool <name> failed` collapse.
  2. Escaped tool failures identify the failing operation clearly enough that maintainers can choose a next recovery step without reading raw stack traces first.
  3. Brownfield tests make the boundary behavior explicit enough that later contract work can change schemas without reintroducing generic failure collapse.
**Plans**: TBD

### Phase 15: Capability Seams
**Goal**: Maintainers can evolve read, search, and topic behavior through capability-oriented internals rather than tool-name-shaped implementation seams.
**Depends on**: Phase 14
**Requirements**: CAP-01
**Success Criteria** (what must be TRUE):
  1. Maintainer can change shared read/search/topic behavior without first duplicating logic across `ListMessages`, `SearchMessages`, and `ListTopics`.
  2. Public tool adapters stay thin enough that future surface changes can reuse the same underlying capability paths.
  3. Tests or code-level boundaries make the capability seams visible independently of current public tool names.
**Plans**: TBD

### Phase 16: Unified Navigation Contract
**Goal**: LLMs can continue read and search workflows through one coherent navigation model while preserving current fidelity guarantees.
**Depends on**: Phase 15
**Requirements**: NAV-01, NAV-02
**Success Criteria** (what must be TRUE):
  1. LLM can continue either message reads or searches through one shared continuation vocabulary instead of separate `next_cursor`, `next_offset`, and `from_beginning` concepts.
  2. Topic-scoped reads, explicit ambiguity handling, and readable transcript output still work after the navigation contract changes.
  3. Contract tests cover first-page, continuation, and navigation-edge behavior under the new shared model instead of relying on the old split concepts.
**Plans**: TBD

### Phase 17: Direct Read/Search Workflows
**Goal**: LLMs can complete common read and search jobs with fewer helper-first steps while keeping topic fidelity and hit-local context intact.
**Depends on**: Phase 16
**Requirements**: FLOW-01, FLOW-02
**Success Criteria** (what must be TRUE):
  1. LLM can complete a common message-reading job without defaulting to `ListDialogs -> ListTopics -> ListMessages` when it already knows the intended target.
  2. Forum reads become more direct while still preserving explicit topic choice and clear recovery when a topic is ambiguous, inaccessible, or deleted.
  3. LLM can complete common searches with lower orchestration burden while still receiving hit-local context and dialog scoping.
  4. Any remaining helper-step choreography is explicit exception handling rather than the default path for ordinary reads and searches.
**Plans**: TBD

### Phase 18: Surface Posture & Rollout Proof
**Goal**: Maintainers can state which tools are primary versus secondary/helper surfaces and prove the changed contract in both repository and live runtime validation.
**Depends on**: Phase 17
**Requirements**: SURF-01, ROLL-01, ROLL-02
**Success Criteria** (what must be TRUE):
  1. Maintainer can point to code, tests, and planning artifacts that consistently classify current tools as primary or secondary/helper surfaces.
  2. Brownfield tests, reflected local schemas, and restarted-runtime verification all agree on the changed public contract for the affected tools.
  3. Privacy audit and telemetry tests still prove that message content and identifying payloads are not logged after the surface changes.
  4. The rebuilt and restarted runtime exposes the same intended schema and behavior that local contract checks expect.
**Plans**: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 14 -> 15 -> 16 -> 17 -> 18

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 14. Boundary Recovery | 0/TBD | Not started | - |
| 15. Capability Seams | 0/TBD | Not started | - |
| 16. Unified Navigation Contract | 0/TBD | Not started | - |
| 17. Direct Read/Search Workflows | 0/TBD | Not started | - |
| 18. Surface Posture & Rollout Proof | 0/TBD | Not started | - |

## Shipped Milestones

<details>
<summary>✅ v1.0 Core API (Phases 1-5) - SHIPPED 2026-03-11</summary>

- [x] Phase 1: Support Modules (4/4 plans) - completed 2026-03-10
- [x] Phase 2: Tool Updates (4/4 plans) - completed 2026-03-10
- [x] Phase 3: New Tools (2/2 plans) - completed 2026-03-10
- [x] Phase 4: SearchMessages Context Window (2/2 plans) - completed 2026-03-11
- [x] Phase 5: Cache & Error Hardening (2/2 plans) - completed 2026-03-11

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>✅ v1.1 Observability & Completeness (Phases 6-9) - SHIPPED 2026-03-13</summary>

- [x] Phase 6: Telemetry Foundation (4/4 plans) - completed 2026-03-12
- [x] Phase 7: Cache Improvements & Optimization (3/3 plans) - completed 2026-03-12
- [x] Phase 8: Navigation Features (2/2 plans) - completed 2026-03-12
- [x] Phase 9: Forum Topics Support (6/6 plans) - completed 2026-03-12

Full details: `.planning/milestones/v1.1-ROADMAP.md`

</details>

<details>
<summary>✅ v1.2 MCP Surface Research (Phases 10-13) - SHIPPED 2026-03-13</summary>

- [x] Phase 10: Evidence Base & Audit Frame (3/3 plans) - completed 2026-03-13
- [x] Phase 11: Current Surface Comparative Audit (3/3 plans) - completed 2026-03-13
- [x] Phase 12: Redesign Options & Pareto Recommendation (3/3 plans) - completed 2026-03-13
- [x] Phase 13: Implementation Sequencing & Decision Memo (3/3 plans) - completed 2026-03-13

Full details: `.planning/milestones/v1.2-ROADMAP.md`

</details>
