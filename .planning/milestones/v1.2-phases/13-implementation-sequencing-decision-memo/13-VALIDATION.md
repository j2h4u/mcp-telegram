---
phase: 13
slug: implementation-sequencing-decision-memo
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-13
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | shell-based artifact verification using `rg`, `test`, and local CLI reflection |
| **Config file** | none — validation is document, code-anchor, and runtime-contract oriented |
| **Quick run command** | `test -f .planning/phases/13-implementation-sequencing-decision-memo/13-RESEARCH.md && rg -n "Phase 12 Medium Path|migration stage toward a later Maximal|Backward compatibility is not a default planning constraint|Recommended Plan Split|Validation Architecture|Phase 13 is ready for planning now" .planning/phases/13-implementation-sequencing-decision-memo/13-RESEARCH.md && ( test ! -f .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md || rg -n "Medium is already chosen|migration stage toward a later Maximal|backward compatibility is not a default|read-only|privacy-safe telemetry|explicit ambiguity handling|stateful runtime|recovery-critical|GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md ) && ( test ! -f .planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md || rg -n "must land for Medium|prepare now to make Maximal cheaper|defer to later Maximal|error-surface cleanup|capability-layer|continuation|helper|rollout|list-tools|restart|runtime verification" .planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md ) && ( test ! -f .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md || rg -n "Recommended Implementation Path|Sequencing|Validation Checkpoints|Open Questions Before Coding|Risks and Failure Modes|Deferred Work and Future Maximal Preparation|must land|prepare now|defer|list-tools|restart|Medium|Maximal" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md )` |
| **Full suite command** | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages" && test -f .planning/phases/13-implementation-sequencing-decision-memo/13-RESEARCH.md && test -f .planning/phases/13-implementation-sequencing-decision-memo/13-VALIDATION.md && rg -n "Recommended Plan Split|Validation Architecture|runtime verification discipline|open questions" .planning/phases/13-implementation-sequencing-decision-memo/13-RESEARCH.md && ( test ! -f .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md || rg -n "Medium is already chosen|stateful runtime|privacy-safe telemetry|GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|primary|secondary|merge|future-removal" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md ) && ( test ! -f .planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md || rg -n "must land for Medium|prepare now to make Maximal cheaper|defer to later Maximal|list-tools|restart|rebuild|snapshotted|ListMessages|SearchMessages|ListTopics|server.py|tests/test_tools.py|tests/test_analytics.py|tests/privacy_audit.sh|runtime verification" .planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md ) && ( test ! -f .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md || rg -n "Recommended Implementation Path|Sequencing|Validation Checkpoints|Open Questions Before Coding|Risks and Failure Modes|Deferred Work and Future Maximal Preparation|future implementation milestone|without rerunning the source audit|without rerunning the audit or comparison work|Medium|Maximal" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md )` |
| **Final verification command** | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages" && test -f .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md && rg -n "Recommended Implementation Path|Sequencing|Validation Checkpoints|Open Questions Before Coding|future implementation milestone|without rerunning the source audit|without rerunning the audit or comparison work|Medium|Maximal|list-tools|restart" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full suite command
- **Before `$gsd-verify-work`:** Run the final verification command; the reflected tool list and final decision memo must both pass
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 13-01-01 | 01 | 1 | RECO-02 | doc | `rg -n "Medium is already chosen|migration stage toward a later Maximal|backward compatibility is not a default|future milestone" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md` | ❌ W1 | ⬜ pending |
| 13-01-02 | 01 | 1 | EVID-02 | doc | `rg -n "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|read-only|privacy-safe telemetry|explicit ambiguity handling|stateful runtime|recovery-critical" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md` | ❌ W1 | ⬜ pending |
| 13-02-01 | 02 | 2 | RECO-02 | doc | `rg -n "must land for Medium|prepare now to make Maximal cheaper|defer to later Maximal|error-surface cleanup|capability-layer|continuation|helper|rollout" .planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md` | ❌ W2 | ⬜ pending |
| 13-02-02 | 02 | 2 | EVID-02 | runtime/doc | `rg -n "list-tools|restart|rebuild|snapshotted|ListMessages|SearchMessages|ListTopics|server.py|tests/test_tools.py|tests/test_analytics.py|tests/privacy_audit.sh|runtime verification" .planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md && UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages"` | ❌ W2 | ⬜ pending |
| 13-03-01 | 03 | 3 | RECO-02 | doc | `rg -n "Recommended Implementation Path|Sequencing|Validation Checkpoints|Open Questions Before Coding|Risks and Failure Modes" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md` | ❌ W3 | ⬜ pending |
| 13-03-02 | 03 | 3 | EVID-02 | doc | `rg -n "future implementation milestone|without rerunning the source audit|without rerunning the audit or comparison work|must land|prepare now|defer|Medium|Maximal|list-tools|restart" .planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md` | ❌ W3 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 stubs are required for this documentation phase.
Execution should keep shell-based checks current as the sequencing artifacts and final memo land.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| The sequence is actionable rather than abstract strategy prose | RECO-02 | Requires judgment about whether the artifacts teach an actual implementation path and not just architectural preference | Read the sequencing brief and implementation memo and confirm they describe a clear order of work, stage boundaries, and why that order reduces migration risk |
| Medium is treated as a migration step toward Maximal, not the permanent end state | RECO-02 | Requires editorial review of sequencing posture and deferred-work boundaries | Review the sequencing brief and implementation memo and confirm they distinguish what must land now, what prepares for Maximal later, and what is intentionally deferred |
| The deliverable is directly usable by a future implementation milestone | EVID-02 | Requires human judgment about whether the memo provides enough concrete guidance to plan work without redoing earlier phases | Read the final memo and confirm it consolidates sequencing, validation checkpoints, and open questions into one decision-ready artifact |
| Compatibility assumptions are explicit rather than smuggled back in | EVID-02 | Requires checking the posture of open questions and migration guidance | Review the final memo and confirm compatibility shims or dual-contract rollout are framed as explicit decisions or questions, not as default assumptions |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent shell validation
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
