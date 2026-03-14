---
phase: 12
slug: redesign-options-pareto-recommendation
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-13
---

# Phase 12 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | shell-based artifact verification using `rg`, `test`, and local CLI reflection |
| **Config file** | none — validation is document, code-anchor, and runtime-contract oriented |
| **Quick run command** | `test -f .planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md && rg -n "Comparison Dimensions|preserved-strength retention|state-model impact|Public Contract Delta Rules|GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|discovery-first flow|disambiguation retry flow|topic-selection flow|pagination flow|text-first result parsing|generic server-boundary failure behavior|keep|reshape|merge|demote|remove|rename" .planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md && ( test ! -f .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md || rg -n "Minimal Path|Medium Path|Maximal Path|impact|migration risk|implementation scope|preserved invariants|Public Contract Delta Inventory|affected invariants" .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md ) && ( test ! -f .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md || rg -n "Pareto Recommendation|chosen path|rejected alternative|large share of model burden|smallest safe change set|explicit ambiguity handling|Phase 13 Handoff Notes" .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md )` |
| **Full suite command** | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages" && test -f .planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md && rg -n "Comparison Dimensions|Public Contract Delta Rules|read-only|privacy-safe telemetry" .planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md && ( test ! -f .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md || rg -n "Minimal Path|Medium Path|Maximal Path|Public Contract Delta Inventory" .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md ) && ( test ! -f .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md || rg -n "Option Matrix|Public Contract Delta Inventory|minimal|medium|maximal|keep|reshape|merge|demote|remove|Pareto Recommendation|Phase 13 Handoff Notes|read-only scope|privacy-safe telemetry|recovery-critical state" .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md )` |
| **Final verification command** | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages" && test -f .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md && rg -n "Scope and Decision Posture|Option Matrix|Public Contract Delta Inventory|minimal|medium|maximal|keep|reshape|merge|demote|remove|Pareto Recommendation|chosen path|rejected alternative|large share of model burden|smallest safe change set|read-only scope|privacy-safe telemetry|recovery-critical state|explicit ambiguity handling|Phase 13 Handoff Notes" .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full command
- **Before `$gsd-verify-work`:** Run the final verification command; the primary deliverable and reflected tool list must both pass
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 12-01-01 | 01 | 1 | OPTION-01 | doc | `rg -n "Comparison Dimensions|user-task fit|continuation-contract simplicity|contract delta size|migration risk|implementation scope|preserved-strength retention|recovery quality|output-shape burden|state-model impact|operational/runtime risk" .planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md` | ❌ W1 | ⬜ pending |
| 12-01-02 | 01 | 1 | OPTION-02 | doc | `rg -n "Public Contract Delta Rules|GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|discovery-first flow|disambiguation retry flow|topic-selection flow|pagination flow|text-first result parsing|generic server-boundary failure behavior|dialog|topic|sender|cursor|offset|from_beginning|exclude_archived|ignore_pinned|unread|keep|reshape|merge|demote|remove|rename|rationale|affected invariants" .planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md` | ❌ W1 | ⬜ pending |
| 12-02-01 | 02 | 2 | OPTION-01 | doc | `rg -n "Minimal Path|seven-tool topology|metadata cleanup|continuation normalization|error-surface cleanup|impact|migration risk|implementation scope|preserved invariants" .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md` | ❌ W2 | ⬜ pending |
| 12-02-02 | 02 | 2 | OPTION-01 | doc | `rg -n "Medium Path|capability-oriented workflows|helper-step burden|consolidation|impact|migration risk|implementation scope|preserved invariants|read-only|stateful" .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md` | ❌ W2 | ⬜ pending |
| 12-02-03 | 02 | 2 | OPTION-02 | doc | `rg -n "Maximal Path|tool-merging|role changes|result-shape changes|Public Contract Delta Inventory|GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|keep|reshape|merge|demote|remove|rename|affected invariants|operational risk" .planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md` | ❌ W2 | ⬜ pending |
| 12-03-01 | 03 | 3 | RECO-01 | doc | `rg -n "Pareto Recommendation|chosen path|rejected alternative|large share of model burden|smallest safe change set|read-only scope|privacy-safe telemetry|recovery-critical state|explicit ambiguity handling" .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md` | ❌ W3 | ⬜ pending |
| 12-03-02 | 03 | 3 | OPTION-02 | doc | `rg -n "Option Matrix|Public Contract Delta Inventory|minimal|medium|maximal|GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|keep|reshape|merge|demote|remove|disambiguation|pagination|generic server-boundary failure|Phase 13 Handoff Notes" .planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md` | ❌ W3 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 stubs are required for this research-only phase.
Execution should keep the shell-based coverage checks current as the redesign artifact is populated.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Options are genuinely distinct and not just effort-labeled rewrites | OPTION-01 | Requires judgment about whether minimal, medium, and maximal imply materially different surface shapes | Read the option matrix and confirm each tier changes topology, burden, or contract shape in a substantively different way |
| Contract actions are evidence-backed rather than speculative | OPTION-02 | Requires editorial review that keep/reshape/merge/demote/remove calls trace back to Phase 10/11 evidence and current code/runtime anchors | Review representative rows in the delta inventory and confirm each action is justified by the comparative audit or direct brownfield anchors |
| The recommendation is Pareto-shaped and evidence-backed rather than framed as abstract “best overall” advice | RECO-01 | Requires judgment on tradeoff quality, rejected-alternative handling, and whether the rationale traces back to retained evidence or direct brownfield anchors | Read the recommendation section and confirm it names a rejected alternative, explains why the chosen path removes a large share of model burden with the smallest safe change set, and cites Phase 10/11 artifacts or direct code/runtime anchors for its rationale |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent shell validation
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
