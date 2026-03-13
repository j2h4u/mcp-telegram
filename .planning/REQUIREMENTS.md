# Requirements: mcp-telegram

**Defined:** 2026-03-13
**Milestone:** v1.2 — MCP Surface Research
**Core Value:** LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup boilerplate before every real task

## v1.2 Requirements

### Comparative Audit

- [ ] **AUDIT-01**: Maintainer can review a grounded comparison of the current MCP tool surface against MCP and Anthropic best practices, with findings tied to named sources.
- [ ] **AUDIT-02**: Maintainer can review the current tool surface both tool-by-tool and workflow-by-workflow, including discovery, reading, search, topic handling, and recovery/error flows.
- [ ] **AUDIT-03**: Maintainer can identify where the current public surface leaks low-level mechanics to the model unnecessarily, including pagination, disambiguation, and helper-step burden.

### Refactor Options

- [ ] **OPTION-01**: Maintainer can compare minimal, medium, and maximal redesign paths for the public MCP surface, including expected impact, migration risk, and implementation scope.
- [ ] **OPTION-02**: Maintainer can see which current tools, parameters, and interaction patterns each redesign path would likely keep, reshape, merge, demote, or remove from the public contract.

### Recommendation

- [ ] **RECO-01**: Maintainer can review one Pareto-style recommendation that targets the highest likely model-usage impact with the smallest safe change set.
- [ ] **RECO-02**: Maintainer can review a recommended next implementation path, including sequencing, validation concerns, and open questions that should be resolved before coding.

### Research Quality

- [ ] **EVID-01**: The milestone distinguishes authoritative guidance from supporting secondary/community guidance and records which sources materially shaped the conclusions.
- [ ] **EVID-02**: The final deliverable is actionable for a future implementation milestone and does not stop at abstract best-practice summaries.

## v2 Requirements

### Follow-On Work

- **IMPL-01**: Execute the chosen MCP tool-surface refactor in code.
- **IMPL-02**: Run post-refactor evals against realistic LLM workflows and confirm the new surface reduces agent burden.
- **CLEANUP-01**: Address deferred v1.1 cleanup and large-forum validation work if it materially affects the chosen redesign path.

## Out of Scope

| Feature | Reason |
|---------|--------|
| Public tool-surface refactor implementation | This milestone is for research, audit, and guidance, not code changes to the MCP contract. |
| Large-scale prototype or live migration | Would dilute the research deliverable and bias the option analysis toward one path too early. |
| Telegram backend rewrite | The focus is the model-facing MCP surface, not replacing Telethon integration or core read-only architecture. |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| AUDIT-01 | Pending | Pending |
| AUDIT-02 | Pending | Pending |
| AUDIT-03 | Pending | Pending |
| OPTION-01 | Pending | Pending |
| OPTION-02 | Pending | Pending |
| RECO-01 | Pending | Pending |
| RECO-02 | Pending | Pending |
| EVID-01 | Pending | Pending |
| EVID-02 | Pending | Pending |

**Coverage:**
- v1.2 requirements: 9 total
- Mapped to phases: 0
- Unmapped: 9 ⚠️

---
*Requirements defined: 2026-03-13*
*Last updated: 2026-03-13 after initial definition*
