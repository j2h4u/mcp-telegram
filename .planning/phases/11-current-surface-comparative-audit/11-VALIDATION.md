---
phase: 11
slug: current-surface-comparative-audit
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-13
---

# Phase 11 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | shell-based artifact verification using `rg`, `test`, and local CLI reflection |
| **Config file** | none — validation is document, code-anchor, and runtime-contract oriented |
| **Quick run command** | `rg -n "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|next_cursor|next_offset|from_beginning|Tool <name> failed|TextContent" .planning/phases/11-current-surface-comparative-audit/*.md` |
| **Full suite command** | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools && rg -n "discovery|reading|search|topic handling|recovery|error flows|AUDIT-01|AUDIT-02|AUDIT-03|strength|gap|invariant" .planning/phases/11-current-surface-comparative-audit/*.md` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full command
- **Before `$gsd-verify-work`:** Full command must pass and the reflected tool list must still
  match the audit scope
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 11-01-01 | 01 | 1 | AUDIT-01 | runtime/doc | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages"` | ❌ W1 | ⬜ pending |
| 11-01-02 | 01 | 1 | AUDIT-01 | doc | `rg -n "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages" .planning/phases/11-current-surface-comparative-audit/11-TOOL-AUDIT.md` | ❌ W1 | ⬜ pending |
| 11-01-03 | 01 | 1 | AUDIT-03 | doc | `rg -n "metadata|schema|TextContent|next_cursor|next_offset|from_beginning|ListTopics|Tool <name> failed" .planning/phases/11-current-surface-comparative-audit/11-TOOL-AUDIT.md` | ❌ W1 | ⬜ pending |
| 11-02-01 | 02 | 1 | AUDIT-02 | doc | `rg -n "discovery|reading|search|topic handling|recovery|error flows" .planning/phases/11-current-surface-comparative-audit/11-WORKFLOW-AUDIT.md` | ❌ W1 | ⬜ pending |
| 11-02-02 | 02 | 1 | AUDIT-03 | doc | `rg -n "next_cursor|next_offset|from_beginning|ambiguity|not found|invalid cursor|ListTopics|Tool <name> failed" .planning/phases/11-current-surface-comparative-audit/11-WORKFLOW-AUDIT.md` | ❌ W1 | ⬜ pending |
| 11-03-01 | 03 | 2 | AUDIT-01 | doc | `rg -n "strength|gap|invariant|comparison matrix|Phase 12" .planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md` | ❌ W2 | ⬜ pending |
| 11-03-02 | 03 | 2 | AUDIT-02 | doc | `rg -n "tool-level|workflow-level|discovery|reading|search|topic handling|recovery" .planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md` | ❌ W2 | ⬜ pending |
| 11-03-03 | 03 | 2 | AUDIT-03 | doc | `rg -n "helper-step burden|low-level mechanics|pagination|disambiguation|tool choreography" .planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md` | ❌ W2 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 stubs are required for this research-only phase.
Execution should keep the shell-based coverage checks current as each audit artifact lands.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Findings are grounded rather than generic | AUDIT-01 | Requires judgment about whether each finding pairs named Phase 10 evidence with direct current-surface anchors | Review each major finding and confirm it cites both retained evidence and code/tests/runtime anchors |
| Workflow burden is evaluated as user-visible choreography, not just handler-local behavior | AUDIT-02 | Requires editorial review of end-to-end flow coverage | Read the workflow audit and confirm it covers discovery, reading, search, topic handling, and recovery/error flows explicitly |
| Final audit preserves invariants while surfacing redesign pressure | AUDIT-03 | Requires checking distinction between current-state facts and future-option pressure | Review `11-COMPARATIVE-AUDIT.md` and confirm read-only scope, privacy-safe telemetry, recovery-critical state, and tests-as-contract stay separated from critique |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent shell validation
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
