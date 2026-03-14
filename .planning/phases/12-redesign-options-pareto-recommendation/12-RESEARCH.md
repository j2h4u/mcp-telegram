# Phase 12: Redesign Options & Pareto Recommendation - Research

**Researched:** 2026-03-13
**Domain:** planning the redesign-comparison and recommendation phase for the `mcp-telegram` MCP surface
**Confidence:** HIGH

## Summary

Phase 12 is not a fresh discovery phase and it is not the implementation milestone in disguise.
The planner should treat it as a bounded comparison-and-decision phase that consumes the fixed
Phase 10 evidence hierarchy and the fixed Phase 11 current-state audit, then produces:

1. one option matrix covering `minimal`, `medium`, and `maximal` redesign paths
2. one explicit contract-delta view showing what each path would keep, reshape, merge, demote, or
   remove
3. one named Pareto recommendation with explicit rationale, preserved invariants, and bounded
   migration risk

The core planning question is:

What structure does the maintainer need so the redesign discussion stays evidence-backed, comparable
across options, and directly usable by Phase 13 without drifting into vague product strategy?

The answer is:

- Freeze Phase 11's current-state synthesis as the baseline to compare against.
- Compare options by burden reduction, not by feature novelty.
- Make contract deltas first-class, not side notes.
- Force the recommendation to name what it refuses to break: read-only scope, privacy-safe
  telemetry, and recovery-critical state.
- Validate completeness at the option, contract, and recommendation levels so the selected path is
  defensible before Phase 13 turns it into sequencing guidance.

## What The Planner Must Inherit As Fixed Inputs

Phase 12 should not reopen these questions:

### Source and evidence posture

- Use the Phase 10 retained evidence set as the only normative basis for external claims:
  MCP Tools specification and Anthropic tool-use guidance.
- Use live runtime reflection, source, and tests as the authority for current-surface claims.
- Do not widen the evidence base with blogs or community commentary unless a concrete gap appears.

### Frozen current-state baseline

Phase 11 already established the shipped comparison baseline:

- reflected seven-tool public surface:
  `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`,
  `SearchMessages`
- reflection-based discovery with process-start snapshotting in `src/mcp_telegram/server.py`
- text-first results and action-oriented recovery across the public contract
- mixed continuation conventions:
  `ListMessages` uses `next_cursor`, `SearchMessages` uses `next_offset`, and `ListMessages` also
  exposes `from_beginning=True`
- explicit workflow burden in discovery, forum-topic handling, pagination, and disambiguation
- generic server-boundary collapse to `Tool <name> failed` for escaped exceptions

### Default-preserve invariants

Unless an option explicitly argues otherwise, the planner should treat these as non-negotiable:

- read-only Telegram scope
- privacy-safe aggregate telemetry with no message-content logging
- stateful runtime reality, including cached client/session and SQLite-backed local state
- recovery-critical caches and topic metadata, including deleted/inaccessible topic history
- explicit ambiguity handling instead of silent auto-picks

## What This Phase Is And Is Not

### In scope

- compare redesign paths for the public MCP surface
- estimate expected impact, migration risk, and implementation scope
- map contract changes tool-by-tool and workflow-by-workflow
- select one Pareto recommendation with explicit rationale

### Out of scope

- coding the redesign
- building a prototype unless a tiny example is needed to clarify an option shape
- re-auditing the current surface from scratch
- deciding detailed implementation sequencing for the follow-on milestone
  That belongs in Phase 13.

## Option Shapes The Planner Should Compare

The planner should require all three option tiers to be concrete enough that a maintainer could
imagine the public contract after the change.

### Minimal path

Expected shape:

- preserve the current seven-tool topology
- keep read/search/topic capabilities separate
- reduce burden mostly through metadata cleanup, continuation normalization, error-surface cleanup,
  and small contract edits

Why it must be included:

- it is the lowest-risk baseline for `OPTION-01`
- it tests whether outsized usage gains are available without structural consolidation

### Medium path

Expected shape:

- preserve the read-only/stateful baseline
- materially reshape the model-facing surface around capability-oriented workflows
- likely consolidate helper-step burden where current choreography is strongest

Why it must be included:

- it is the likely Pareto candidate range
- it tests whether a smaller number of safe surface changes can remove most orchestration burden

### Maximal path

Expected shape:

- revisit the public contract more aggressively
- allow larger tool-merging, role changes, or result-shape changes
- still preserve the non-negotiable invariants unless explicitly challenged

Why it must be included:

- it shows the upper bound of redesign ambition
- it prevents the recommendation from being chosen against a false two-option frame

## Required Comparison Dimensions

Phase 12 should compare all options across the same dimensions. If the planner omits any of these,
the recommendation will be hard to defend.

| Dimension | Why it matters in this project |
| --- | --- |
| user-task fit | Phase 11 showed the current surface is workflow-capable but continuation-heavy; options must be judged by whether they reduce helper work around discovery, reading, search, and topic handling. |
| continuation-contract simplicity | The current surface mixes `next_cursor`, `next_offset`, and `from_beginning=True`; options must state whether they normalize, preserve, or hide that complexity. |
| contract delta size | `OPTION-02` requires explicit keep/reshape/merge/demote/remove mapping for tools, parameters, and interaction patterns. |
| migration risk | The project serves long-lived runtimes and reflected schemas; planners must distinguish safe contract evolution from high-breakage redesign. |
| implementation scope | Phase 13 needs downstream sequencing inputs, so Phase 12 must compare rough implementation breadth, not just UX upside. |
| preserved-strength retention | Topic fidelity, action-oriented recovery, and privacy-safe telemetry are real strengths that options must not casually regress. |
| recovery quality | Each option should say how ambiguity, invalid cursors, inaccessible topics, and escaped exceptions would behave after the redesign. |
| output-shape burden | The current text-first contract is readable but parse-heavy; options should state whether they preserve text-first output, add lightweight structure, or change the contract more deeply. |
| state-model impact | Because the server is read-only but not stateless, options must say what runtime state they depend on, preserve, or expose more clearly. |
| operational/runtime risk | Process-start snapshotting and stale-runtime deployment are real concerns; options should note whether they increase or reduce operational mismatch risk. |

## Contract Delta Inventory The Planner Must Require

The option matrix alone is not enough. The planner should require a contract-delta table that makes
the redesign legible at the surface level.

Minimum rows:

- all seven current public tools
- current shared interaction patterns:
  discovery-first flow, disambiguation retry flow, topic-selection flow, pagination flow,
  text-first result parsing, generic server-boundary failure behavior
- high-signal parameters and tokens:
  `dialog`, `topic`, `sender`, `cursor`, `offset`, `from_beginning`, `exclude_archived`,
  `ignore_pinned`, `unread`

Required columns:

| Column | Purpose |
| --- | --- |
| current surface element | Proves coverage against the shipped contract |
| current role | Captures what job the tool, parameter, or pattern serves today |
| minimal path action | `keep`, `reshape`, `merge`, `demote`, `remove`, or `rename` |
| medium path action | Same vocabulary so the rows compare cleanly |
| maximal path action | Same vocabulary so escalation is visible |
| rationale | Explains why the action changes model burden or preserves safety |
| affected invariants | Prevents accidental recommendations that break read-only/privacy/state constraints |

Planning rule:

- every tool must appear at least once
- every current continuation pattern must appear at least once
- every option row must use explicit action verbs, not vague prose

## Recommended Deliverable Shape

Phase 12 should produce one primary artifact and one verification artifact.

### Primary artifact

Recommended file: `12-REDESIGN-OPTIONS.md`

Recommended sections:

1. `Scope and Decision Posture`
2. `Frozen Baseline From Phase 11`
3. `Comparison Dimensions`
4. `Option Matrix`
5. `Public Contract Delta Inventory`
6. `Pareto Recommendation`
7. `Recommendation Guardrails and Invariants`
8. `Phase 13 Handoff Notes`

Why one primary artifact:

- Phase 13 needs a stable decision input, not scattered comparison notes
- the recommendation must be read against the rejected alternatives in one place

### Verification artifact

Required file: `12-VERIFICATION.md`

Reason:

- this phase has high judgment content but still has crisp completeness checks
- requirement coverage is inspectable:
  `OPTION-01`, `OPTION-02`, and `RECO-01`

## Likely Plan Decomposition

A planner should probably split Phase 12 into three executable plans.

### Plan 01: Comparison framework and contract inventory

Goal:

- freeze the dimensions, option vocabulary, and contract-delta inventory structure before choosing a
  recommendation

Primary outputs:

- comparison dimensions section
- option-template skeleton
- contract-delta matrix skeleton

Why first:

- it prevents premature recommendation bias
- it gives later plans a stable format for evidence-backed comparison

### Plan 02: Populate minimal, medium, and maximal options

Goal:

- fill the option matrix and contract-delta inventory using the Phase 11 redesign pressures and
  preserved invariants

Primary outputs:

- completed option profiles
- completed keep/reshape/merge/demote/remove mapping
- scoped impact/risk/scope comparison

Why second:

- recommendation quality depends on fully populated alternatives, not sketches

### Plan 03: Pareto recommendation and validation

Goal:

- select one recommendation explicitly and prove why it is Pareto-superior for the next milestone

Primary outputs:

- named recommendation
- rationale for outsized impact versus safe change set
- explicit invariant guardrails
- requirement verification

Why last:

- it forces the recommendation to emerge from the comparison rather than leading it

## Recommendation Logic The Planner Should Enforce

The planner should require the final recommendation to answer these questions directly:

1. Which redesign pressure from Phase 11 does this option relieve most effectively?
2. Why is that impact likely larger than the size of the change set?
3. What safer strengths from the current surface does it preserve?
4. Which more aggressive changes were intentionally rejected, and why are they not needed yet?
5. What invariants would become risky to disturb if the option were expanded further?

This should be framed as a Pareto argument, not as "best overall" rhetoric.
The recommendation wins if it removes a large share of model burden while preserving the strongest
brownfield constraints and avoiding needless migration scope.

## Validation Architecture

The planner should require Phase 12 validation at four levels.

### 1. Coverage validation

Check that the deliverable includes:

- exactly three option tiers:
  `minimal`, `medium`, `maximal`
- all seven current tools
- all required contract-change verbs across the delta inventory where applicable:
  keep, reshape, merge, demote, remove
- all three requirement IDs:
  `OPTION-01`, `OPTION-02`, `RECO-01`

### 2. Evidence validation

Check that:

- major claims about the current surface point back to Phase 10/11 artifacts or direct code/runtime
  anchors
- major claims about preserved invariants cite the brownfield baseline or tests
- no option claim depends on unverified assumptions about the current contract

### 3. Comparison-quality validation

Check that:

- all options are compared on the same dimensions
- risk and scope are expressed comparatively, not as unsupported numbers
- medium and maximal paths are not just reworded copies of minimal
- the recommendation is explicitly compared against at least one rejected alternative

### 4. Safety/invariant validation

Check that the recommendation explicitly preserves or consciously challenges:

- read-only scope
- privacy-safe telemetry
- recovery-critical state and cached metadata
- explicit ambiguity recovery
- operational reality of a stateful runtime with reflected tool discovery

## High-Yield Brownfield Anchors For Planning

These are the highest-value code/runtime anchors a planner should reuse while drafting executable
plans:

- `src/mcp_telegram/server.py`
  reflection-based tool exposure, empty prompts/resources/templates, process-start snapshotting,
  generic escaped-error wrapping
- `src/mcp_telegram/tools.py`
  tool descriptions, schema exposure, pagination tokens, `from_beginning=True`, topic flow,
  recovery messaging, current tool boundaries
- `src/mcp_telegram/pagination.py`
  cursor opacity and cross-dialog guardrails
- `src/mcp_telegram/resolver.py`
  explicit disambiguation semantics
- `src/mcp_telegram/cache.py`
  recovery-critical state around entities, reactions, and topics
- `src/mcp_telegram/analytics.py`
  privacy-safe telemetry fields and batching model
- `tests/test_tools.py`
  contract-locked behavior for pagination, topic recovery, search windows, and continuation tokens
- `tests/test_analytics.py` and `tests/privacy_audit.sh`
  privacy invariants
- live `uv run cli.py list-tools`
  current reflected tool names, descriptions, and input schemas

## Risks The Planner Should Anticipate

- Premature convergence on a recommendation before the contract-delta matrix is complete.
- Treating "medium" and "maximal" as effort labels instead of distinct surface shapes.
- Letting the recommendation erase topic-state fidelity or ambiguity recovery in the name of
  simplification.
- Treating text-first output as wholly bad, instead of distinguishing readable transcript value from
  parse-heavy continuation burden.
- Ignoring deployment/runtime freshness risk when proposing contract changes for long-lived
  containers.

## Planning Bottom Line

Phase 12 will be plan-ready when the planner treats it as a comparative design phase with strict
inputs and strict outputs:

- inputs:
  Phase 10 evidence posture and Phase 11 current-state audit
- outputs:
  one option matrix, one contract-delta inventory, one Pareto recommendation, one verification pass

If the future `PLAN.md` files preserve that structure, the phase should satisfy `OPTION-01`,
`OPTION-02`, and `RECO-01` without drifting into either vague strategy language or premature
implementation design.

## RESEARCH COMPLETE
