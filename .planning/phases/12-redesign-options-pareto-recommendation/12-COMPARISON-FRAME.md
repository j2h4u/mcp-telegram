# Phase 12 Comparison Frame

Last updated: 2026-03-13

This artifact freezes how Phase 12 will compare redesign options for the `mcp-telegram` public MCP
surface. It is a bounded comparison-and-recommendation phase, not an implementation plan. The goal
here is to lock the baseline, guardrails, and evaluation dimensions before later Phase 12 plans
populate option profiles or choose a Pareto recommendation.

## Scope and Decision Posture

- Phase 12 is a bounded comparison-and-recommendation phase, not an implementation plan or a fresh
  audit pass.
- The frozen baseline is the reflected seven-tool current surface plus the Phase 11 synthesis in
  [11-COMPARATIVE-AUDIT.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md),
  not stale inherited notes or new speculative discovery.
- The comparison posture stays evidence-backed: Phase 10 remains the source hierarchy and audit
  method, while Phase 11 remains the current-state judgment that later option work must compare
  against rather than reopen.
- Phase 12 compares options by burden reduction and contract clarity, not by feature novelty or
  implementation ambition for its own sake.
- No section of this frame should pre-select the minimal, medium, or maximal path. It exists to
  make those later comparisons legible and consistent.

## Frozen Baseline

The frozen baseline for Phase 12 is the shipped public surface reflected on 2026-03-13:
`GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`, and
`SearchMessages`.

Phase 11's stable synthesis is the comparison starting point:

- the surface is workflow-capable but continuation-heavy
- discovery and topic handling often require helper-step choreography before the user-visible job
- continuation contracts are mixed across `next_cursor`, `next_offset`, and `from_beginning`
- result bodies are text-first and recovery is usually action-oriented
- unexpected escaped failures can still collapse to generic server-boundary wrapping

These baseline facts come from the frozen Phase 10 and Phase 11 artifacts, especially
[10-BROWNFIELD-BASELINE.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md),
[10-AUDIT-FRAME.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md),
[11-COMPARATIVE-AUDIT.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md),
and [12-RESEARCH.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/12-redesign-options-pareto-recommendation/12-RESEARCH.md).

## Preserved Guardrails

Every redesign option starts from the default-preserve guardrails below unless that option
explicitly argues that a guardrail should change and explains why the evidence justifies the move.

- `read-only` Telegram scope remains the default boundary for the public contract.
- `privacy-safe telemetry` remains mandatory; no option should widen telemetry into message-content
  logging or user-identifying event payloads.
- `stateful runtime reality` is first-class input, not a flaw to paper over. The comparison must
  account for cached client/session state plus SQLite-backed local caches and analytics.
- `recovery-critical caches` and topic metadata remain preserved strengths because they carry
  deleted-topic and inaccessible-topic context across calls.
- `explicit ambiguity handling` remains a default guardrail; later options may reduce retry burden
  but should not regress into silent auto-picks.

## Comparison Dimensions

Every Phase 12 option profile must be compared across the same shared dimensions so the later
recommendation is decision-ready instead of impressionistic.

| Dimension | What the option comparison must ask |
| --- | --- |
| user-task fit | Does the option make discovery, reading, search, and topic handling feel closer to the actual user job instead of helper-step setup work? |
| continuation-contract simplicity | Does the option simplify or normalize continuation mechanics across reading, search, and replay-style flows? |
| contract delta size | How much of the current public contract changes, and how much model/client adaptation does that imply? |
| migration risk | How likely is the option to create breakage for reflected schemas, long-lived runtimes, or established agent workflows? |
| implementation scope | Roughly how much follow-on build work would Phase 13 need to sequence if this option were chosen? |
| preserved-strength retention | Does the option keep the current strengths that already matter, especially topic fidelity, action-oriented recovery, and privacy-safe telemetry? |
| recovery quality | After the redesign, how well would ambiguity, missing entities, invalid continuation tokens, inaccessible topics, and escaped failures recover? |
| output-shape burden | Does the option reduce text-first parsing burden, preserve it deliberately, or deepen it with new complexity? |
| state-model impact | How does the option interact with the system's stateful runtime, cache-backed resolution, and local persistence assumptions? |
| operational/runtime risk | Does the option reduce or worsen deployment freshness, reflected-schema drift, and other runtime mismatch risks? |

## Usage Rule For Later Phase 12 Plans

- Plan 02 should populate options against this frame instead of inventing new criteria midstream.
- Plan 03 should justify any recommendation by referring back to these dimensions and guardrails,
  not by introducing a new evaluation vocabulary at the end.
