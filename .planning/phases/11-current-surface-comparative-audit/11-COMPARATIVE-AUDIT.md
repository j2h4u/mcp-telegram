# Phase 11 Comparative Audit: Current MCP Surface

Last verified: 2026-03-13

This document is the primary current-state audit deliverable for Phase 11. It compresses the
tool-level and workflow-level findings into one evidence-backed view of what the shipped
`mcp-telegram` MCP surface does well, where it burdens the model, and which properties later phases
should treat as default-preserve constraints.

## Scope and Method

- Scope: the shipped seven-tool reflected surface on 2026-03-13:
  `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`, and
  `SearchMessages`.
- Method: reuse the retained Phase 10 evidence set rather than introducing new research. Normative
  external guidance remains the MCP Tools specification plus Anthropic tool-use guidance. Brownfield
  authority remains live reflection, source, and tests.
- Brownfield anchors: live `uv run cli.py list-tools`; [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py),
  [tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py),
  [resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/resolver.py),
  [formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py),
  [pagination.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py),
  [analytics.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/analytics.py), and
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py).
- Judgment posture: keep the Phase 10 `strong` / `mixed` / `weak` rubric, treat workflow burden as
  part of the public contract, and separate preserved invariants from redesign pressure rather than
  treating all critique as a recommendation to rebuild.

## Tool-by-Tool Audit Summary

| Tool | Primary user job | Current assessment | Current strengths | Current burden |
| --- | --- | --- | --- | --- |
| `GetMyAccount` | Confirm which Telegram account is active | `strong` | One-call fit, empty schema is clear, auth-gate text keeps the next step legible. | Returns a text line instead of stable fields. |
| `GetUsageStats` | Read local telemetry summary | `mixed` | Zero-argument call, privacy-safe aggregate summary, empty-state messaging is explicit. | Hidden dependence on local analytics state and prose-only metrics. |
| `GetUserInfo` | Resolve a natural-name user and inspect profile context | `mixed` | User-centered job shape, shared-chat context is useful, retry guidance is explicit. | Cache dependence, fuzzy-match retries, and text-first profile rendering. |
| `ListDialogs` | Discover reachable chats and channels | `strong` | Direct discovery fit, archived-scope knobs are legible, warms cache for later calls. | Often a helper step before the actual task; output is newline text rows. |
| `ListMessages` | Read one dialog or topic with recovery help | `mixed` | Rich recovery paths, dual pagination modes, topic-aware rendering, cross-topic reads are possible. | Heaviest continuation burden: helper calls, cursors, directionality, and prose parsing. |
| `ListTopics` | Discover exact forum-topic choices before reading | `strong` | Teaches topic selection clearly and preserves topic-state fidelity. | Mostly a prerequisite step rather than the final user task. |
| `SearchMessages` | Search one dialog with local hit context | `strong` | Search job maps well to the tool, hit windows are deliberate, continuation is explicit. | Uses a different continuation token from reading and stays text-first. |

Across the individual tools, the pattern is stable: discovery and recovery are better than a naive
text-only MCP surface, but structured continuation still leaks through tokens, retries, and helper
choreography.

## Workflow Audit Summary

| Workflow | Typical choreography | Current assessment | What works today | What still burdens the model |
| --- | --- | --- | --- | --- |
| discovery | `tools/list` -> `ListDialogs` -> exact retry value in later call | `mixed` | Reflection exposes the surface, and `ListDialogs` teaches real reachable names. | Real work often starts with inventory and cache warmup before the user task begins. |
| reading | `ListDialogs` -> `ListMessages` -> `next_cursor` or `from_beginning=True` follow-up | `mixed` | The read path is capable and recovery-rich. | The model has to learn paging direction, reuse cursor state, and sometimes stage topic lookup first. |
| search | known dialog or `ListDialogs` -> `SearchMessages` -> `next_offset` follow-up | `mixed` | Search is close to the actual user job and preserves hit-local context. | Continuation differs from reading, and hit state is embedded in prose. |
| topic handling | `ListDialogs` -> `ListTopics` -> `ListMessages(topic=...)` | `mixed` | Topic selection is explicit and deleted/inaccessible topic semantics are preserved. | Common forum reads still require separate topic discovery before reading. |
| recovery/error | handler guidance -> retry with exact names/cursors -> boundary fallback | `mixed` | Handler-local recovery is one of the strongest parts of the surface. | Escaped exceptions still collapse into generic `Tool <name> failed` wrapping. |

The workflow view matters because the model does not experience `tools.py` one function at a time.
It experiences helper calls, retries, cursor reuse, and topic lookups as part of one user-visible
job.

## Low-Level Contract Leakage

| Leak | Where it leaks | Why it matters |
| --- | --- | --- |
| Pagination mechanics | `ListMessages` exposes `next_cursor`; `SearchMessages` exposes `next_offset`; `from_beginning=True` adds a second read mode. | The model has to manage low-level continuation state instead of just asking for the next relevant slice. |
| Disambiguation retries | Dialog, sender, topic, and user resolution depend on candidate lists from [resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/resolver.py). | Explicit ambiguity is safer than silent guessing, but it still turns one user job into a multi-step retry flow. |
| Helper-step choreography | Discovery and forum reads frequently require `ListDialogs` and `ListTopics` before the real read or search. | The public contract teaches setup work that feels adjacent to the job rather than integral to it. |
| Reflection snapshot behavior | [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py) snapshots the reflected mapping at process start. | Discovery is authoritative for the running process, but stale runtime exposure remains a real operational edge. |
| Text-first parsing | [formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py) and tool handlers embed continuation cues in readable text. | Readability is good, but the model still has to parse transcripts, hit markers, topic labels, and next-step tokens out of prose. |
| Boundary failure collapse | [server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py) wraps escaped failures as `Tool <name> failed`. | The surface loses its strongest recovery behavior exactly when unexpected failures escape the handler boundary. |

These leaks are not evenly bad. Some are useful explicitness that later phases should reduce
carefully; others are pure redesign pressure because they consume attention without adding user-task
value.

## Preserved Invariants and Redesign Pressure

### Preserved invariants

- Read-only Telegram access remains a shipped boundary. The reflected surface is entirely oriented
  around listing, searching, lookup, and aggregate telemetry rather than mutation.
- Stateful runtime remains part of the real contract. [telegram.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/telegram.py)
  caches the client, and XDG-backed state such as the Telegram session, entity/topic caches, and
  analytics database survives across calls.
- Recovery-critical metadata should be preserved. Topic tombstones, inaccessible-topic history, and
  cache-backed resolution reduce repeated agent confusion and make later calls more recoverable.
- Privacy-safe telemetry is a default-preserve constraint. [analytics.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/analytics.py)
  and [tests/privacy_audit.sh](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/privacy_audit.sh)
  lock the rule that usage stats stay aggregate and do not log message content or user identifiers.
- Tests remain contract evidence, not optional implementation detail. Formatter, resolver,
  pagination, analytics, and tool tests define what is actually shipped today.

### Redesign pressure

- Reading and search both work, but they expose different continuation contracts for adjacent
  navigation jobs.
- Forum-topic support is genuinely valuable, yet common thread-reading tasks still require a helper
  discovery phase before the model can do the user-visible read.
- The surface invests heavily in action-oriented handler recovery, but that quality drops at the
  server boundary when unexpected exceptions escape.
- Text-first responses are readable for humans and models, but they still force downstream parsing
  of fields, markers, and pagination cues that could be more direct.
- Reflection-based discovery is appropriate for this project, but process-start snapshotting means
  runtime freshness is partly an operational concern rather than a purely contract-level guarantee.

## Current-State Synthesis

The current `mcp-telegram` surface is not a broken MCP server. It already reflects several good
decisions that later phases should avoid discarding casually: the public tools are read-oriented,
the docstrings and schemas usually teach the intended job, ambiguity recovery is explicit instead of
silently guessy, forum-topic edge cases are preserved as real state, and telemetry remains
privacy-safe.

The main weakness is different. The surface is capable, but it often makes the model carry the
contract machinery needed to finish the job. The model has to discover dialogs before reading them,
pick exact retry candidates after fuzzy resolution, remember whether the current path pages by
`next_cursor` or `next_offset`, and parse readable but still prose-shaped outputs for the state
needed to continue. That burden is strongest in message-reading and topic-handling workflows, where
the model can succeed but must spend attention on orchestration as well as the underlying Telegram
content.

That leaves Phase 11 with a stable conclusion: the current surface should be understood as
workflow-capable but continuation-heavy. It contains meaningful strengths worth preserving, but it
also exposes enough low-level mechanics that Phase 12 should compare redesign options around burden
reduction rather than around adding wholly new capabilities.

### Decision-Friendly Comparison Matrix

| area | current strength | current weakness | preserved invariant | redesign pressure |
| --- | --- | --- | --- | --- |
| account and operator context | `GetMyAccount` is a clear zero-argument check and keeps auth state legible. | Success still arrives as a text line that must be parsed for fields. | Keep the one-call, read-only account check. | Reduce prose parsing for basic identity state. |
| telemetry and local observability | `GetUsageStats` preserves privacy-safe telemetry and useful empty-state messaging. | The contract hides its local-state dependency and collapses metrics into prose. | Keep privacy-safe aggregate telemetry and no message-content logging. | Compare whether lightweight structure can expose metrics without widening privacy scope. |
| identity lookup | `GetUserInfo` frames the job around natural-name lookup and shared-chat context. | Fuzzy resolution and cache dependence can turn one query into a retry loop. | Preserve explicit ambiguity handling instead of silent auto-picks. | Reduce retry burden while keeping safe disambiguation. |
| dialog discovery | `ListDialogs` is a strong inventory surface and cache warmup step. | Many real tasks still need this helper call before the actual read or search. | Preserve reflection-based tool discovery and natural-name dialog inventory. | Compare ways to shrink helper-step discovery burden. |
| message reading | `ListMessages` handles the richest real task, including topic-aware reads and recovery help. | Cursor direction, helper choreography, and text-first continuation create the highest orchestration cost. | Preserve read-only access, readable transcripts, and recovery-aware topic handling. | Compare lower-burden continuation and thread selection models. |
| topic handling | `ListTopics` makes forum-topic state explicit, including inaccessible history. | Forum reads still require a separate topic catalog step for common tasks. | Preserve deleted/inaccessible topic fidelity and cross-topic read support. | Compare whether common topic reads can become more direct without dropping state fidelity. |
| search | `SearchMessages` aligns well with the user job and returns useful local hit context. | Search uses `next_offset`, which diverges from message reading's `next_cursor`. | Preserve local context windows around hits. | Compare a more uniform navigation contract across read and search workflows. |
| recovery boundary | Handler-local recovery text is unusually actionable for not-found, ambiguous, and invalid-cursor cases. | Escaped failures still degrade to generic `Tool <name> failed`. | Preserve explicit retry guidance and action-oriented failures where they already exist. | Remove the boundary between rich handler recovery and generic server wrapping. |
| runtime and state model | The surface already benefits from cached clients, caches, and durable local metadata. | Statefulness is helpful but implicit, especially for discovery freshness and cache-backed resolution. | Preserve the stateful runtime and recovery-critical caches. | Compare how to surface state assumptions more clearly without pretending the system is stateless. |
