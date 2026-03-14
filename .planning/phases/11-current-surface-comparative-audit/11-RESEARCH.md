# Phase 11: Current Surface Comparative Audit - Research

**Researched:** 2026-03-13
**Domain:** planning the current-surface audit for the `mcp-telegram` MCP contract
**Confidence:** HIGH

## Summary

Phase 11 should not spend time re-selecting sources or re-inventing the rubric. Phase 10 already
fixed the evidence hierarchy, brownfield baseline, and judgment model. The planning question for
Phase 11 is narrower:

What artifacts, evidence samples, and comparison structures are required so the maintainer can
review a grounded, decision-friendly audit of the *current* public surface?

The answer is:

1. Audit the shipped surface at two levels: each reflected public tool and each main user workflow.
2. Require every major finding to pair named external guidance with a specific brownfield anchor in
   `tools.py`, `server.py`, or the relevant tests/runtime output.
3. Make low-level contract leakage an explicit audit object rather than an incidental observation.
4. Preserve the Phase 10 baseline and validation posture so Phase 12 can compare redesign options
   without redoing current-state discovery.

## What The Planner Must Lock In

### Fixed units of analysis

Phase 11 must cover both units mandated by Phase 10:

- The seven current public tools exposed by reflection:
  `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`,
  `SearchMessages`
- The five main workflows named in the roadmap:
  discovery, reading, search, topic handling, recovery/error flows

Planning implication:

- Do not allow a plan that audits only tools or only workflows.
- Do not rely on older six-tool notes. The reflected runtime inventory on 2026-03-13 includes
  `ListTopics`, and the audit must treat that as authoritative.
- Treat the `AGENTS.md` six-tool list as stale project context, not as source of truth.

### Fixed audit posture

Phase 11 inherits these rules from Phase 10 and should not renegotiate them:

- Use the retained source hierarchy from `10-EVIDENCE-LOG.md`.
- Use the non-numeric `strong` / `mixed` / `weak` judgment bands from `10-AUDIT-FRAME.md`.
- Treat source, tests, and reflected runtime as more trustworthy than prior planning notes.
- Treat workflow burden, pagination, ambiguity recovery, and output shape as part of the public
  contract.
- Treat the current read-only, privacy-safe, stateful baseline as preserved by default.

## Required Artifacts

Phase 11 needs one primary audit artifact and one validation artifact.

### 1. Primary audit deliverable

Recommended file: `11-COMPARATIVE-AUDIT.md`

The planner should require one decision-friendly document with these sections:

1. `Scope and Method`
2. `Tool-by-Tool Audit`
3. `Workflow Audit`
4. `Low-Level Contract Leakage`
5. `Preserved Invariants and Redesign Pressure`
6. `Current-State Synthesis`

This should be one coherent document, not scattered notes, because Phase 12 needs a stable current
state baseline to compare redesign options against.

### 2. Validation artifact

Required file: `11-VALIDATION.md`

Phase 11 is research-heavy, but it still has hard completeness requirements:

- all seven tools covered
- all five workflows covered
- each major finding tied to named evidence
- leakage inventory explicitly present
- preserved invariants explicitly carried forward

That is enough structure to justify a Nyquist validation artifact.

## What The Primary Audit Must Contain

### Tool matrix

The audit should contain a tool matrix with one row per public tool. Recommended columns:

| Column | Why it must exist |
|--------|-------------------|
| Tool | Proves full public-surface coverage |
| Primary user job | Anchors task-shape fit |
| Current contract shape | Captures inputs/outputs the model actually sees |
| Judgment band | Uses the Phase 10 rubric directly |
| Strengths | Preserves what should not be broken casually |
| Gaps / burdens | Makes current weaknesses explicit |
| Named external evidence | Satisfies AUDIT-01 |
| Brownfield anchor | Satisfies AUDIT-02 |
| Main leak, if any | Satisfies AUDIT-03 |

Minimum planning rule:

- Each row must cite at least one Phase 10 external source and at least one concrete code/test or
  runtime anchor.

### Workflow matrix

The audit should contain a workflow matrix with one row per workflow:

- discovery
- reading
- search
- topic handling
- recovery/error flows

Recommended columns:

| Column | Why it must exist |
|--------|-------------------|
| Workflow | Locks roadmap coverage |
| Typical tool choreography | Shows how the model actually completes the task |
| Where burden appears | Makes helper-step cost explicit |
| Evidence anchors | Grounds claims in source/tests/runtime |
| Judgment band | Reuses the audit frame |
| Why it matters for later redesign | Makes the phase actionable for Phase 12 |

Important planning note:

- For this project, workflow burden is not optional commentary. The current surface already teaches
  sequences such as `ListDialogs -> ListTopics -> ListMessages`, and the audit must judge whether
  that choreography is aligned with the user task or leaked implementation mechanics.

### Contract-leak inventory

Phase 11 needs an explicit comparison table or equivalent section for contract leakage. Recommended
rows:

- pagination conventions
- disambiguation / fuzzy resolution retries
- tool choreography / helper-step burden
- discovery freshness and reflection snapshot behavior
- text parsing burden from text-first outputs
- generic server-boundary failure wrapping

Recommended columns:

| Column | Purpose |
|--------|---------|
| Leak category | Makes cross-tool patterns visible |
| Where it appears | Points to specific tool/workflow locations |
| Model burden | Explains why the leak matters |
| Evidence | Ties claim to named source and brownfield anchor |
| Preserve / change pressure | Feeds Phase 12 option design |

### Synthesis matrix

The phase success criteria require a decision-friendly summary. The planner should require a final
matrix or equivalent summary with these columns:

| Column | Meaning |
|--------|---------|
| Area | Tool or workflow area being summarized |
| Current strength | What works well today |
| Current weakness | What creates model burden today |
| Preserved invariant | What later redesigns should keep unless evidence overturns it |
| Redesign pressure | Why Phase 12 will need to revisit this area |

## Minimum Evidence Sample

Phase 11 does not need broad new research. It needs disciplined re-use of Phase 10 plus direct
sampling of the current surface.

### Normative external evidence

Use the retained Phase 10 sources directly:

- MCP Tools specification
- Anthropic tool-use implementation guidance
- Anthropic tool-use overview

Planning rule:

- Do not introduce weaker community/blog sources unless the planner can name a concrete gap in the
  existing evidence base. Phase 10 intentionally excluded them.

### Brownfield runtime evidence

The audit should sample the current surface from runtime reflection, not just source reading.
Minimum runtime sample:

- `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`

Recommended additional cross-check if the plan wants deployment realism:

- inspect the long-lived runtime container’s reflected tool list/schema as a stale-runtime guard,
  because project notes explicitly warn that tests passing does not prove the served surface is
  current

Planning constraint:

- Avoid audit evidence that logs or copies real Telegram message content unless strictly necessary.
  The phase is about the public contract shape, not message-data collection.

### Brownfield code evidence

The planner should require direct sampling of these files:

- `src/mcp_telegram/server.py`
  - reflection-based tool enumeration
  - process-start snapshot mapping
  - empty prompts/resources/resource templates
  - generic `Tool <name> failed` server-boundary wrapping
- `src/mcp_telegram/tools.py`
  - `tool_description()` and `_sanitize_tool_schema()`
  - `ToolArgs` docstrings and schemas for all seven tools
  - action-oriented recovery helpers
  - `ListDialogs`, `ListTopics`, `ListMessages`, `SearchMessages`
  - `GetUserInfo`, `GetMyAccount`, `GetUsageStats`
- `src/mcp_telegram/resolver.py`
  - ambiguity and exact-choice retry behavior
- `src/mcp_telegram/pagination.py`
  - cursor encoding/decoding semantics and cross-dialog guardrails
- `src/mcp_telegram/formatter.py`
  - text-first rendering conventions
- `src/mcp_telegram/telegram.py`
  - process-cached client and XDG-backed session/state behavior
- `src/mcp_telegram/cache.py`
  - entity/reaction/topic caches and recovery-critical topic metadata
- `src/mcp_telegram/analytics.py`
  - privacy-safe telemetry invariant

### Brownfield test evidence

The planner should require sampling of tests that lock the contract:

- `tests/test_tools.py`
  - topic flows
  - deleted/inaccessible topic recovery
  - `from_beginning=True`
  - `next_cursor`
  - `next_offset`
  - `[HIT]` search grouping
- `tests/test_resolver.py`
  - ambiguity behavior
- `tests/test_pagination.py`
  - cursor round-trip and cross-dialog failure semantics
- `tests/test_formatter.py`
  - text rendering conventions
- `tests/test_analytics.py`
  - telemetry behavior and constraints
- `tests/privacy_audit.sh`
  - privacy guardrail

## Where The Planner Should Expect Findings To Cluster

Phase 11 should assume the highest-yield audit areas are these:

### 1. Discovery and metadata

Likely evidence anchors:

- reflection-based exposure in `server.py`
- docstring plus Pydantic schema metadata path in `tools.py`
- empty prompts/resources/templates in `server.py`

Why this matters:

- AUDIT-01 is not satisfied by handler correctness alone. The audit must judge what an LLM can
  infer *before* calling a tool.

### 2. Reading workflow burden

Likely evidence anchors:

- `ListMessages` docstring and input schema
- cursor handling and `from_beginning=True`
- topic and sender filtering branches
- formatter output conventions

Why this matters:

- Reading is the richest current workflow and likely carries the most continuation burden.

### 3. Topic handling

Likely evidence anchors:

- `ListTopics`
- topic status labels such as `general`, `active`, `previously_inaccessible`
- deleted-topic and inaccessible-topic recovery paths
- topic-related tests in `tests/test_tools.py`

Why this matters:

- Topic handling is a major shipped strength, but it also introduces choreographed helper steps and
  topic-state semantics that the model must carry.

### 4. Search contract shape

Likely evidence anchors:

- `SearchMessages` hit-window behavior
- `[HIT]` marking
- `next_offset` pagination

Why this matters:

- Search is not just “return matches.” It already encodes a specific result shape and pagination
  contract that must be compared against the reading flow.

### 5. Recovery and failure boundaries

Likely evidence anchors:

- action-oriented recovery helpers in `tools.py`
- resolver behavior
- server-boundary generic exception wrapping in `server.py`

Why this matters:

- The current surface appears strong on guided recovery inside handlers, but weaker when failures
  escape to the generic `Tool <name> failed` boundary. That contrast should be planned as an audit
  theme.

## Preserved Invariants From Phase 10

These are default-preserve constraints for Phase 11 framing and later Phase 12 interpretation:

- read-only Telegram scope
- privacy-safe telemetry; no message-content logging
- stateful runtime behavior through cached client and XDG-backed databases
- recovery-critical caches and topic tombstones
- text-first result contract as current shipped reality
- action-oriented recovery as a real strength, not incidental implementation detail
- reflection-based tool exposure and process-start discovery snapshot as current-surface facts
- Phase 10 evidence hierarchy and rubric

Planning implication:

- Phase 11 should surface redesign pressure without casually labeling these invariants as defects.
- The audit should distinguish between “current burden worth changing” and “current property that
  later options must preserve.”

## Validation Architecture

Phase 11 should have a Nyquist validation artifact: `11-VALIDATION.md`.

This is a research/document phase, so validation should stay shell-first and structure-first, with
manual editorial checks for claim quality.

### Quick validation

Run after each meaningful document update.

Recommended quick checks:

- verify the primary audit artifact exists
- verify all seven tool names appear
- verify all five workflow names appear
- verify the terms `strong`, `mixed`, `weak` appear
- verify leakage categories appear
- verify preserved invariants appear

Example command shape:

```bash
rg -n "GetMyAccount|GetUsageStats|GetUserInfo|ListDialogs|ListMessages|ListTopics|SearchMessages|discovery|reading|search|topic handling|recovery/error|strong|mixed|weak|pagination|disambiguation|tool choreography|text-first|privacy-safe telemetry" .planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md
```

### Full validation

Run at the end of each plan wave and before phase verification.

Recommended full checks:

1. Runtime reflection still matches the audited tool list.
2. The audit artifact cites the required external sources.
3. The audit artifact cites the required brownfield anchors.
4. The audit explicitly names preserved invariants and leakage categories.

Example command shape:

```bash
UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools
rg -n "MCP|Anthropic|server.py|tools.py|resolver.py|formatter.py|telegram.py|cache.py|analytics.py|tests/test_tools.py|tests/test_resolver.py|tests/test_formatter.py|tests/test_analytics.py|tests/privacy_audit.sh|next_cursor|next_offset|from_beginning|previously_inaccessible|Tool <name> failed" .planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md
```

### Manual verification

The planner should require manual checks for:

- every major finding pairs one named external source with one specific brownfield anchor
- every tool has at least one explicit judgment
- every workflow has at least one explicit judgment
- leakage findings explain *model burden*, not just implementation detail
- strengths are preserved explicitly, not lost in a gap-only critique
- the final synthesis is strong enough that Phase 12 can compare redesign options without repeating
  current-state discovery

## Planning Risks

The main ways to plan Phase 11 badly are:

- turning it into a generic best-practice essay instead of a grounded audit
- focusing on tools but not workflows
- cataloging weaknesses without preserving strengths and invariants
- relying on stale planning notes instead of runtime reflection and tests
- skipping the explicit contract-leak section, which would leave AUDIT-03 under-specified

## Recommended Phase Shape

A planner could structure Phase 11 into three plan units:

1. Build the audit scaffold and tool matrix from reflected runtime plus source metadata.
2. Audit the five workflows and contract-leak patterns from source/tests.
3. Synthesize strengths, gaps, and preserved invariants into the final comparison matrix, then run
   validation.

That sequencing matches the requirements cleanly:

- AUDIT-01 depends on named evidence being threaded through all findings
- AUDIT-02 depends on both tool and workflow coverage
- AUDIT-03 depends on an explicit leak inventory and synthesis section

## RESEARCH COMPLETE

Changed files:

- `.planning/phases/11-current-surface-comparative-audit/11-RESEARCH.md`
