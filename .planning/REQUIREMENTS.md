# Requirements: mcp-telegram

**Defined:** 2026-03-14
**Milestone:** v1.3 — Medium Implementation
**Core Value:** LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup boilerplate before every real task

## v1.3 Requirements

### Error Surface

- [x] **ERR-01**: Maintainer can observe actionable recovery detail from unexpected tool failures instead of generic `Tool <name> failed` collapse at the server boundary.

### Capability Layer

- [x] **CAP-01**: Maintainer can evolve read, search, and topic behavior through capability-oriented internals instead of binding implementation seams directly to current public tool names.

### Navigation

- [x] **NAV-01**: LLM can continue both read and search workflows through one coherent continuation vocabulary instead of separate `next_cursor`, `next_offset`, and `from_beginning` concepts.
- [x] **NAV-02**: Topic fidelity, ambiguity handling, and readable transcript behavior remain preserved while the continuation contract changes.

### Workflow Shape

- [ ] **FLOW-01**: LLM can complete common message-reading jobs with fewer helper-first steps than the current `ListDialogs -> ListTopics -> ListMessages` choreography.
- [ ] **FLOW-02**: LLM can complete common search workflows with lower orchestration burden while preserving hit-local context and dialog scoping.

### Surface Posture

- [ ] **SURF-01**: Maintainer can classify current tools as primary or secondary/helper surfaces in code, tests, and planning artifacts after the primary workflows are reshaped.

### Rollout Safety

- [ ] **ROLL-01**: Maintainer can prove contract-affecting changes through brownfield tests, reflected local schemas, and restarted-runtime verification against the live container.
- [ ] **ROLL-02**: Maintainer can prove telemetry remains privacy-safe after the surface changes.

## v2 Requirements

### Follow-On Work

- **CLEANUP-01**: Address deferred `v1.1` cleanup and large-forum validation as a follow-up milestone unless implementation proves they are required immediately.
- **MAX-01**: Revisit the broader Maximal-path surface redesign after the Medium migration lands cleanly.
- **EVAL-01**: Add a dedicated eval or benchmark harness for tracking model-burden reduction over time.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Backward-compatibility shims by default | The milestone is intentionally biased toward a cleaner Medium contract unless a concrete client constraint appears. |
| Full Maximal redesign | Too large for the bounded implementation posture chosen for v1.3. |
| Deferred `v1.1` cleanup as automatic scope | Cleanup stays out unless implementation work proves it is necessary to land the Medium path safely. |
| Research-only deliverables | `v1.2` already produced the audit, option comparison, and implementation memo this milestone executes against. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| ERR-01 | Phase 14 | Complete |
| CAP-01 | Phase 15 | Complete |
| NAV-01 | Phase 16 | Complete |
| NAV-02 | Phase 16 | Complete |
| FLOW-01 | Phase 17 | Pending |
| FLOW-02 | Phase 17 | Pending |
| SURF-01 | Phase 18 | Pending |
| ROLL-01 | Phase 18 | Pending |
| ROLL-02 | Phase 18 | Pending |

**Coverage:**
- v1.3 requirements: 9 total
- Mapped to phases: 9
- Unmapped: 0

---
*Requirements defined: 2026-03-14*
*Last updated: 2026-03-14 after roadmap creation*
