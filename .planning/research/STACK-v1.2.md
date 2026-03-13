# Stack Research: v1.2 MCP Surface Research

**Project:** `mcp-telegram`  
**Milestone:** `v1.2 MCP Surface Research`  
**Researched:** 2026-03-13  
**Confidence:** HIGH

---

## Executive Summary

For redesigning an LLM-facing MCP surface, the authoritative research stack should be:

1. **Official MCP specification and official MCP docs/blog** for the protocol contract and MCP-specific server-design features.
2. **Official Anthropic docs and engineering guidance** for how Claude actually interprets tool definitions and why some tool surfaces perform better than others.
3. **Community interpretations** only as secondary synthesis, examples, and implementation patterns.

This matters because the Telegram server is not designing a generic API wrapper. It is designing a **tool surface for an LLM client**. The highest-value sources are therefore the ones that define:

- what MCP tools can express,
- what MCP clients can consume,
- how Claude turns tool definitions into prompt context,
- and how to shape tools around agent workflows rather than raw backend endpoints.

The five sources already identified by the user are all worth keeping, but they are **not equally authoritative**:

- Official: `anthropic.com`, `docs.anthropic.com` / `platform.claude.com`
- Community / secondary: `modelcontextprotocol.info`, `scalekit.com`, third-party GitHub ADRs

The main missing primary sources were:

- official MCP `tools` specification,
- official MCP `lifecycle` specification,
- official MCP guidance on `server instructions`,
- official MCP docs clarifying tool annotations,
- official Anthropic MCP connector docs.

---

## Recommended Source Stack

### Tier 0: Normative MCP Sources

These should be treated as the protocol source of truth.

| Priority | Source | Authority | Why It Matters For v1.2 |
|---|---|---|---|
| P0 | https://modelcontextprotocol.io/specification/2025-06-18/server/tools | Official MCP specification | Canonical contract for tool shape: `name`, `title`, `description`, `inputSchema`, `outputSchema`, `annotations`, `structuredContent`, `isError`, and security expectations. This is the baseline for any tool-surface redesign. |
| P0 | https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle | Official MCP specification | Canonical location for server `instructions` during initialization. Important because some cross-tool guidance should live at the server level rather than being duplicated in every tool description. |
| P1 | https://modelcontextprotocol.io/legacy/concepts/tools | Official MCP docs (older but still official and more explanatory) | Best official explanation of **tool annotations** like `readOnlyHint`, `destructiveHint`, `idempotentHint`, and `openWorldHint`. For a read-only Telegram server, this materially affects client presentation and approval behavior. |

### Tier 1: Official Anthropic Behavior Guidance

These should be treated as the source of truth for Claude-facing tool design.

| Priority | Source | Authority | Why It Matters For v1.2 |
|---|---|---|---|
| P0 | https://anthropic.mintlify.app/en/docs/agents-and-tools/tool-use/implement-tool-use | Official Anthropic docs (canonical currently redirects to `platform.claude.com`) | Most important Claude-specific source. Anthropic explicitly says detailed tool descriptions are the biggest lever in tool performance and explains that tool definitions are injected into a special system prompt. This directly governs description/schema rewriting. |
| P0 | https://www.anthropic.com/research/building-effective-agents | Official Anthropic engineering guidance (canonical currently redirects to `/engineering/...`) | High-level source for agent-computer interface design. It frames tool definitions as prompt engineering and argues for simplicity, transparency, evaluation, and deliberate interface design over abstraction-heavy wrappers. |
| P1 | https://docs.anthropic.com/en/docs/agents-and-tools/mcp-connector | Official Anthropic docs | Important product constraint: Anthropic’s MCP connector currently supports **tool calls only**. For Anthropic-facing usage, that increases the importance of the tool surface relative to MCP resources/prompts. |

### Tier 1.5: Official MCP Practical Guidance

These are not normative spec pages, but they are still official MCP guidance and materially useful.

| Priority | Source | Authority | Why It Matters For v1.2 |
|---|---|---|---|
| P1 | https://blog.modelcontextprotocol.io/posts/2025-11-03-using-server-instructions/ | Official MCP blog | Best official practical guidance on what belongs in server instructions: cross-tool relationships, operational patterns, constraints, and concise model-agnostic guidance. This is directly relevant to Telegram workflow hints such as discovery-before-read or search-before-browse behavior. |

### Tier 2: Community / Secondary Sources

Keep these, but treat them as interpretation or implementation examples, not source of truth.

| Priority | Source | Authority | Why It Still Matters |
|---|---|---|---|
| P2 | https://modelcontextprotocol.info/docs/tutorials/writing-effective-tools/ | Community / mirror / secondary | Strong synthesis of effective-tool advice for agents, especially around avoiding one-tool-per-endpoint design, returning high-signal context, prompt-engineering descriptions, and iterative evaluation. Useful, but not official. |
| P2 | https://www.scalekit.com/blog/wrap-mcp-around-existing-api | Community blog | Useful framing for deciding whether the Telegram MCP surface should be direct endpoint translation or capability aggregation. Good architecture vocabulary, but not protocol authority. |
| P2 | https://github.com/vishnu2kmohan/mcp-server-langgraph/blob/main/adr/adr-0023-anthropic-tool-design-best-practices.md | Third-party implementation ADR | Helpful as a concrete migration example showing namespacing, search-focused tools, response shaping, and observability. Good for pattern borrowing, not for deciding what is canon. |

---

## Why Each Source Stays In The Stack

### 1. MCP specification: tools

This is the non-negotiable base document for the milestone. It defines the contract the Telegram server can actually expose. The important v1.2 implications are:

- `outputSchema` and `structuredContent` are first-class, so response redesign should consider machine-readable outputs instead of text-only blobs.
- `annotations` are part of the tool definition, so behavior hints belong in protocol metadata, not only prose.
- security guidance is part of the tool contract, which matters even for a read-only server.

### 2. MCP specification: lifecycle

This source matters because it is the protocol basis for **server instructions**. If the redesign needs server-level guidance like:

- when to search instead of browse,
- how ambiguity is resolved,
- or what cross-tool workflow is preferred,

that belongs here conceptually, not as repeated per-tool text.

### 3. Official MCP docs: tool annotations

This source matters because the current Telegram server is read-only, and annotations are the cleanest MCP-native place to express that. The practical implication is that current tools likely want `readOnlyHint: true`; for Telegram, `openWorldHint: true` is also a likely fit because the server reads from an external, evolving system. That last point is an inference from the official annotation meanings, not an explicit MCP prescription.

### 4. Anthropic: implement tool use

This is the single most important source for the **LLM-facing** part of the redesign. It establishes that:

- tool definitions are injected into Claude’s tool-use system prompt,
- description quality is the biggest driver of tool performance,
- parameter semantics and caveats must be explicit,
- and parallel tool behavior is real and should be considered in surface design.

For `mcp-telegram`, this means tool descriptions are not “API docs”; they are prompt material.

### 5. Anthropic: building effective agents

This source matters because it reframes the problem correctly: the redesign is an **agent-computer interface** problem, not merely an MCP compliance problem. It reinforces:

- simplify before adding complexity,
- test with evaluation rather than intuition,
- and invest real design effort in tool formats, names, parameters, and failure modes.

### 6. Anthropic: MCP connector

This source matters because it constrains how Anthropic products consume MCP today. The current connector supports only **tool calls**, not the full MCP feature set. That means a Telegram MCP server meant to work well with Anthropic products cannot rely on prompts/resources to rescue a weak tool surface. The tools themselves must carry the load.

### 7. Official MCP blog: server instructions

This fills an important gap between the spec and tool descriptions. It shows where to put:

- cross-feature relationships,
- workflow ordering hints,
- rate/size constraints,
- and concise operational guidance.

It also makes clear what **not** to do:

- do not duplicate tool descriptions,
- do not write a long manual,
- do not use instructions to change model personality,
- and do not rely on instructions as a substitute for sound tool design.

### 8. `modelcontextprotocol.info` tutorial

This source remains valuable because it collects several practical ideas in one place:

- avoid one-tool-per-endpoint design,
- prefer a few workflow-oriented tools,
- return only high-signal context,
- use explicit parameter naming,
- and iterate with evaluations.

However, its own About page describes it as “a comprehensive resource,” not the official MCP documentation site. Use it as synthesis, not canon.

### 9. Scalekit wrapper article

This source matters because the Telegram server is exactly the kind of system that can accidentally become a thin wrapper around backend endpoints and paging mechanics. Its most useful contribution is the vocabulary of:

- direct translation,
- capability aggregation,
- context-aware wrapping,
- hybrid designs.

The capability-aggregation lens is especially useful for deciding whether current tools should remain close to Telethon operations or collapse into higher-level agent tasks.

### 10. Third-party ADR

This source matters because it shows how another MCP server translated Anthropic guidance into concrete refactors:

- replacing generic tool names,
- preferring search-focused tools over list-all tools,
- adding response-shaping controls,
- and tracking adoption/quality metrics.

This is useful implementation inspiration, but it is still downstream interpretation of primary guidance.

---

## Research Conclusions For v1.2

The source stack above supports the following design direction:

1. **Design for workflows, not Telethon endpoints.**  
   The strongest official and secondary guidance all point away from one-tool-per-endpoint surfaces.

2. **Treat descriptions as prompt engineering.**  
   Tool descriptions, parameter descriptions, caveats, and examples are part of the model-facing interface, not documentation garnish.

3. **Use MCP-native metadata.**  
   Add annotations and structured outputs where they reduce ambiguity or improve client behavior.

4. **Use server instructions sparingly but intentionally.**  
   Put cross-tool workflow rules there, not redundant tool summaries.

5. **Optimize for context efficiency.**  
   Prefer search, filtering, and high-signal summaries over raw lists, opaque IDs, and low-level pagination burdens.

6. **Assume Anthropic clients may only see tools.**  
   For Anthropic product compatibility, the tool layer must stand on its own.

7. **Evaluate instead of guessing.**  
   The redesign should be measured with real tool-use tasks, not only by schema elegance.

---

## Recommended Reading Order For Implementation

1. `modelcontextprotocol.io/specification/2025-06-18/server/tools`
2. `modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle`
3. `docs.anthropic.com/.../tool-use/implement-tool-use`
4. `anthropic.com/research/building-effective-agents`
5. `blog.modelcontextprotocol.io/posts/2025-11-03-using-server-instructions/`
6. `docs.anthropic.com/.../mcp-connector`
7. `modelcontextprotocol.io/legacy/concepts/tools`
8. `modelcontextprotocol.info/docs/tutorials/writing-effective-tools/`
9. `scalekit.com/blog/wrap-mcp-around-existing-api`
10. third-party ADR

This order keeps the milestone anchored in protocol truth first, Claude behavior second, and community interpretation last.

---

## Final Decision

For milestone `v1.2`, the **authoritative research base** is:

- **Primary / official MCP:** spec `tools`, spec `lifecycle`, official docs on tool annotations, official MCP blog on server instructions
- **Primary / official Anthropic:** tool-use implementation docs, effective-agents engineering guidance, MCP connector docs
- **Secondary / community:** the three user-supplied non-official sources

That is the stack to use when redesigning the Telegram MCP surface around capability-oriented, LLM-usable tools rather than backend-shaped wrappers.
