---
phase: 10
slug: evidence-base-audit-frame
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-13
---

# Phase 10 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | shell-based artifact verification using `rg`, `test`, and local CLI reflection |
| **Config file** | none — validation is document and runtime-contract oriented |
| **Quick run command** | `rg -n "Primary external|Brownfield authority|Supporting official|Context only|ListTopics|from_beginning|\\[HIT\\]|exclude_archived|previously_inaccessible" .planning/phases/10-evidence-base-audit-frame/10-EVIDENCE-LOG.md .planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md` |
| **Full suite command** | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools && rg -n "ListTopics|list_prompts|list_resources|list_resource_templates|next_cursor|next_offset|from_beginning|\\[HIT\\]|exclude_archived|previously_inaccessible|Tool <name> failed" .planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md && ( test ! -f .planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md || rg -n "task-shape fit|metadata/schema clarity|continuation burden|ambiguity recovery|structured-output expectations|strong|mixed|weak|Phase 11|Phase 12|Phase 13" .planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md )` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full command
- **Before `$gsd-verify-work`:** Full command must pass and the reflected tool list must still match the baseline
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 10-01-01 | 01 | 1 | EVID-01 | doc | `rg -n "Primary external|Brownfield authority|Supporting official|Context only|Later consumers" .planning/phases/10-evidence-base-audit-frame/10-EVIDENCE-LOG.md` | ❌ W1 | ⬜ pending |
| 10-01-02 | 01 | 1 | EVID-01 | doc | `rg -n "MCP Tools spec|Anthropic implement-tool-use|Anthropic tool-use overview" .planning/phases/10-evidence-base-audit-frame/10-EVIDENCE-LOG.md` | ❌ W1 | ⬜ pending |
| 10-01-03 | 01 | 1 | EVID-01 | doc | `rg -n "Supporting official|Context only|secondary|community|none retained" .planning/phases/10-evidence-base-audit-frame/10-EVIDENCE-LOG.md` | ❌ W1 | ⬜ pending |
| 10-02-01 | 02 | 1 | EVID-01 | runtime/doc | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "ListTopics|ListDialogs|ListMessages|SearchMessages|GetUserInfo|GetUsageStats|GetMyAccount"` | ❌ W1 | ⬜ pending |
| 10-02-02 | 02 | 1 | EVID-01 | doc | `rg -n "reflection-based|list_prompts|list_resources|list_resource_templates|text-first|next_cursor|next_offset|from_beginning|\\[HIT\\]|exclude_archived|previously_inaccessible|Tool <name> failed|read-only" .planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md` | ❌ W1 | ⬜ pending |
| 10-03-01 | 03 | 2 | EVID-01 | doc | `rg -n "task-shape fit|metadata/schema clarity|continuation burden|ambiguity recovery|structured-output expectations" .planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md` | ❌ W2 | ⬜ pending |
| 10-03-02 | 03 | 2 | EVID-01 | doc | `rg -n "strong|mixed|weak|Phase 11|Phase 12|Phase 13" .planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md` | ❌ W2 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 stubs are required for this research-only phase.
Execution must keep the per-task shell checks current so EVID-01 is enforced during both Wave 1 and Wave 2.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Source applicability is defensible rather than generic | EVID-01 | Requires editorial judgment about whether each retained source materially shapes later conclusions | Read each evidence-log row and confirm the `Why it applies` note mentions a concrete `mcp-telegram` concern instead of generic MCP advice |
| Brownfield baseline reflects the intended runtime contract | EVID-01 | Requires confirming that the written baseline is a faithful summary of code/tests/runtime, not just string-matching | Compare `10-BROWNFIELD-BASELINE.md` against `src/mcp_telegram/server.py`, `src/mcp_telegram/telegram.py`, `src/mcp_telegram/tools.py`, the reflected CLI tool list, and the brownfield tests in `tests/test_formatter.py`, `tests/test_resolver.py`, `tests/test_analytics.py`, and `tests/privacy_audit.sh` before marking the phase complete |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent shell validation
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
