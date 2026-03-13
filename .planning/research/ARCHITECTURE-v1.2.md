# Architecture: v1.2 MCP Surface Research

**Domain:** Telegram MCP server tool-surface redesign  
**Researched:** 2026-03-13  
**Goal:** Evolve the server from endpoint-shaped tools toward a smaller, capability-oriented, read-only MCP surface without breaking brownfield deployments unnecessarily.

## Recommendation

Choose the **medium migration** path.

It delivers the highest practical gain for this codebase because the current problem is not tool-count explosion, but **agent-visible orchestration leakage**:

- the public surface still exposes transport-shaped pagination (`cursor`, `offset`, `from_beginning`)
- topic lookup and ambiguity handling still require agent choreography
- recoverable domain states are mostly encoded as long text instructions rather than structured states
- every `ToolArgs` subclass is public by reflection today, so the public/internal boundary is weak

The server already has the internals needed to support macro-tools safely: resolver, SQLite entity/topic/reaction caches, a shared Telethon client factory, formatter utilities, and privacy-safe telemetry. The most defensible v1.2 direction is therefore:

1. introduce an explicit public tool registry
2. add a small macro-tool layer on top of existing internals
3. keep legacy tools temporarily behind a compatibility profile
4. preserve read-only behavior as a hard invariant, not a UI convention

## Brownfield Findings

### What exists now

The current server exposes tools by reflecting over every `ToolArgs` subclass in `src/mcp_telegram/tools.py`, then building the list in `src/mcp_telegram/server.py` ([`server.py`](../../src/mcp_telegram/server.py), lines 29-40). That means "defined class" currently equals "public MCP tool", which is convenient but too weak for a long-term boundary.

Current public tools:

- `ListDialogs`
- `ListTopics`
- `ListMessages`
- `SearchMessages`
- `GetMyAccount`
- `GetUserInfo`
- `GetUsageStats`

Key leakage points in the current public contract:

- `ListMessages` exposes `cursor`, `sender`, `topic`, `unread`, and `from_beginning`, plus text instructions telling the model how to retry and disambiguate ([`tools.py`](../../src/mcp_telegram/tools.py), lines 1140-1174 and 1189-1569).
- `SearchMessages` exposes Telegram-search-shaped `offset` pagination rather than a unified continuation model ([`tools.py`](../../src/mcp_telegram/tools.py), lines 1597-1768).
- `ListTopics` exists largely to support the agent in preparing a later `ListMessages(topic=...)` call, which is a classic sign that orchestration has leaked into the public surface ([`tools.py`](../../src/mcp_telegram/tools.py), lines 1042-1137).
- Most successful results are returned only as free text, even though MCP now supports structured tool output and output schemas.
- Unexpected execution failures become protocol errors through `call_tool`, while MCP guidance prefers recoverable tool-execution errors in tool results when the model can self-correct ([`server.py`](../../src/mcp_telegram/server.py), lines 72-93).

### What can already support macro-tools

The codebase already contains most of the substrate needed for a safer public layer:

- fuzzy resolution with `Resolved | Candidates | NotFound` in [`resolver.py`](../../src/mcp_telegram/resolver.py), lines 26-166
- entity, topic, and reaction metadata caches in [`cache.py`](../../src/mcp_telegram/cache.py), lines 22-260
- privacy-safe telemetry in [`analytics.py`](../../src/mcp_telegram/analytics.py), lines 35-223
- dialog/topic fetch helpers and fallback logic in [`tools.py`](../../src/mcp_telegram/tools.py), especially lines 227-409 and 1281-1549
- schema sanitization already exists, so emitting stricter MCP-facing schemas is incremental rather than architectural ([`tools.py`](../../src/mcp_telegram/tools.py), lines 147-188)

### Safety invariants worth preserving

These should remain non-negotiable:

- no send/edit/delete/write tools
- no message-content cache
- telemetry stays PII-safe
- topic/entity caches may persist metadata only
- all Telegram reads remain fresh at call time

That current stance is aligned with the product boundary in `.planning/PROJECT.md`, where read-only scope is explicitly treated as a security invariant rather than a product backlog choice.

## External Research That Matters Here

### MCP protocol guidance

From the MCP tools specification and related changelogs:

- MCP tools are model-controlled and should still assume a human can deny tool invocations; even read-only surfaces should be designed with trust and safety in mind.  
  Source: MCP Tools spec, 2025-06-18.
- Tool definitions now support `title`, `annotations`, `inputSchema`, and `outputSchema`; structured tool output is part of the spec as of the 2025-06-18 revision.  
  Source: MCP changelog, 2025-06-18.
- JSON Schema without `$schema` now defaults to 2020-12, and implementations must support that dialect.  
  Source: MCP basic spec, 2025-11-25.
- Tool results can return `structuredContent`, and when doing so should also include text for backward compatibility.  
  Source: MCP Tools spec, draft/2025-06-18.
- Tool execution errors should be returned as tool results with `isError: true` when the model can potentially recover, instead of always surfacing protocol-level failures.  
  Source: MCP Tools spec, draft/2025-06-18.
- Tool annotations include `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`, but annotations are hints and not security controls.  
  Source: MCP tools concepts page and 2025-03-26 changelog.

### Anthropic guidance

From Anthropic's tool-use documentation and engineering notes:

- Detailed descriptions are the strongest single lever for tool performance; Anthropic explicitly recommends at least 3-4 sentences for complex tools.  
  Source: Claude tool-use docs, "How to implement tool use".
- Anthropic recommends consolidating related operations into fewer tools when that reduces selection ambiguity.  
  Source: Claude tool-use docs, "How to implement tool use".
- Tool design should avoid exposing low-level identifiers when higher-signal fields are sufficient; semantic names outperform cryptic IDs.  
  Source: Anthropic Engineering, "Writing effective tools for agents".
- More tools are not automatically better; endpoint wrappers often perform worse than tools shaped around natural agent sub-tasks.  
  Source: Anthropic Engineering, "Writing effective tools for agents".
- Tool definitions consume context, and larger libraries should optimize for discovery and token efficiency.  
  Source: Anthropic Engineering, "Introducing advanced tool use on the Claude Developer Platform".

### What that means for this repo

For `mcp-telegram`, the research points to five concrete conclusions:

1. The next surface should be **task-shaped**, not Telethon-shaped.
2. The public contract should expose **stable capability names** and hide transport details like `add_offset`, `reply_to`, `max_id`, and numeric disambiguation retries.
3. Tool results should become **structured first, readable second**.
4. Because this server has only 7 public tools today, **tool-search/deferred loading is not the immediate lever**; better descriptions, clearer boundaries, and fewer public choreographies are.
5. Read-only safety should be made more legible in MCP metadata with annotations, but still enforced in code and surface design.

## Target Architecture

```text
MCP Client
   |
   v
Public Surface Registry
   |- telegram_discover
   |- telegram_read
   |- telegram_search
   |- telegram_get_contact
   |- telegram_get_self
   |
   +-- Compatibility Registry (optional profile)
   |      |- legacy ListDialogs / ListTopics / ListMessages / SearchMessages / ...
   |
   v
Application Services
   |- discovery service
   |- read planner
   |- search planner
   |- people/account service
   |- result serializer (text + structuredContent)
   |
   v
Existing internals
   |- Telethon client
   |- resolver
   |- formatter
   |- entity/topic/reaction caches
   |- analytics
```

### Boundary rules

**Public MCP tools**

- few in number
- named by user task
- accept natural-language inputs
- hide Telegram pagination mechanics
- return structured state for success, ambiguity, and not-found cases

**Compatibility tools**

- preserved only for migration
- off by default once macro-tools are stable
- explicitly marked legacy in descriptions

**Internal-only helpers**

- never subclass the public registration base unless intentionally exposed
- include topic loaders, pagination codecs, retry helpers, telemetry helpers, and low-level Telethon-specific fetch planners

## Proposed Public v2 Surface

### 1. `telegram_discover`

Purpose: one discovery tool for dialogs, contacts, and topics.

Why: it replaces agent choreography that currently spans `ListDialogs`, `ListTopics`, and parts of `GetUserInfo`.

Suggested input shape:

```json
{
  "kind": "dialogs | contacts | topics | auto",
  "query": "optional natural-language search",
  "dialog": "required only when kind=topics",
  "limit": 20
}
```

Suggested output shape:

```json
{
  "status": "ok | ambiguous | not_found | unavailable",
  "kind": "dialogs | contacts | topics",
  "items": [...],
  "resolved": {...},
  "guidance": "short natural-language fallback"
}
```

### 2. `telegram_read`

Purpose: read a dialog or topic without forcing the model to understand Telegram paging internals.

Why: this should become the main macro-tool and absorb most of the current `ListMessages` orchestration burden.

Suggested input shape:

```json
{
  "dialog": "required dialog name or @username",
  "topic": "optional natural-language topic name",
  "participant": "optional sender name",
  "mode": "recent | older | newer | unread | oldest",
  "limit": 50,
  "continuation": "optional opaque token"
}
```

Guidance:

- replace `cursor` and `from_beginning` with `mode` + `continuation`
- never require the model to send numeric dialog IDs for ordinary flow
- perform topic lookup internally
- if ambiguity remains, return structured candidates instead of prose-only retry instructions

### 3. `telegram_search`

Purpose: search messages with context, but through a stable continuation abstraction.

Why: search is a distinct retrieval mode and should remain separate from read, even though Anthropic recommends fewer tools. A single "do everything" Telegram tool would be too broad and would reintroduce ambiguity.

Suggested input shape:

```json
{
  "dialog": "required for v2 unless later cross-dialog search is intentionally added",
  "query": "required",
  "topic": "optional",
  "participant": "optional",
  "limit": 20,
  "continuation": "optional opaque token"
}
```

Internally, `continuation` can still encode Telethon `add_offset`; the model should not have to know that.

### 4. `telegram_get_contact`

Purpose: user profile plus relationship context.

Why: keep a dedicated people/relationship tool, but rename it to a task-oriented, namespaced surface.

### 5. `telegram_get_self`

Purpose: current-account identity.  
Why: small, stable support tool; useful, but not on the hot path.

### Operator-only

`GetUsageStats` should move out of the default public surface. It is valuable for operators and future eval work, but it is not a primary user task and it adds avoidable selection noise in the normal assistant loop.

## Schema Design Guidance

### Use explicit MCP metadata

For every public read-only tool:

- set a stable programmatic `name`
- add a human-readable `title`
- add `annotations.readOnlyHint = true`
- add `annotations.openWorldHint = true` because Telegram is an external, changing system
- do not rely on annotations as security controls

### Prefer strict input schemas

Use JSON Schema 2020-12 semantics and make input objects tight:

- `additionalProperties: false`
- explicit enums for mode/kind/action fields
- explicit required fields
- consistent parameter names across tools

Brownfield implication: the current Pydantic models can keep generating schemas, but the public base model should likely move to `extra="forbid"` and use explicit field descriptions.

### Unify pagination

Public tools should expose one concept: `continuation`.

Internally that token may encode:

- current message cursor
- search offset
- dialog/topic resolution state
- requested mode

The model should not need to reason about whether Telethon uses `max_id`, `min_id`, or `add_offset`.

### Return structured states, not just text blobs

Recommended top-level response envelope for public tools:

```json
{
  "status": "ok | ambiguous | not_found | unavailable | error",
  "summary": "short human-readable text",
  "resolved": {...},
  "items": [...],
  "continuation": "optional opaque token",
  "candidates": [...],
  "warnings": [...]
}
```

Then also return a concise text rendering for backward compatibility and human inspection.

### Make ambiguity machine-readable

Current tools often emit actionable text, which is good, but still forces the model to parse prose. Move ambiguity into structured fields:

- `status: "ambiguous"`
- `candidates: [{display_name, kind, username, confidence}]`

Do not make numeric IDs the primary retry path. If internal identifiers must remain available, include them as opaque references or internal fields, not as the main user-facing contract.

### Error handling split

Use protocol errors only for:

- unknown tool
- malformed request shape
- internal server failures the model cannot fix

Use tool results with `isError: true` or `status: "error"` for:

- dialog not found
- topic unavailable
- ambiguous sender/topic/dialog
- invalid continuation token

Those are recoverable interaction states, not transport failures.

## Migration Options

## Option A: Minimal

Keep the current 7 tools, but tighten their MCP contract.

Changes:

- improve descriptions to Anthropic-quality tool descriptions
- add `title`, annotations, and output schemas
- return `structuredContent` alongside text
- unify text around shorter summaries and structured retry data
- keep legacy names and signatures mostly intact
- move `GetUsageStats` to an operator profile if possible

Pros:

- lowest implementation risk
- minimal downstream breakage
- fast to ship

Cons:

- topic discovery and disambiguation choreography still leaks
- `ListMessages` and `SearchMessages` still look too much like adapted APIs
- public/internal boundary remains only partially improved unless registration changes too

Best if:

- the next milestone budget is very small
- maintaining near-perfect backward compatibility is more important than improving agent ergonomics

## Option B: Medium

Add a namespaced macro-tool surface and keep legacy tools temporarily in compatibility mode.

Changes:

- explicit registry replaces "all `ToolArgs` are public"
- new public tools: `telegram_discover`, `telegram_read`, `telegram_search`, `telegram_get_contact`, `telegram_get_self`
- legacy tools remain available in a `legacy` or `mixed` profile for one milestone
- output becomes structured-first
- pagination becomes unified `continuation`
- `ListTopics` becomes compatibility-only because topic discovery is absorbed into `telegram_discover` and `telegram_read`

Pros:

- best balance of UX gain vs rewrite cost
- reuses existing caches/resolver/fetch helpers
- lets clients migrate gradually
- sharpens the public/internal boundary

Cons:

- requires compatibility logic and duplicated adapters for a while
- needs explicit rollout messaging and runtime validation

Best if:

- the follow-up milestone is intended to materially improve agent behavior, not just documentation quality

## Option C: Maximal

Treat v2 as a full surface split: public macro MCP server/profile, operator/debug profile, and optional resource-first expansion.

Changes:

- split public tools from operator tools completely
- add resource links or embedded resources for large result sets
- possibly add prompts or elicitation for guided workflows
- consider multi-profile or multi-server deployment (`public`, `legacy`, `ops`)
- consider on-demand tool discovery only if tool count later grows materially

Pros:

- cleanest long-term architecture
- best separation of user tasks vs operator concerns
- aligns well with future growth beyond the current tool count

Cons:

- highest migration and runtime complexity
- unnecessary if the server remains small

Best if:

- the tool catalog is expected to grow beyond roughly 10 public tools
- operator/debug surfaces are expected to expand
- multiple client environments need different exposure policies

## Recommended Rollout Plan

### Stage 1: Boundary hardening

- add an explicit public registry
- stop auto-exposing every `ToolArgs` subclass
- define exposure profiles: `legacy`, `mixed`, `public`, `ops`

### Stage 2: Contract hardening on existing tools

- improve descriptions
- add titles, annotations, output schemas
- emit structured results plus text
- convert recoverable failures to tool-result errors

### Stage 3: Introduce macro-tools in mixed mode

- ship `telegram_discover`, `telegram_read`, `telegram_search`
- keep legacy tools alongside them
- label legacy tools clearly in descriptions as compatibility tools

### Stage 4: Telemetry-guided cutover

- use privacy-safe telemetry to see which legacy tools are still called
- default new deployments to `public`
- retain `legacy` only as an opt-in compatibility profile for one additional milestone

### Stage 5: Runtime verification

For the future implementation milestone, do not stop at tests:

- rebuild the runtime container
- restart it
- inspect the live tool list/schema
- verify the active runtime exposes the intended public profile and output schemas

That is especially important here because the project has already learned that green tests do not prove the live container is serving the new tool surface.

## Why Medium Is The Pareto Path

The research does **not** support a huge architectural jump right now.

Reasons:

- the current tool count is still small, so advanced tool-search/deferred-loading is premature
- the repo already has strong low-level internals, so the best next step is an adapter-layer redesign, not a storage/runtime rewrite
- the biggest practical defects are in the public contract, not the fetch pipeline

So the highest-return change set is:

- explicit public registry
- 3-5 task-oriented public tools
- structured outputs
- unified continuation model
- legacy compatibility for one milestone

That is large enough to materially improve model behavior, but still small enough to ship without destabilizing the read path.

## Sources

Primary sources used on 2026-03-13:

- MCP Tools specification (draft / 2025-06-18 semantics): https://modelcontextprotocol.io/specification/draft/server/tools
- MCP basic specification, JSON Schema usage (2025-11-25): https://modelcontextprotocol.io/specification/2025-11-25/basic
- MCP changelog (2025-03-26): https://modelcontextprotocol.io/specification/2025-03-26/changelog
- MCP tools concepts / annotations reference: https://modelcontextprotocol.io/legacy/concepts/tools
- Anthropic Claude tool-use docs: https://platform.claude.com/docs/en/agents-and-tools/tool-use/implement-tool-use
- Anthropic Engineering, "Writing effective tools for agents": https://www.anthropic.com/engineering/writing-tools-for-agents
- Anthropic Engineering, "Introducing advanced tool use on the Claude Developer Platform": https://www.anthropic.com/engineering/advanced-tool-use
- Telethon client reference (`iter_messages`, `add_offset`, `reply_to`): https://docs.telethon.dev/en/stable/modules/client.html

Codebase sources used:

- `src/mcp_telegram/server.py`
- `src/mcp_telegram/tools.py`
- `src/mcp_telegram/cache.py`
- `src/mcp_telegram/analytics.py`
- `src/mcp_telegram/resolver.py`
- `.planning/PROJECT.md`
