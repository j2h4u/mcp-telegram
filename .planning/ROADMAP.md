# Roadmap: mcp-telegram

## Milestones

- ✅ **v1.0 Core API** — Phases 1–5 (shipped 2026-03-11)
- ✅ **v1.1 Observability & Completeness** — Phases 6–9 (shipped 2026-03-13)
- 📋 **v1.2 MCP Surface Research** — Phases 10–13 (planned)

## Overview

v1.2 is a research-only milestone. It starts from authoritative MCP and Anthropic guidance plus the
actual `mcp-telegram` brownfield surface, then moves through a grounded current-state audit, a
minimal/medium/maximal redesign comparison, and a final decision memo with sequencing and
validation guidance for the later implementation milestone.

## Archives

- `.planning/milestones/v1.0-ROADMAP.md`
- `.planning/milestones/v1.0-REQUIREMENTS.md`
- `.planning/milestones/v1.1-ROADMAP.md`
- `.planning/milestones/v1.1-REQUIREMENTS.md`
- `.planning/milestones/v1.0-MILESTONE-AUDIT.md`
- `.planning/milestones/v1.1-MILESTONE-AUDIT.md`

## Phases

**Phase Numbering:**
- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions between integers

<details>
<summary>✅ v1.0 Core API (Phases 1–5) — SHIPPED 2026-03-11</summary>

- [x] Phase 1: Support Modules (4/4 plans) — completed 2026-03-10
- [x] Phase 2: Tool Updates (4/4 plans) — completed 2026-03-10
- [x] Phase 3: New Tools (2/2 plans) — completed 2026-03-10
- [x] Phase 4: SearchMessages Context Window (2/2 plans) — completed 2026-03-11
- [x] Phase 5: Cache & Error Hardening (2/2 plans) — completed 2026-03-11

Full details: `.planning/milestones/v1.0-ROADMAP.md`

</details>

<details>
<summary>✅ v1.1 Observability & Completeness (Phases 6–9) — SHIPPED 2026-03-13</summary>

- [x] Phase 6: Telemetry Foundation (4/4 plans) — completed 2026-03-12
- [x] Phase 7: Cache Improvements & Optimization (3/3 plans) — completed 2026-03-12
- [x] Phase 8: Navigation Features (2/2 plans) — completed 2026-03-12
- [x] Phase 9: Forum Topics Support (6/6 plans) — completed 2026-03-12

Full details: `.planning/milestones/v1.1-ROADMAP.md`

</details>

### 📋 v1.2 MCP Surface Research (Planned)

- [x] **Phase 10: Evidence Base & Audit Frame** - Establish the source hierarchy, audit rubric, and brownfield constraints for the current MCP surface. (completed 2026-03-13)
- [x] **Phase 11: Current Surface Comparative Audit** - Produce a grounded audit of the current tools and workflows against named best-practice sources and code reality. (completed 2026-03-13)
- [ ] **Phase 12: Redesign Options & Pareto Recommendation** - Compare minimal, medium, and maximal redesign paths and select the highest-leverage safe direction.
- [ ] **Phase 13: Implementation Sequencing & Decision Memo** - Turn the research into sequencing, validation, and open-question guidance for the future implementation milestone.

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Support Modules | v1.0 | 4/4 | Complete | 2026-03-10 |
| 2. Tool Updates | v1.0 | 4/4 | Complete | 2026-03-10 |
| 3. New Tools | v1.0 | 2/2 | Complete | 2026-03-10 |
| 4. SearchMessages Context Window | v1.0 | 2/2 | Complete | 2026-03-11 |
| 5. Cache & Error Hardening | v1.0 | 2/2 | Complete | 2026-03-11 |
| 6. Telemetry Foundation | v1.1 | 4/4 | Complete | 2026-03-12 |
| 7. Cache Improvements & Optimization | v1.1 | 3/3 | Complete | 2026-03-12 |
| 8. Navigation Features | v1.1 | 2/2 | Complete | 2026-03-12 |
| 9. Forum Topics Support | v1.1 | 6/6 | Complete | 2026-03-12 |
| 10. Evidence Base & Audit Frame | v1.2 | 3/3 | Complete    | 2026-03-13 |
| 11. Current Surface Comparative Audit | v1.2 | 3/3 | Complete   | 2026-03-13 |
| 12. Redesign Options & Pareto Recommendation | v1.2 | 0/TBD | Not started | - |
| 13. Implementation Sequencing & Decision Memo | v1.2 | 0/TBD | Not started | - |

## Phase Details

### Phase 10: Evidence Base & Audit Frame
**Goal**: Maintainer has a source-ranked evidence base and audit frame for evaluating the current MCP surface without drifting into a vague literature review.
**Depends on**: Nothing (first phase of v1.2; audits the shipped v1.1 surface)
**Requirements**: EVID-01
**Success Criteria** (what must be TRUE):
1. Research materials clearly separate authoritative MCP and Anthropic guidance from supporting secondary or community guidance.
2. The evidence log records which named sources materially shape the later conclusions and why they apply to `mcp-telegram`.
3. An audit rubric exists for judging each current tool and workflow on task-shape, metadata quality, continuation burden, ambiguity recovery, and structured-output expectations.
4. Brownfield constraints from the live codebase are captured up front, including read-only scope, reflection-based tool exposure, and text-first result conventions.
**Plans**: 01-03 complete

### Phase 11: Current Surface Comparative Audit
**Goal**: Maintainer can review a grounded comparative audit of the current MCP surface from the LLM-facing perspective.
**Depends on**: Phase 10
**Requirements**: AUDIT-01, AUDIT-02, AUDIT-03
**Success Criteria** (what must be TRUE):
1. The audit covers each current public tool and the main user workflows for discovery, reading, search, topic handling, and recovery/error flows.
2. Findings tie each major strength or weakness to named evidence and to specific current-surface behaviors in `tools.py` and `server.py`.
3. The audit explicitly identifies where the public contract leaks low-level mechanics or helper-step burden to the model, including pagination, disambiguation, and tool choreography.
4. The deliverable summarizes current-state strengths, gaps, and preserved invariants in a decision-friendly comparison matrix or equivalent format.
**Plans**: 01-03 complete

### Phase 12: Redesign Options & Pareto Recommendation
**Goal**: Maintainer can compare redesign paths and review one evidence-backed Pareto recommendation for the next milestone.
**Depends on**: Phase 11
**Requirements**: OPTION-01, OPTION-02, RECO-01
**Success Criteria** (what must be TRUE):
1. An option matrix defines minimal, medium, and maximal redesign paths with expected impact, migration risk, and implementation scope.
2. Each option makes clear which current tools, parameters, and interaction patterns it would keep, reshape, merge, demote, or remove from the public contract.
3. One Pareto recommendation is named explicitly, with rationale for why its smaller safe change set should deliver outsized model-usage impact.
4. The recommendation calls out the invariants that should not be casually broken, including read-only scope, privacy-safe telemetry, and recovery-critical state.
**Plans**: TBD

### Phase 13: Implementation Sequencing & Decision Memo
**Goal**: Maintainer has a decision-ready memo that turns the research into a sequenced, validate-able implementation brief for the follow-up milestone.
**Depends on**: Phase 12
**Requirements**: RECO-02, EVID-02
**Success Criteria** (what must be TRUE):
1. The final memo consolidates the audit, option tradeoffs, and selected recommendation into one decision-ready deliverable rather than disconnected notes.
2. The memo includes recommended implementation sequencing, migration checkpoints, and runtime validation guidance for the future build milestone.
3. The memo names open questions, risks, and evaluation criteria that should be resolved before coding begins.
4. The deliverable is actionable enough that the next implementation milestone can be planned directly from it without rerunning the source audit or redesign comparison.
**Plans**: TBD
