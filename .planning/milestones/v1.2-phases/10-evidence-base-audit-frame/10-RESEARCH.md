# Phase 10: Evidence Base & Audit Frame - Research

**Researched:** 2026-03-13
**Domain:** MCP tool-surface evidence hierarchy, model-facing audit criteria, `mcp-telegram` brownfield constraints
**Confidence:** HIGH

## Summary

Phase 10 should stay narrow and produce a reusable audit frame rather than a broad best-practice survey.
The planning implication is:

1. Use official MCP and Anthropic tool documentation as the normative external evidence base.
2. Use the live reflected tool list, source code, and tests as the authority for current `mcp-telegram`
   behavior.
3. Capture later-phase inputs in three durable artifacts:
   - an evidence log that ranks sources and records why each source applies
   - a brownfield baseline that freezes the current public contract and workflow burden
   - an audit frame that defines the rubric, judgment bands, and handoff rules for Phases 11-13

The current MCP surface is not the six-tool list in older notes. The live reflected surface exposes
seven public tools: `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`,
`ListTopics`, and `SearchMessages`. Later audit work should treat that reflected runtime inventory as
authoritative.

## Research Question

What does the maintainer need in order to plan Phase 10 well enough that later phases can audit and
redesign the MCP surface without drifting away from either official guidance or actual code reality?

## Normative External Sources

### MCP Tools Specification

Source:
- `https://modelcontextprotocol.io/specification/2025-03-26/server/tools`

Why it matters:
- Establishes the MCP mental model for tools as model-controlled capabilities.
- Defines discovery and invocation via `tools/list` and `tools/call`.
- Frames the user interaction model, including human-in-the-loop expectations.

Planning implication:
- Phase 10 should evaluate whether `mcp-telegram` presents a tool surface that is easy for a model to
  discover and call, not just whether the handlers work.

### Anthropic Tool-Use Implementation Guidance

Sources:
- `https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use`
- `https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/overview`

Why they matter:
- They emphasize that tool definitions are contracts.
- They explicitly call out detailed plaintext descriptions and precise input schemas as high-leverage
  steering surfaces.
- Anthropic also documents strict structured outputs for tool input conformance, which is relevant as
  a comparison point when auditing unstructured text-first result contracts.

Planning implication:
- Phase 10 should treat description quality, schema clarity, and structured-output expectations as
  first-class audit dimensions.

## Brownfield Authority Sources

These are not secondary evidence; they are the authority for current project reality.

### Live Reflected Tool Surface

Source:
- `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`

Observed on 2026-03-13:
- Public tools exposed: `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`,
  `ListMessages`, `ListTopics`, `SearchMessages`

Planning implication:
- Phase 10 must freeze the public surface from live reflection, because older notes are already stale.

### Tool Exposure Path

Sources:
- `src/mcp_telegram/server.py`
- `src/mcp_telegram/tools.py`

Observed facts:
- Tool discovery is reflection-based over `ToolArgs` subclasses.
- The tool mapping is snapshotted at process start through cached enumeration.
- `tool_description()` derives tool metadata from class docstrings and Pydantic JSON schema.
- `server.py` exposes no prompts, resources, or resource templates today.
- Unhandled tool exceptions are wrapped as generic `Tool <name> failed` errors at the server boundary.

Planning implication:
- Later phases must audit not only tool handlers, but also the absence of prompts/resources and the
  metadata path that reaches the client.

### Current Result Contract and Recovery Style

Sources:
- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/formatter.py`
- `src/mcp_telegram/resolver.py`
- `tests/test_tools.py`

Observed facts:
- Public outputs are overwhelmingly single `TextContent` bodies.
- Recovery is text-first and action-oriented: not-found, ambiguity, invalid cursor, topic-unavailable,
  and telemetry-empty cases all instruct the model what to do next.
- Workflow burden is part of the public contract, especially for discovery and forum-topic flows.
- `ListMessages` includes forward pagination through `from_beginning=True`, not just backward cursor
  pagination.
- `SearchMessages` is hit-centric: it groups results with `+-3` context windows and explicit `[HIT]`
  marking.

Planning implication:
- Phase 11 must audit workflow choreography and continuation burden, not just per-tool descriptions.

### Brownfield State and Privacy Constraints

Sources:
- `src/mcp_telegram/cache.py`
- `src/mcp_telegram/analytics.py`
- repo instructions in `AGENTS.md`

Observed facts:
- Telegram access is read-only.
- Runtime state exists through cached Telegram client, XDG-backed cache DBs, and analytics DB.
- Telemetry is deliberately aggregate and excludes message content.
- Existing tests and runtime behavior outrank stale planning notes.

Planning implication:
- Later redesign recommendations must preserve read-only scope, privacy-safe telemetry, and any
  recovery-critical state that materially reduces agent burden.

## Evidence Log Shape

Phase 10 should keep a compact evidence matrix rather than long narrative summaries.

Recommended columns:

| Column | Purpose |
|--------|---------|
| Source | Named document, runtime command, or code anchor |
| Tier | `Primary external`, `Brownfield authority`, `Supporting official`, or `Context only` |
| Area informed | Source hierarchy, metadata quality, workflow burden, rubric design, invariants |
| Why it applies | Why the source materially constrains `mcp-telegram` rather than offering generic advice |
| Later consumers | Which of Phases 11, 12, or 13 should reuse it |

Retention rule:
- Include a source only if later audit findings or redesign decisions would cite it directly.

## Recommended Source Hierarchy

### Primary External

Use as the default basis for normative claims:
- official MCP specification/docs for tools
- official Anthropic docs directly about tool use, tool definition, and structured outputs

### Brownfield Authority

Use as the default basis for current-state claims:
- live reflected tool list
- code in `server.py`, `tools.py`, `resolver.py`, `formatter.py`, `cache.py`, `analytics.py`
- code in `telegram.py` for cached-client/session-state behavior
- tests that lock behavior and recovery semantics

### Supporting Official

Use only when they clarify but do not override the above:
- official SDK docs
- maintainer or official issue discussions, if needed later

### Context Only

Use sparingly and never as sole justification:
- blogs
- community guidance
- third-party MCP commentary

## Audit Frame Requirements

The rubric should be non-numeric and judgment-based. Numeric scoring would create false precision at
this phase.

Required dimensions from roadmap/context:
- task-shape fit
- metadata/schema clarity
- continuation burden
- ambiguity recovery
- structured-output expectations

Recommended judgment bands:
- `strong`
- `mixed`
- `weak`

Each rubric row should require:
- current behavior observed in `mcp-telegram`
- named evidence backing the judgment
- preserved invariant or redesign pressure implied by the finding

## Brownfield Baseline That Later Phases Must Freeze

Phase 10 planning should require the execution phase to capture these baseline facts explicitly:

1. Tool exposure is reflection-based from `ToolArgs` subclasses in `server.py`.
2. The current public surface includes seven tools, including `ListTopics`.
3. `list_prompts`, `list_resources`, and `list_resource_templates` currently return empty lists.
4. Tool metadata comes from docstrings plus Pydantic-generated input schema passed through
   `_sanitize_tool_schema()`.
5. Tool discovery is not live-refreshing within the process because the reflected tool mapping is
   snapshotted at process start.
6. Unhandled handler exceptions are collapsed to `Tool <name> failed` at the server boundary.
7. Result bodies are text-first, usually one `TextContent`, and do not expose a structured
   result schema.
8. Recovery is part of the contract: ambiguity, not-found, invalid cursor, and forum-topic edge
   cases all return action-guiding text rather than opaque failures.
9. Workflow burden exists in the public contract, including multi-step discovery and forum-topic
   flows such as `ListDialogs -> ListTopics -> ListMessages`.
10. `ListDialogs` defaults to mixed archived + non-archived scope and exposes `exclude_archived`
    plus `ignore_pinned`.
11. `ListTopics` exposes topic status distinctions such as `general`, `active`, and
    `previously_inaccessible`.
12. `ListMessages` preserves deleted-topic and inaccessible-topic recovery paths and, in forum
    dialogs without `topic=`, returns a cross-topic page with inline topic labels.
13. `ListMessages` supports two reading modes:
    - backward pagination via `next_cursor`
    - forward-in-time pagination via `from_beginning=True`
14. `SearchMessages` returns hit-centric groups with `+-3` context windows and explicit `[HIT]`
    marking.
15. Pagination conventions are mixed today:
   - `ListMessages` uses `next_cursor`
   - `SearchMessages` uses `next_offset`
16. Runtime state is not purely stateless because caches, analytics, and cached client creation
   affect later interactions.
17. Privacy-safe telemetry is an invariant: aggregate metrics are retained, message content is not.

## Evidence Matrix Seed

These are the minimum sources the execution phase should retain unless later evidence proves one is
irrelevant:

| Source | Tier | Informs | Why it applies | Later consumers |
|--------|------|---------|----------------|-----------------|
| MCP Tools spec (`2025-03-26`) | Primary external | Tool discovery, invocation model, human-in-loop expectations | Defines the protocol contract that `mcp-telegram` claims to implement | 11, 12, 13 |
| Anthropic implement-tool-use doc | Primary external | Description quality, schema clarity, tool contract expectations | Directly describes the levers LLMs rely on when choosing tools | 11, 12 |
| Anthropic tool-use overview | Primary external | Contract framing, strict structured-output comparison point | Useful for auditing where text-only outputs create model burden | 11, 12 |
| `uv run cli.py list-tools` reflected inventory | Brownfield authority | Current public surface | Shows what the client actually sees today | 10, 11 |
| `src/mcp_telegram/server.py` | Brownfield authority | Discovery path, absence of prompts/resources | Freezes how tools are exposed today | 10, 11, 12 |
| `src/mcp_telegram/telegram.py` | Brownfield authority | Cached-client statefulness and session storage | Confirms process-cached Telegram client and XDG-backed state are part of runtime reality | 10, 11, 12 |
| `src/mcp_telegram/tools.py` | Brownfield authority | Descriptions, schemas, pagination, recovery, workflow burden | Most surface behavior lives here | 10, 11, 12 |
| `src/mcp_telegram/resolver.py` | Brownfield authority | Ambiguity recovery and resolution expectations | Governs a major part of continuation burden | 10, 11 |
| `src/mcp_telegram/formatter.py` | Brownfield authority | Text-first output conventions | Locks presentation shape seen by the model | 10, 11 |
| `src/mcp_telegram/cache.py` | Brownfield authority | Stateful constraints and topic metadata persistence | Defines durable state that later redesigns must not ignore | 10, 11, 12 |
| `src/mcp_telegram/analytics.py` | Brownfield authority | Privacy-safe telemetry invariant | Constrains recommendation space in later phases | 10, 12, 13 |
| `tests/test_formatter.py` | Brownfield authority | Date/session formatting contract | Confirms message rendering conventions the model actually sees | 10, 11 |
| `tests/test_resolver.py` | Brownfield authority | Resolution/disambiguation behavior | Confirms ambiguity recovery is deliberate contract, not incidental code | 10, 11 |
| `tests/test_analytics.py` | Brownfield authority | Telemetry privacy assertions | Confirms analytics constraints are test-backed | 10, 12, 13 |
| `tests/privacy_audit.sh` | Brownfield authority | Privacy-safe telemetry audit guardrail | Adds repo-level privacy evidence beyond unit tests | 10, 12, 13 |
| `tests/test_tools.py` | Brownfield authority | Locked behavior, especially recovery flows | Confirms contract intent beyond implementation details | 10, 11 |

## Recommended Plan Split

Phase 10 is best planned as three execution plans across two waves.

### Wave 1A: Source Hierarchy and Evidence Log

Deliverable:
- `10-EVIDENCE-LOG.md`

Purpose:
- capture the retained source set, source tiers, applicability notes, and explicit reuse guidance

### Wave 1B: Brownfield Surface Baseline

Deliverable:
- `10-BROWNFIELD-BASELINE.md`

Purpose:
- freeze the reflected public surface, current workflow burden, pagination/recovery conventions, and
  preserved invariants from code/tests/runtime

### Wave 2: Audit Frame and Handoff

Deliverable:
- `10-AUDIT-FRAME.md`

Purpose:
- merge the evidence hierarchy and brownfield baseline into the rubric and instructions that Phases
  11-13 will consume

## Risks To Plan Around

### Risk 1: Stale planning notes bias the audit

Mitigation:
- require live reflection and code anchors in the brownfield baseline

### Risk 2: The phase drifts into generic literature review

Mitigation:
- enforce the evidence-log retention rule and require applicability notes for every retained source

### Risk 3: Later phases lose traceability between findings and sources

Mitigation:
- require every major Phase 11 finding to cite one external source and one brownfield anchor where
  applicable

### Risk 4: Tool-level audit misses multi-step burden

Mitigation:
- require workflow-level audit coverage for discovery, reading, search, topic handling, and
  recovery/error flows

## Validation Architecture

Phase 10 execution should validate artifacts with quick, deterministic shell checks rather than
waiting for later narrative review.

Validation expectations:

1. `10-EVIDENCE-LOG.md` exists and explicitly distinguishes source tiers.
2. `10-BROWNFIELD-BASELINE.md` records the reflected seven-tool surface, the absence of
   prompts/resources/templates, and the current text-first result contract.
3. `10-AUDIT-FRAME.md` includes all five required rubric dimensions and the `strong/mixed/weak`
   judgment bands.
4. The audit frame explicitly tells Phase 11 to evaluate both tools and workflows.
5. Later-phase consumers are named in the evidence log so source reuse is intentional rather than
   ad hoc.

Recommended quick checks:
- `rg -n "Primary external|Brownfield authority|Context only" .planning/phases/10-evidence-base-audit-frame/10-EVIDENCE-LOG.md`
- `rg -n "ListTopics|list_prompts|list_resources|text-first|next_cursor|next_offset" .planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md`
- `rg -n "task-shape fit|metadata/schema clarity|continuation burden|ambiguity recovery|structured-output expectations|strong|mixed|weak" .planning/phases/10-evidence-base-audit-frame/10-AUDIT-FRAME.md`

Recommended full check:
- `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`
- compare that output against the brownfield baseline before closing the phase

## Planning Verdict

Phase 10 is ready for planning now. Research is sufficient because:
- the normative external sources are identified and current as of 2026-03-13
- the live brownfield surface has been confirmed locally
- the phase boundary is narrow and does not depend on unresolved product decisions

The highest-leverage plan is:
- parallelize evidence capture and brownfield baseline work in Wave 1
- use Wave 2 only for the audit rubric and later-phase handoff synthesis

---

*Phase: 10-evidence-base-audit-frame*
*Research completed: 2026-03-13*
