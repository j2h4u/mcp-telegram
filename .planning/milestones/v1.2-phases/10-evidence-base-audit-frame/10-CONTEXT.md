# Phase 10: Evidence Base & Audit Frame - Context

**Gathered:** 2026-03-13
**Status:** Ready for planning

<domain>
## Phase Boundary

Establish the evidence base, source hierarchy, audit rubric, and brownfield constraints for
evaluating the shipped `mcp-telegram` MCP surface. This phase is research setup only. It does not
change the public MCP contract, add new capabilities, or pre-decide the redesign path that later
phases will compare.

</domain>

<decisions>
## Implementation Decisions

### Source hierarchy
- Optimize for rigor over breadth because the user explicitly delegated methodology choices.
- Treat external evidence in three tiers:
  - Tier 1 authoritative: official MCP specification/docs, official Anthropic guidance directly
    relevant to MCP/tool design, and other official first-party docs when they define behavior or
    expectations.
  - Tier 2 supporting: official issue discussions, maintainer commentary, and official SDK docs
    that clarify but do not redefine the normative guidance.
  - Tier 3 contextual: community posts, blog posts, and secondary commentary used only when they
    help explain tradeoffs, never as the sole basis for conclusions.
- Treat local code, tests, and runtime behavior as the authority for current `mcp-telegram`
  reality. Later audit claims should pair external guidance with brownfield code anchors whenever
  possible.

### Evidence log shape
- Keep the output tight and decision-oriented, not a literature review.
- Record only sources that materially influence later audit or redesign conclusions.
- For each retained source, capture:
  - source name and tier
  - exact area it informs
  - why it applies to `mcp-telegram`
  - which later phase(s) should rely on it
- Prefer a compact evidence matrix or log over long prose summaries.

### Audit rubric structure
- Use a hybrid rubric: fixed evaluation dimensions plus short narrative evidence, not a weighted
  numeric score.
- The fixed Phase 10 rubric dimensions are:
  - task-shape fit
  - metadata/schema clarity
  - continuation burden
  - ambiguity recovery
  - structured-output expectations
- Apply the rubric both tool-by-tool and workflow-by-workflow so Phase 11 can evaluate the public
  surface from both perspectives.
- Use simple judgment bands such as `strong`, `mixed`, and `weak` with evidence notes instead of
  false precision.

### Brownfield constraint envelope
- Freeze the current public surface from live code and tests, not from stale planning notes.
- Capture these constraints as non-negotiable starting context for later audit work:
  - read-only Telegram access remains a hard invariant
  - tool exposure is reflection-based from `ToolArgs` subclasses
  - reflected tool inventory is snapshotted at process start rather than refreshed dynamically
  - result bodies are text-first and usually a single `TextContent`
  - unhandled handler failures are wrapped as generic `Tool <name> failed` server-boundary errors
  - recovery is action-oriented, with explicit retry guidance on ambiguity and not-found paths
  - `ListDialogs` defaults to mixed archived + non-archived scope and exposes `exclude_archived`
    plus `ignore_pinned`
  - `ListTopics` exposes status distinctions such as `general`, `active`, and
    `previously_inaccessible`
  - `ListMessages` preserves deleted-topic and inaccessible-topic recovery behavior
  - `ListMessages` supports forward-in-time pagination through `from_beginning=True`
  - `SearchMessages` is hit-centric, with `+-3` context windows and explicit `[HIT]` marking
  - pagination contracts are mixed today (`next_cursor` for `ListMessages`, `next_offset` for
    `SearchMessages`)
  - process state exists via cached Telegram client plus XDG-backed cache and analytics databases
  - privacy-safe telemetry exists, but it is aggregate and intentionally avoids message-content
    logging

### Claude's Discretion
- Exact evidence-log table columns and document layout
- Whether supporting-source commentary lives inline or in a short appendix
- Whether telemetry observations are folded into the audit rubric or presented as a separate note
- How concise the final Phase 10 artifact can be while still guiding Phases 11–13 cleanly

</decisions>

<specifics>
## Specific Ideas

- The user did not want to steer the methodology details and explicitly delegated those choices to
  downstream agents.
- The phase should stay narrow: set up a trustworthy audit frame, not a broad research survey.
- Later conclusions should be traceable to named sources and to concrete `mcp-telegram` behaviors.

</specifics>

<code_context>
## Existing Code Insights

### Reusable Assets
- `src/mcp_telegram/tools.py`: canonical source for public tool contracts, result conventions,
  pagination behavior, topic handling, and telemetry recording.
- `src/mcp_telegram/server.py`: authoritative tool-discovery path; confirms reflection-based MCP
  exposure, process-start tool snapshotting, empty prompts/resources/templates, and generic
  server-boundary failure wrapping.
- `src/mcp_telegram/telegram.py`: canonical source for cached-client behavior and XDG-backed session
  state.
- `src/mcp_telegram/resolver.py`: canonical ambiguity-recovery rules and exact-match-only
  auto-resolution behavior.
- `src/mcp_telegram/formatter.py`: canonical text-first rendering contract for message output.
- `src/mcp_telegram/analytics.py`: privacy-safe telemetry model for usage evidence.
- `src/mcp_telegram/cache.py`: durable entity, reaction, and topic metadata that define
  brownfield statefulness.

### Established Patterns
- Tool descriptions and input schemas come from `ToolArgs` docstrings and Pydantic models, then
  pass through `_sanitize_tool_schema()` before exposure.
- All current public tools are read-oriented and return human-readable text, with structured input
  but largely unstructured output.
- Ambiguity and recovery paths are deliberate UX, not incidental errors; tests lock this behavior.
- `ListMessages` supports both backward cursor pagination and forward-in-time reading through
  `from_beginning=True`.
- `SearchMessages` is not a plain hit list; it returns grouped hit windows with explicit `[HIT]`
  marking and surrounding context.
- Forum/topic behavior exposes additional contract details beyond simple filtering, including topic
  statuses and deleted/inaccessible topic recovery paths.
- Forum support adds multi-step workflows (`ListDialogs` -> `ListTopics` -> `ListMessages`) that
  should be audited as workflow burden, not just per-tool behavior.

### Integration Points
- Phase 11 should anchor findings in `tools.py`, `server.py`, and the relevant tool tests.
- Phase 11 should treat formatter tests, resolver tests, telemetry tests, and privacy audit checks
  as part of the evidence base for current-surface behavior.
- Phase 11 should treat `telegram.py` as part of the brownfield authority for cached-client and
  session-state constraints.
- Phase 12 and Phase 13 should inherit the source hierarchy and rubric from this file rather than
  redefining evaluation criteria.

</code_context>

<deferred>
## Deferred Ideas

- Public MCP contract redesign belongs to later phases in v1.2, not this setup phase.
- Deferred v1.1 cleanup and large-forum validation remain backlog work unless Phase 10 research
  discovers they materially distort the audit surface.

</deferred>

---

*Phase: 10-evidence-base-audit-frame*
*Context gathered: 2026-03-13*
