# Pitfalls Research: v1.2 MCP Surface Research

**Project:** mcp-telegram  
**Milestone:** v1.2 MCP Surface Research  
**Researched:** 2026-03-13  
**Goal:** Capture the main failure modes when refactoring an MCP server's model-facing tool surface for LLM efficiency.  
**Overall Confidence:** HIGH for the failure modes, MEDIUM-HIGH for the redesign recommendation.

---

## Executive Summary

The main trap in an MCP surface refactor is optimizing for tool count instead of model success. Current MCP, Anthropic, and OpenAI guidance all push in the same direction:

- tool contracts should be explicit, stable, and schema-driven
- tools should return actionable failures that the model can recover from
- debugging and evaluation need first-class support
- large or numerous tools are not, by themselves, a reason to collapse everything into a macro-tool

For `mcp-telegram`, the highest-risk mistakes are:

1. Exposing Telegram/Telethon mechanics directly as tools.
2. Over-correcting into one or two macro-tools with giant mode switches.
3. Removing state the model currently uses to recover: ambiguity candidates, resolved names, topic labels, pagination tokens, and topic-access warnings.
4. Leaving the surface text-only and unstable while modern tool ecosystems increasingly assume schema-conformant inputs and, where available, structured outputs.
5. Breaking debuggability by hiding useful failure details behind generic server exceptions.
6. Producing "research" that does not define measurable success criteria for the refactor.

**Recommendation:** target a **medium redesign**, not a minimal cleanup and not a maximal rewrite. Keep the current user-task shape (`list dialogs`, `list topics`, `browse messages`, `search messages`, `inspect account/user`) but make contracts far more explicit and more structured. A maximal redesign only makes sense if we are willing to invest in evals, trace/debug infrastructure, and a staged migration.

---

## Current Repo Facts That Matter

These are not theoretical concerns; they are visible in the current code:

- `src/mcp_telegram/tools.py` derives tool `inputSchema` directly from `ToolArgs` Pydantic models.
- `src/mcp_telegram/tools.py` does **not** publish `outputSchema` or MCP tool annotations today, even though the current MCP spec supports both.
- Most tool results are single `TextContent` blobs with ad hoc conventions like `[resolved: ...]`, `next_cursor: ...`, `next_offset: ...`, and inline ambiguity lists.
- `ListMessages` currently does many jobs at once: dialog resolution, sender resolution, topic resolution, cursor decoding, unread filtering, oldest-first mode, topic boundary protection, message formatting, and pagination.
- `SearchMessages` uses integer `offset`, while `ListMessages` uses opaque `cursor`, so the model already sees inconsistent pagination idioms.
- `ListTopics` and topic-aware message browsing are real requirements, not optional embellishments. The test suite contains explicit topic leakage and topic-inaccessible cases.
- `src/mcp_telegram/server.py` catches exceptions and raises `RuntimeError(f"Tool {name} failed")`, which discards actionable details that MCP clients and models could otherwise use.

This means v1.2 is not starting from a blank slate. The refactor must preserve what currently helps the model recover, while replacing brittle text conventions with more stable contracts.

---

## Redesign Options Used In This Memo

### Minimal redesign

Keep the current tool set and names. Improve descriptions, input schemas, and outputs without changing the basic workflow.

Typical moves:

- keep `ListDialogs`, `ListTopics`, `ListMessages`, `SearchMessages`, `GetMyAccount`, `GetUserInfo`, `GetUsageStats`
- add tool annotations like `readOnlyHint`
- standardize output envelopes and pagination metadata
- improve error reporting and preserve current recovery affordances

### Medium redesign

Keep the current user-task boundaries, but split or reshape overloaded tools where the model burden is too high.

Typical moves:

- preserve the main workflows, but make the surface more explicit and more structured
- unify pagination semantics
- introduce stable handles or refs for dialogs/topics/messages
- keep human-readable summaries, but add structured result objects and actionable warnings

### Maximal redesign

Re-think the entire surface around explicit navigation state, structured outputs, and possibly resources/prompts in addition to tools.

Typical moves:

- redesign around canonical entity refs and structured result objects
- move stable catalogs to resources where appropriate
- split browsing, search, resolution, and inspection into narrower primitives
- require new eval suites and a staged migration plan

---

## Critical Pitfalls

### Pitfall 1: 1:1 API wrapping disguised as "clean architecture"

**What goes wrong**

The MCP surface becomes a shallow wrapper around Telethon or Telegram RPCs:

- one tool per request
- raw request names or parameters leak into the public contract
- backend transport semantics become model-facing design

For `mcp-telegram`, this would look like exposing tools shaped around:

- `GetForumTopicsRequest`
- `GetForumTopicsByIDRequest`
- `GetPeerDialogsRequest`
- `GetMessageReactionsListRequest`
- raw pagination parameters like `offset_id`, `offset_topic`, `min_id`, `max_id`

**Why this is a real risk here**

- Telegram topics are not a clean, stable user abstraction. They involve `reply_to_top_id`, deleted topics, inaccessible topics, and fallback scanning behavior.
- Telethon itself documents that friendly methods should be preferred unless a raw request is needed for missing functionality or extra control. That is a backend implementation concern, not a good public tool boundary.
- Current `ListMessages` already hides substantial Telegram complexity. Re-exposing that complexity would increase model reasoning burden immediately.

**Consequences**

- tool explosion without better outcomes
- model confusion over low-level parameters that only exist because of Telegram internals
- higher schema churn whenever Telethon or Telegram semantics move
- brittle prompts that accidentally encode backend behavior

**Mitigations**

- Define tools by **user task**, not by SDK method or RPC.
- Treat Telethon raw requests as implementation detail unless the user task truly is that primitive.
- Use explicit descriptions and examples for topic workflows instead of surfacing raw thread mechanics.

**Option mapping**

- **Minimal:** keep current task-shaped tools; do not add RPC-shaped tools for topics, reactions, or dialog retrieval.
- **Medium:** split only where the user task boundary is clear, such as topic discovery versus message browsing, not by underlying request type.
- **Maximal:** if moving to narrower tools, name them around user intent (`resolve_dialog`, `browse_messages`, `search_messages`, `list_topics`), never around Telethon/TL types.

---

### Pitfall 2: Over-aggregation into a mega-tool

**What goes wrong**

The refactor responds to tool proliferation by collapsing the surface into one giant tool such as:

- `TelegramQuery`
- `TelegramNavigate`
- `TelegramRead`

with modes like `dialogs`, `topics`, `messages`, `search`, `account`, `user_info`, plus many optional parameters.

**Why this is a real risk here**

`mcp-telegram` already has one overloaded tool: `ListMessages`. Making the whole server look like that would multiply the current complexity:

- dialog resolution
- sender filtering
- topic filtering
- unread filtering
- oldest-first mode
- pagination
- ambiguity recovery
- topic inaccessibility edge cases

That creates invalid parameter combinations and makes the schema harder for the model to reason about.

**Relevant external guidance**

- Anthropic explicitly recommends detailed tool descriptions and JSON schemas, with optional input examples.
- OpenAI’s current function-calling guidance says that if an application has many functions or large schemas, tool search can defer rarely used tools. That is a sign not to over-collapse tools just to reduce count.

**Consequences**

- giant schemas with weak semantic boundaries
- hidden internal branching that is hard to test
- worse debuggability because the same tool name can fail in many unrelated ways
- harder migration because every surface change becomes high blast radius

**Mitigations**

- Prefer a moderate number of well-scoped tools over one mode-heavy macro-tool.
- Keep each tool’s success conditions and retry behavior obvious.
- Where one tool must remain broad, constrain its modes and publish explicit output guarantees per mode.

**Option mapping**

- **Minimal:** keep current tools, but do not introduce a new umbrella tool.
- **Medium:** split `ListMessages` only if the split reduces invalid argument combinations and clarifies recovery paths.
- **Maximal:** use more tools if needed, but pair that with strong schemas, annotations, and possibly tool search rather than a mega-tool.

---

### Pitfall 3: Hiding too much state

**What goes wrong**

The redesign strips out "incidental" details in the name of simplicity, but those details are exactly what let the model recover and continue:

- resolved-name echoes
- ambiguity candidate lists
- topic labels in cross-topic pages
- pagination continuation tokens
- explicit topic-access or topic-deleted warnings
- whether the dialog or topic was matched fuzzily

**Why this is a real risk here**

Current tools already rely on these cues:

- `[resolved: "..."]` helps the model notice fuzzy resolution and decide whether to continue.
- ambiguity lists let the model retry with a numeric ID.
- topic labels help prevent silent cross-topic confusion.
- `next_cursor` / `next_offset` enable long reads.
- topic-inaccessible tests prove that Telegram state is not always cleanly fetchable.

If a v1.2 refactor removes these signals because they look noisy, the model will lose the ability to self-correct.

**Telegram-specific failure examples**

- A forum dialog contains multiple similar topics, but the refactor returns only a "success" page of messages without exposing which topic was actually used.
- A deleted or inaccessible topic silently falls back to dialog-level messages, mixing unrelated threads.
- A fuzzy dialog match is not surfaced, so the model continues browsing the wrong chat.

**Consequences**

- more wrong-tool retries
- silent data mistakes instead of explicit recoverable failures
- regression in long-running browse flows
- harder human review because the surface hides the causal path

**Mitigations**

- Keep recovery-critical state explicit.
- Distinguish between:
  - **navigation state** the model needs to continue
  - **debug state** the operator may need occasionally
  - **backend state** the model does not need
- Add structured fields for resolution, warnings, and pagination instead of depending on free-text footers only.

**Option mapping**

- **Minimal:** preserve existing resolved/ambiguous/pagination cues, but regularize them into a stable envelope.
- **Medium:** expose canonical refs and warning objects so the model can continue without scraping prose.
- **Maximal:** keep recovery state explicit in structured outputs; move deep implementation detail behind optional debug views, not the default response.

---

### Pitfall 4: Hiding too little state

**What goes wrong**

The refactor goes the other way and exposes too much raw Telegram or Telethon machinery:

- negative peer IDs and raw entity IDs everywhere
- transport-shaped cursors and offsets as first-class reasoning burden
- TL request vocabulary in user-facing descriptions
- internal cache or fallback state leaking into normal flows
- too many optional flags that encode backend execution strategy instead of user intent

**Why this is a real risk here**

The current repo already has legitimate complexity:

- dialog IDs, user IDs, topic IDs
- cursor tokens tied to dialog/message identity
- topic metadata including deletion/inaccessible states
- raw fallback scanning when thread fetch fails

Some of that must remain available. Most of it should not become the model’s default problem.

**Telegram-specific failure examples**

- The model is asked to choose between `topic_id`, `reply_to_top_id`, `offset_topic`, and `top_message_id` even though the actual user task is just "continue reading Release Notes".
- A model sees raw inaccessible-topic timestamps or cache TTL artifacts and anchors on irrelevant details.

**Consequences**

- context pollution
- higher reasoning cost per tool call
- lower tool-selection accuracy
- more prompt coupling to implementation quirks

**Mitigations**

- Define one canonical identifier per surfaced concept:
  - dialog
  - topic
  - message page / continuation
- Keep raw IDs available only where they improve recovery or disambiguation.
- Separate normal output from optional debug output.

**Option mapping**

- **Minimal:** keep numeric IDs only where they already help disambiguation; avoid expanding raw parameter surface.
- **Medium:** introduce stable refs/handles so the model can continue navigation without repeatedly re-resolving names or juggling multiple raw IDs.
- **Maximal:** formalize a split between normal model output and operator/debug views.

---

### Pitfall 5: Losing debuggability and self-correction

**What goes wrong**

The redesign focuses on elegance and forgets that MCP servers are operational systems:

- failures become generic
- traces disappear
- humans cannot reproduce what the model saw
- clients lose actionable tool errors

**Why this is a real risk here**

This repo already has a debuggability gap:

- `server.py` currently converts exceptions into `RuntimeError("Tool X failed")`
- tools return mostly text blobs rather than structured results
- there is no MCP logging capability exposed today
- runtime freshness matters because this project is commonly exercised through a long-lived container

Modern MCP guidance goes in the other direction:

- MCP has an Inspector specifically for testing and debugging servers.
- MCP transport guidance requires stdout to stay clean for protocol traffic and allows logging on stderr.
- MCP logging exists as a structured protocol capability.
- Current MCP tool guidance distinguishes protocol errors from tool execution errors and says tool execution errors should contain actionable feedback the model can use to self-correct.
- OpenAI’s trace grading guidance explicitly argues for inspecting end-to-end traces of decisions and tool calls to identify workflow-level failures.

**Telegram-specific failure examples**

- Topic fetch fails with `TOPIC_ID_INVALID`, but the model only sees "Tool ListMessages failed".
- A generic search failure hides whether the issue was dialog resolution, Telegram RPC failure, or schema validation.
- A stale container serves an old tool schema and the operator cannot easily verify what surface is live.

**Consequences**

- lower retry success
- slower operator diagnosis
- false confidence during rollout
- poor live/runtime verification

**Mitigations**

- Preserve actionable tool execution errors; do not flatten them all into generic server exceptions.
- Add structured logging or at minimum consistent stderr/server logs that correlate call, resolution path, warnings, and failure class.
- Verify the live container after surface changes, not just tests.
- Use MCP Inspector against the actual runtime to confirm exposed schemas and outputs.

**Option mapping**

- **Minimal:** stop discarding actionable failure detail; improve logs and runtime verification.
- **Medium:** add structured warning/error objects to tool outputs and a stable debug section for resolution path and retry hints.
- **Maximal:** add first-class structured tracing/logging and treat every redesign step as trace/eval-backed, not just unit-test-backed.

---

### Pitfall 6: Unstable schemas, pagination, and output contracts

**What goes wrong**

The refactor changes names, nullability, pagination styles, and output layouts opportunistically:

- tool names change without a migration story
- equivalent tools paginate differently
- required/optional fields drift between versions
- the model has to parse prose for key state
- clients disagree on JSON Schema interpretation

**Why this is a real risk here**

Current repo facts:

- `inputSchema` is generated from Pydantic and then sanitized.
- `outputSchema` is absent today.
- `ListMessages` uses `cursor`; `SearchMessages` uses `offset`.
- result metadata currently lives in prose footers and prefixes, not stable fields.

External changes matter too:

- MCP now supports both `inputSchema` and optional `outputSchema`.
- MCP community standardization has moved toward JSON Schema 2020-12 as the default dialect for embedded schemas.
- Anthropic strict tool use and OpenAI structured outputs both reward stable schemas and punish drift.

**Telegram-specific failure examples**

- A future refactor replaces `next_cursor` prose with `page.next.cursor`, but only for some tools, breaking learned retry behavior.
- A new topic-aware browse tool returns one shape for normal pages and another for inaccessible/deleted-topic cases.
- A migration changes `dialog` to `dialog_ref`, but fuzzy-name flows remain text-driven and half-migrated.

**Consequences**

- models learn the wrong contract
- schema validation failures rise
- mixed-version clients behave inconsistently
- rollout risk increases sharply

**Mitigations**

- Treat tool names, pagination, and warning/error fields as versioned public contract.
- Publish `outputSchema` where feasible.
- Pick one pagination idiom for navigational reads.
- Explicitly declare or at least consistently target the current JSON Schema dialect assumptions.
- Add tool descriptions/examples that show ambiguous dialog, topic, and pagination flows.

**Option mapping**

- **Minimal:** preserve existing tool names; unify pagination semantics only when it can be done with a compatibility layer.
- **Medium:** define a standard result envelope across browse/search tools: `items`, `page`, `resolved`, `warnings`, `summary`.
- **Maximal:** version the model-facing contract explicitly and migrate old/new surfaces in parallel during rollout.

---

### Pitfall 7: Non-actionable research

**What goes wrong**

The milestone produces tasteful principles like "make tools smaller" or "make outputs more structured" without a decision rubric, without evals, and without a recommended migration path.

**Why this is a real risk here**

This repo is already at the stage where vague guidance is not useful. We already know the surface has:

- overloaded browse behavior
- Telegram topic edge cases
- schema/output inconsistencies
- runtime verification concerns

v1.2 research is only useful if it narrows choices and defines what "better" means.

Anthropic and OpenAI both explicitly recommend defining measurable success criteria and using evals continuously. OpenAI also recommends logging and trace-based inspection for agent workflows. That applies here directly.

**Consequences**

- endless bikeshedding between minimal and maximal redesign ideas
- refactors optimized for aesthetics, not model success
- no way to detect regressions in tool selection, retries, or topic correctness

**Mitigations**

- End the research with a clear recommendation and anti-goals.
- Define a concrete eval set before changing the public surface.
- Tie every redesign option to specific measurable outcomes.

**Option mapping**

- **Minimal:** require a short acceptance checklist before shipping.
- **Medium:** require a targeted eval suite and runtime verification gates.
- **Maximal:** require staged rollout, trace inspection, comparative eval runs, and explicit migration windows.

---

## Required Eval Criteria For v1.2

If this milestone is to be actionable, the surface redesign should be evaluated against concrete criteria. Suggested minimum set:

### Tool-selection and completion metrics

- percent of tasks completed without human tool nudging
- mean tool calls per successful task
- mean retries after ambiguous resolution
- percent of successful long reads across 3+ pages

### Telegram-specific correctness metrics

- percent of topic reads with no adjacent-topic leakage
- percent of inaccessible/deleted-topic cases surfaced as explicit warnings, not silent fallbacks
- percent of fuzzy dialog matches surfaced explicitly
- percent of successful continuation after a first-page browse response

### Contract stability metrics

- schema validation failure rate
- tool-call argument error rate
- pagination continuation success rate
- output parsing success for clients consuming structured fields

### Debuggability metrics

- percent of failures with actionable retry text or structured warning
- time-to-diagnose for seeded failures using logs/Inspector
- live-runtime schema verification after deployment

---

## Recommendation

### Recommended redesign level: Medium

The best default for v1.2 is a **medium redesign**:

- keep task-shaped tools rather than wrapping Telegram requests 1:1
- do not collapse the surface into one macro-tool
- preserve recovery-critical state, but regularize it into structured fields
- add MCP annotations and, where practical, `outputSchema`
- unify pagination and result envelopes across browse/search flows
- stop flattening all server-side failures into generic runtime errors

### What "medium" should mean concretely for this repo

- `ListMessages` and `SearchMessages` should converge on a shared result envelope even if they remain separate tools.
- Dialog/topic resolution should be visible in structured form, not just prose prefixes.
- Topic warnings (`deleted`, `inaccessible`, fallback used) should be explicit.
- Read-only annotations should be set on all public tools.
- Live runtime verification should be mandatory after schema-affecting changes.

### Anti-goals

Do **not** treat these as success:

- fewer tools at any cost
- more raw Telegram power in the public surface
- prettier output that removes retry or debug affordances
- a rewrite without evals

---

## Sources

Primary sources used for current web research on 2026-03-13:

- Model Context Protocol tools specification: https://modelcontextprotocol.io/specification/2025-11-25/server/tools
- Model Context Protocol transport guidance: https://modelcontextprotocol.io/docs/concepts/transports
- Model Context Protocol debugging guide: https://modelcontextprotocol.io/docs/tools/debugging
- Model Context Protocol Inspector guide: https://modelcontextprotocol.io/docs/tools/inspector
- MCP JSON Schema dialect standardization (SEP-1613): https://modelcontextprotocol.io/community/seps/1613-establish-json-schema-2020-12-as-default-dialect-f
- Anthropic tool-use implementation guide: https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use
- Anthropic tool-use overview and strict tool use: https://docs.anthropic.com/en/docs/tool-use
- Anthropic eval guidance: https://docs.anthropic.com/en/docs/test-and-evaluate/define-success
- OpenAI function calling guide: https://platform.openai.com/docs/guides/function-calling
- OpenAI structured outputs guide: https://platform.openai.com/docs/guides/structured-outputs
- OpenAI evaluation best practices: https://platform.openai.com/docs/guides/evaluation-best-practices
- OpenAI working with evals: https://platform.openai.com/docs/guides/evals
- OpenAI trace grading: https://platform.openai.com/docs/guides/trace-grading
- OpenAI agent/tool platform announcement for observability emphasis: https://openai.com/index/new-tools-for-building-agents/
- Telethon client reference: https://docs.telethon.dev/en/stable/modules/client.html
- Telethon full API guidance: https://docs.telethon.dev/en/stable/concepts/full-api.html

