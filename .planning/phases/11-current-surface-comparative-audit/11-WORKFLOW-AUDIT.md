# Phase 11 Workflow Audit: Current MCP Surface

Last verified: 2026-03-13

This artifact audits the shipped `mcp-telegram` surface as an end-to-end LLM workflow. It focuses
on what the model must actually do to discover tools, read messages, search history, handle forum
topics, and recover from resolution or runtime failures.

## Scope and Method

- Judgment bands follow the Phase 10 rubric: `strong`, `mixed`, `weak`.
- Named evidence comes from the retained Phase 10 evidence log:
  - MCP Tools specification
  - Anthropic implement-tool-use doc
  - Anthropic tool-use overview
  - Live reflected tool list (`UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`)
- Brownfield anchors come from current runtime reflection, source, and tests.
- Workflow burden is treated as part of the public contract rather than an implementation detail.

## Workflow Matrix

| Workflow | tool choreography | Main continuation burden | Judgment band | Named evidence and direct anchors | Why it matters for later redesign |
| --- | --- | --- | --- | --- | --- |
| discovery | `tools/list` reflection -> `ListDialogs` -> optional retry with exact dialog id or `@username` | Discovery is usable, but freshness is bounded by a process-start snapshot and real work often starts with a helper-step inventory call before the user task begins. | mixed | MCP Tools specification; Anthropic implement-tool-use doc; Live reflected tool list; [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L29), [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L35), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L962), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2469) | Phase 12 needs to decide whether discovery should keep reflection-based exposure but reduce stale-snapshot risk and the need for a separate dialog-catalog warm-up step. |
| reading | `ListDialogs` -> `ListMessages` -> optional `next_cursor` retries or `from_beginning=True` pass -> optional sender/topic retry | The reading path works, but the model must learn two reading modes, remember `next_cursor`, and sometimes stage helper calls before it can read the desired slice. | mixed | Anthropic implement-tool-use doc; Anthropic tool-use overview; [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1140), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1232), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1567), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L352), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2371) | Later redesign work must decide whether the surface should keep both pagination directions and topic-aware transcript rendering without forcing the model to infer low-level paging semantics. |
| search | `ListDialogs` or known dialog -> `SearchMessages` -> optional `next_offset` retries -> parse grouped hit windows | Search is close to the user job, but continuation depends on a different pagination convention from reading and the model still parses prose output rather than stable fields. | mixed | Anthropic tool-use overview; Anthropic implement-tool-use doc; [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1597), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1767), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1675), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1715), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1759) | Phase 12 should revisit whether search continuation and hit/context structure can be made more uniform with reading without losing the current useful context-window behavior. |
| topic handling | `ListDialogs` -> `ListTopics` -> `ListMessages(topic=...)`, or dialog-wide `ListMessages` without topic for cross-topic reading | Topic-aware reading is unusually well taught, but it still leaks helper-step burden because exact topic choice, deleted-topic handling, and inaccessible-topic recovery all depend on separate topic metadata. | mixed | Anthropic implement-tool-use doc; Live reflected tool list; [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1042), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1283), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1565), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L128), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L588), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L632) | Later redesigns should preserve the project’s topic-state fidelity while deciding whether forum browsing can be made more direct for common “read this thread” jobs. |
| recovery/error flows | handler-local recovery text -> retry with exact ids/titles/cursors -> fallback to dialog-wide reads when topic fetch fails -> server boundary generic wrapper when exceptions escape | Recovery is a real project strength inside handlers, but the public contract still has a hard clarity cliff when failures escape to generic `Tool <name> failed` wrapping. | mixed | MCP Tools specification; Anthropic tool-use overview; [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L72), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L507), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L595), [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L604), [tests/test_resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_resolver.py#L82), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2004) | Phase 12 must preserve action-oriented recovery where it exists today while shrinking the gap between rich handler guidance and generic server-boundary failure collapse. |

## discovery workflow

**Typical tool choreography:** `tools/list` reflection, then `ListDialogs`, then a second tool call
with an exact dialog id, full title, or `@username`.

**Main user-visible continuation burden:** the model has to do helper-step discovery before reading
or searching, and discovery freshness is limited because the server snapshots `mapping` from
`enumerate_available_tools()` at process start rather than refreshing dynamically.

**Judgment band:** `mixed`

**Named evidence and direct anchors:**
- MCP Tools specification plus the live reflected tool list establish that tool discovery is
  reflection-driven today.
- [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L29) reflects
  `ToolArgs` subclasses into the public tool list.
- [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L35) snapshots the
  tool mapping at import time.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L962) makes
  `ListDialogs` the effective inventory tool for later workflow steps.

**Why this matters for later redesign:** the current surface already teaches discovery, but it also
teaches that the model must inventory dialogs and trust a static reflection snapshot before real
work begins. That is redesign pressure even though the path is understandable.

## reading workflow

**Typical tool choreography:** `ListDialogs` to identify the chat, then `ListMessages`, then
follow-up calls with `next_cursor` or `from_beginning=True`, with optional sender or topic retry.

**Main user-visible continuation burden:** the model must remember cursor tokens, infer whether it
is paging backward or forward through time, and sometimes call `ListTopics` before it can read one
thread cleanly.

**Judgment band:** `mixed`

**Named evidence and direct anchors:**
- Anthropic implement-tool-use doc is the comparison point for whether tool descriptions teach the
  continuation contract well enough before invocation.
- Anthropic tool-use overview is the comparison point for whether text-first transcript output is a
  workable downstream reasoning surface.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1140) documents the
  `ListMessages` contract, including `cursor`, `topic`, `sender`, and `from_beginning=True`.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1232) splits cursor
  handling by iteration direction.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1567) emits
  `next_cursor` in text output rather than a separate structured field.
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L352) and
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2371) confirm
  both default pagination and `from_beginning=True`.

**Why this matters for later redesign:** reading is the most common real task, so pagination
semantics and transcript shape determine whether the model is doing message comprehension or
contract bookkeeping.

## search workflow

**Typical tool choreography:** `SearchMessages` once the dialog is known, then repeated calls with
`next_offset`, while parsing grouped hit windows and `[HIT]` markers.

**Main user-visible continuation burden:** the model must switch from the reading workflow’s
`next_cursor` convention to search’s `next_offset` convention and continue working from text-first
groups rather than stable hit records.

**Judgment band:** `mixed`

**Named evidence and direct anchors:**
- Anthropic tool-use overview is the external comparison point for structured-output expectations.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1597) documents the
  search contract and its distinct continuation rule.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1767) emits
  `next_offset` in-band in the result text.
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1675) and
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1715) lock the
  context-window grouping and `[HIT]` marker behavior.
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1759) locks
  offset pagination.

**Why this matters for later redesign:** search is close to the user’s intent, so the main pressure
is not missing capability but inconsistent continuation and parsing cost relative to reading.

## topic handling workflow

**Typical tool choreography:** `ListDialogs` -> `ListTopics` -> `ListMessages(topic=...)`, with the
alternative of omitting `topic=` to read a mixed cross-topic page labeled inline.

**Main user-visible continuation burden:** the model often needs a helper discovery step before it
can read one thread safely, and it must interpret topic states such as `general`, `active`,
`deleted`, or `previously_inaccessible`.

**Judgment band:** `mixed`

**Named evidence and direct anchors:**
- Anthropic implement-tool-use doc is the comparison point for whether the public surface teaches
  the topic-selection sequence before the model guesses.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1042) explicitly says
  to use `ListTopics` before `topic=`.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L699) preserves
  `previously_inaccessible` as a stable user-visible topic state.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1565) prepends an
  explicit topic label for thread-scoped reads.
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L155) confirms
  `previously_inaccessible` topic rows.
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L588) and
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L632) confirm
  both thread-specific and cross-topic output shapes.

**Why this matters for later redesign:** forum-topic support is a real strength, but the helper
steps and topic-state semantics are part of the user-visible contract that a redesign must either
simplify or defend explicitly.

## recovery/error workflow

**Typical tool choreography:** a failing call returns action-oriented text, the model retries with
an exact dialog/user/topic/cursor, and only falls back to generic failure when the exception escapes
the handler boundary.

**Main user-visible continuation burden:** recovery is explicit for many ordinary failures, but the
model has to distinguish between recoverable handler text and opaque server-boundary collapse.

**Judgment band:** `mixed`

**Named evidence and direct anchors:**
- Anthropic tool-use overview is the comparison point for whether recovery guidance remains legible
  enough for downstream reasoning.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L507) through
  [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L633) define a broad
  family of action-oriented not-found and ambiguous responses.
- [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L604) gives explicit
  invalid cursor recovery text.
- [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L72) wraps escaped
  failures as `Tool <name> failed`.
- [tests/test_resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_resolver.py#L82) locks
  ambiguous candidate behavior rather than silent auto-selection.
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2004) confirms
  invalid cursor recovery is model-actionable rather than a raw exception.

**Why this matters for later redesign:** this project already invests in recovery guidance, so the
real redesign question is how to preserve that strength while reducing the remaining generic-failure
cliff and helper-step burden.
