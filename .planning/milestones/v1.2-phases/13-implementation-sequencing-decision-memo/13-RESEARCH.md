# Phase 13: Implementation Sequencing & Decision Memo - Research

**Researched:** 2026-03-13

## Summary

Phase 13 should not reopen the audit or the redesign choice. The planning job is to turn the locked
Phase 12 Medium Path into an implementation-ready memo that:

1. sequences the migration from the current seven-tool, continuation-heavy surface toward a
   capability-oriented Medium milestone,
2. preserves the brownfield invariants that Phase 10 and Phase 11 marked as non-negotiable,
3. defines validation checkpoints that prove both document quality and live runtime awareness, and
4. leaves the next build milestone plannable without repeating the evidence-gathering or option
   comparison work.

The strongest plan shape is a three-artifact progression:

- one artifact that freezes sequencing posture and stage boundaries,
- one artifact that turns the chosen path into a migration/validation matrix,
- one final memo that synthesizes sequencing, open questions, and decision criteria into the single
  Phase 13 deliverable.

This phase is documentation-heavy, but it is not abstract. The memo must stay anchored to live
runtime reflection, current code boundaries, and the test-backed behaviors that future
implementation work must preserve.

## Research Question

What does the planner need in order to create executable Phase 13 plans that turn the locked Medium
Path recommendation into a decision-ready implementation brief for the next milestone without
re-running Phases 10-12?

## Evidence To Reuse

Phase 13 should treat these artifacts as direct planning inputs rather than background reading:

- `10-EVIDENCE-LOG.md`: source hierarchy and the retained evidence set for later decisions.
- `10-AUDIT-FRAME.md`: judgment posture, preserved-invariant discipline, and explicit instruction
  that Phase 13 should reuse the audit frame instead of re-deriving methodology.
- `10-BROWNFIELD-BASELINE.md`: frozen seven-tool runtime snapshot, reflection-boundary behavior,
  stateful-runtime facts, and preserved invariants.
- `11-COMPARATIVE-AUDIT.md`: the stable conclusion that the current surface is workflow-capable
  but continuation-heavy.
- `12-OPTION-PROFILES.md`: concrete Minimal/Medium/Maximal deltas that clarify what Medium must
  change and what Maximal is still expected to absorb later.
- `12-REDESIGN-OPTIONS.md`: the locked Medium Path recommendation, rejected-alternative reasoning,
  guardrails, and the bounded Phase 13 handoff.
- live reflection from `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13:
  `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`,
  and `SearchMessages`.
- current brownfield anchors in `src/mcp_telegram/tools.py`, `src/mcp_telegram/server.py`,
  `src/mcp_telegram/pagination.py`, `src/mcp_telegram/telegram.py`, `src/mcp_telegram/cache.py`,
  `src/mcp_telegram/analytics.py`, and `tests/test_tools.py`.

## Locked Decisions From Earlier Phases

The planner should treat the following as fixed inputs, not debate topics:

- The Phase 12 **Medium Path** is the chosen path for the next milestone.
- Medium is a **migration stage toward a later Maximal redesign**, not the final steady-state
  public contract.
- **Backward compatibility is not a default planning constraint** for the follow-up implementation
  milestone; compatibility shims should be treated as explicit decisions, not safe assumptions.
- The phase outcome is a **decision-ready memo**, not executable product code and not another
  option-comparison artifact.
- The next milestone should be able to plan directly from the Phase 13 deliverable without
  re-running the source audit or redesign comparison.

## Brownfield Constraints That Shape Sequencing

### 1. Reflection-based discovery is a deployment boundary

`server.py` snapshots tool exposure at process start. Any sequencing guidance that changes tool
names, tool counts, or schemas must call out restart-sensitive validation explicitly. A future
implementation phase cannot be judged only by tests; it must also confirm the restarted runtime
reflects the intended tool surface.

### 2. The current surface is seven tools and continuation-heavy

The real shipped baseline is the seven-tool surface confirmed by live reflection and `tools.py`.
Phase 13 sequencing should therefore describe migration away from:

- discovery-first choreography via `ListDialogs`,
- topic-helper choreography via `ListTopics`,
- split navigation concepts (`next_cursor`, `next_offset`, `from_beginning=True`),
- text-first continuation cues embedded in prose,
- generic server-boundary failure collapse when exceptions escape handlers.

### 3. Some current "helper" behaviors are actually preserved strengths

The memo must keep explicit ambiguity handling, topic-state fidelity, cache-backed recovery, and
privacy-safe telemetry in the default-preserve set. Those are not accidental details that Medium is
free to simplify away.

### 4. The system is read-only but not stateless

The next milestone's sequencing must respect the persisted Telegram session, entity/topic caches,
analytics database, and process-cached client. That means Phase 13 should distinguish between:

- public-contract cleanup,
- internal capability-layer enabling work,
- runtime/deploy validation,
- follow-on work that belongs to the later Maximal step.

### 5. Tests already define important parts of the contract

`tests/test_tools.py`, `tests/test_analytics.py`, and `tests/privacy_audit.sh` are not optional
implementation details. Sequencing and open questions must recognize that future implementation
work will need to preserve or deliberately replace those contract expectations.

## What Medium Needs To Accomplish Before Maximal

Phase 12 left a stable direction: Medium should remove a large share of model burden without
taking on Maximal's full migration risk. For Phase 13 planning, that implies the next milestone
should focus on these outcomes:

- reframe the public contract around capability-oriented workflows instead of helper-first tool
  boundaries,
- unify read/search continuation semantics enough that navigation feels coherent,
- demote or absorb common helper hops where they exist only because of the current surface split,
- strengthen public result framing so continuation and recovery state are easier to consume,
- keep the internal architecture moving toward a later Maximal path instead of ossifying Medium as
  the permanent final shape.

The memo should therefore separate:

- **must land in Medium**: user-facing sequencing and validation decisions needed to make the next
  milestone coherent,
- **should prepare for Maximal**: internal abstractions or public-contract choices that reduce
  rework later,
- **can wait for Maximal**: broader tool merging or more aggressive surface compression that would
  overshoot the risk budget now.

## Recommended Plan Split

Phase 13 is best planned as three executable documentation plans in a single dependency chain.

### Plan 01: Sequencing Frame and Migration Boundaries

Purpose:
- convert the locked Medium recommendation into an explicit sequence of implementation stages,
- define what belongs in the next milestone versus what is deferred to Maximal,
- freeze the brownfield constraints and preserved invariants that each later stage must respect.

Primary artifact:
- a sequencing frame or staging document for the future implementation milestone.

Why first:
- the phase needs a stable migration vocabulary before it can define validation checkpoints or
  write the final decision memo.

### Plan 02: Validation and Checkpoint Matrix

Purpose:
- define stage-by-stage validation checkpoints for schema changes, continuation unification,
  helper-tool demotion, runtime reflection checks, and privacy/recovery invariants,
- make explicit which checks are document-only in Phase 13 versus which checks a future
  implementation milestone must run against code and a restarted runtime.

Primary artifact:
- a migration checkpoint / validation matrix.

Why second:
- Phase 13's value is not just sequence; it is sequence plus proof obligations.

### Plan 03: Final Decision Memo

Purpose:
- synthesize the staging frame and validation matrix into the single decision-ready Phase 13
  deliverable,
- name open questions, decision criteria, risk posture, and what the next milestone can plan from
  directly.

Primary artifact:
- the final implementation-sequencing decision memo for the phase.

Why last:
- the final memo should consume the earlier artifacts rather than invent them inline, which keeps
  the end deliverable compact, evidence-backed, and easier for a future milestone to trust.

## Risks To Plan Around

### Risk 1: The phase drifts back into redesign comparison

The Medium choice is already locked. Plans should not spend effort re-arguing Minimal versus
Medium versus Maximal.

### Risk 2: The memo becomes generic architecture advice

`EVID-02` requires an actionable deliverable. Each plan should therefore tie its output to concrete
surface transitions such as helper-tool demotion, navigation unification, result framing, reflection
checks, and explicit open questions.

### Risk 3: The phase accidentally treats Medium as the final end state

The planning posture says Medium is a migration stage toward a later Maximal redesign. Plans should
call out where decisions are intentionally chosen to make that later step cheaper.

### Risk 4: Validation is described only at the document level

Because the real product boundary is reflection-based and runtime-sensitive, the decision memo must
teach future execution to verify a restarted runtime surface, not only repository files.

### Risk 5: Backward compatibility sneaks back in as an unstated assumption

Plans should explicitly gate any compatibility shims or dual-surface rollout ideas behind named
questions or justification rather than assuming them as the default safe route.

## Open Questions Phase 13 Should Surface

These questions are worth naming in the final memo, but they should be framed as decision points
for the next implementation milestone rather than blockers for Phase 13 planning:

1. Which current tools remain primary in Medium, which become secondary helpers, and which are only
   compatibility candidates?
2. How far should Medium go on public result structuring before it starts overlapping the later
   Maximal redesign?
3. Should the first Medium milestone introduce a compatibility window at all, or is a clean cut
   preferable given the current no-backward-compatibility posture?
4. What is the narrowest shared continuation model that unifies reading and search without
   flattening important behavioral differences?
5. Which runtime reflection checks should be mandatory after any future contract-affecting change:
   `cli.py list-tools`, container restart verification, schema spot-checks, or all of them?

## Validation Architecture

Phase 13 is a documentation phase, but its validation should still enforce future execution
discipline.

### Test infrastructure

- Primary validation mode: shell-based artifact verification using `test`, `rg`, and local CLI
  reflection.
- Runtime anchor: `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`.
- Supporting evidence anchor: the Phase 10-12 artifacts plus brownfield code/test anchors in
  `src/mcp_telegram/*` and `tests/*`.

### Required verification themes

The future Phase 13 plans should map every task to one or more of these verification themes:

1. **artifact completeness**: required sequencing, checkpoint, and memo artifacts exist and contain
   the locked Medium-path posture;
2. **requirements coverage**: `RECO-02` and `EVID-02` are both covered directly in plan
   frontmatter and in artifact contents;
3. **brownfield grounding**: the artifacts cite the reflected seven-tool runtime and the preserved
   invariants from Phase 10 and Phase 11;
4. **migration clarity**: stage boundaries distinguish must-land Medium work, Maximal preparation,
   and deferred work;
5. **runtime verification discipline**: the artifacts require future implementation phases to
   verify restarted runtime exposure, not only repository diffs;
6. **open-question quality**: the final memo names concrete pre-coding decision points rather than
   generic future concerns.

### Expected validation commands

The eventual `13-VALIDATION.md` should center on fast shell commands such as:

- `test -f` checks for Phase 13 artifacts,
- `rg -n` checks for sequencing, validation, open-question, Medium-path, Maximal-prep, and runtime
  verification language,
- `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools | rg "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages"` to keep the frozen baseline explicit.

### Manual review requirements

Automated checks alone are not enough. Manual review should confirm:

- the sequencing is genuinely actionable for a future implementation milestone,
- the memo does not reopen the redesign choice,
- the phase does not smuggle in compatibility assumptions contrary to the current posture,
- the artifact remains bounded to Medium sequencing while still making later Maximal work cheaper.

## Planning Verdict

Phase 13 is ready for planning now.

The repo already has enough evidence to plan this phase without new external research because:

- the source hierarchy and brownfield baseline are already frozen,
- the current surface and preserved invariants are grounded in live reflection, code, and tests,
- the Medium recommendation and rejected-alternative logic are already explicit,
- the remaining work is sequencing, checkpointing, and decision-memo synthesis rather than another
  discovery pass.

The planner should produce a small number of documentation plans with explicit dependencies, each
directly advancing the final decision memo. If the plans stay anchored to Medium-path sequencing,
runtime verification, and open-question framing, Phase 13 will satisfy both `RECO-02` and
`EVID-02`.

## Sources

- `.planning/REQUIREMENTS.md`
- `.planning/STATE.md`
- `.planning/ROADMAP.md`
- `.planning/phases/10-evidence-base-audit-frame/10-EVIDENCE-LOG.md`
- `.planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md`
- `.planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md`
- `.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md`
- `.planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md`
- `.planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md`
- `src/mcp_telegram/server.py`
- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/pagination.py`
- `src/mcp_telegram/telegram.py`
- `src/mcp_telegram/cache.py`
- `src/mcp_telegram/analytics.py`
- `tests/test_tools.py`
- `tests/test_analytics.py`
- `tests/privacy_audit.sh`
- `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` (observed 2026-03-13)
