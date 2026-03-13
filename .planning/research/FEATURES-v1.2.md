# Feature Landscape: v1.2 (MCP Surface Research)

**Domain:** effective LLM-facing MCP tool design for messaging products
**Researched:** 2026-03-13

## Research Summary

Current primary-source guidance converges on the same core pattern: MCP tools should not be thin wrappers over backend APIs. The strongest surfaces expose a small set of task-shaped capabilities, use metadata and schemas to make routing obvious, return compact structured outputs, and hide low-level protocol details unless they are strictly needed for follow-up calls.

For a messaging product like Telegram, that means optimizing for jobs like "catch me up on this chat", "find the messages about X", and "show the thread/topic context", not for raw transport operations like peer resolution, cursor math, or MTProto-specific identifiers.

## Table Stakes

| Pattern | Why it matters | Telegram translation | Source basis |
|---------|----------------|----------------------|--------------|
| **Task-shaped tools** | Models route better when each tool maps to one clear job instead of a backend endpoint. | Keep `ListMessages` and `SearchMessages` user-legible. Do not expose `resolve_peer`, `get_history_page`, `get_forum_topics`, and `fetch_replies` as separate public tools unless the user job actually needs that split. | OpenAI "Define tools"; OpenAI "What makes a great ChatGPT app" |
| **Explicit input schemas and predictable structured outputs** | Clear schemas reduce malformed calls. Stable structured outputs let the model chain follow-up actions reliably. | Accept fields like `dialog`, `topic`, `query`, `since`, `limit`; return structured message objects plus stable reusable refs. | MCP tools spec; OpenAI "Define tools" |
| **Metadata-quality is routing-quality** | Tool names, descriptions, and parameter docs heavily influence whether the model calls the right tool. | Tool descriptions should say when to use the tool, when not to use it, and what kind of result comes back. Parameters should describe ambiguity handling, defaults, and limits. | OpenAI "Optimize Metadata"; Anthropic tool-use docs |
| **High-signal, token-efficient responses** | Bloated outputs increase cost and error rate. Compact results preserve model working memory for reasoning. | Default to concise message snippets, sender/display name, timestamp, and a stable message ref. Offer pagination, filtering, truncation, or a `response_format`/verbosity escape hatch for deeper inspection. | Anthropic engineering guidance; MCP tools spec |
| **Use the right MCP primitive** | Not every capability should be a tool. Resources and prompts can reduce tool chatter and cognitive load. | Expose account/profile, recent dialogs, or dialog/topic catalogs as resources; expose reusable workflows like "catch up on this dialog" as prompts when user-triggered scaffolding helps. | MCP resources spec; MCP prompts spec |
| **Behavior annotations and safety boundaries** | Clients need clear hints for approval and presentation, especially in mixed read/write ecosystems. | Mark read-only Telegram tools with `readOnlyHint`; keep open-world/destructive hints honest if write tools ever arrive later. | MCP tool annotations; OpenAI "Define tools" |

## Differentiators

| Pattern | Why it differentiates | Telegram translation | Source basis |
|---------|-----------------------|----------------------|--------------|
| **Capability aggregation around real jobs** | The best tools collapse common multi-step workflows into one call, reducing tool-selection ambiguity and intermediate context waste. | Prefer `GetConversationContext` over a chain of `resolve dialog -> resolve topic -> fetch messages -> fetch participants`. Aggregate under the hood if the user intent is singular. | Anthropic engineering guidance; OpenAI app design guidance |
| **Context-aware wrapping** | Good chat-native tools use existing conversational context and ask only the smallest number of follow-up questions needed. | If the user says "same chat, last week, what did Alice decide?", the tool surface should allow dialog/topic reuse and time-window filtering without forcing the model to rebuild state manually. | OpenAI "What makes a great ChatGPT app" |
| **Semantic handles instead of backend leakage** | Models reason better over natural or semantically meaningful references than opaque identifiers. | Return `dialog_ref`, `topic_ref`, `message_ref`, and human labels; keep `access_hash`, raw peer structs, and MTProto pagination state internal unless a detailed mode truly requires them. | Anthropic engineering guidance; OpenAI app design guidance |
| **Progressive disclosure** | A concise-by-default surface lowers token cost, but optional detail preserves power for debugging and follow-up actions. | Support `response_format: "concise" | "detailed"` or equivalent on heavy tools so the model can fetch IDs, rawer metadata, or larger windows only when it actually needs them. | Anthropic engineering guidance |
| **Prompt-set evaluation for tool boundaries** | Strong connectors tune surface area with direct, indirect, and negative prompts rather than intuition alone. | Maintain eval prompts like "catch me up on #support", "find the message where Bob approved the launch", and negative prompts where built-in chat reasoning should answer without Telegram calls. | OpenAI "Optimize Metadata" |
| **Resource-backed context packaging** | Returning resource links or embedding resources gives clients structured context without forcing every follow-up through repeated list calls. | A search result can return a resource for the full thread/topic context or recent dialog state, while the main tool result stays concise. | MCP tools spec; MCP resources spec |

## Anti-Patterns

| Anti-pattern | Why it hurts | Telegram example |
|--------------|--------------|------------------|
| **Low-level API leakage** | Raw protocol fields increase cognitive load and make tool use brittle. | Exposing `peer`, `access_hash`, `offset_id`, `add_offset`, `min_id`, `max_id`, `reply_to_top_id`, or `hash` as first-class user-facing parameters. |
| **Overlapping micro-tools** | Too many near-duplicate tools make routing noisy and error-prone. | `ResolveDialog`, `ResolveTopic`, `GetTopicMessages`, `GetReplies`, `GetMessagesByIds`, `GetRecentMessages`, `SearchInDialog`, all partially covering "find the relevant messages". |
| **Kitchen-sink backend mirrors** | A mega-tool with unrelated verbs is just as confusing as too many tiny tools. | A single `telegram_api(action=..., params=...)` tool or a generic `messages(action="list|search|get_by_id|resolve")`. |
| **Porting the whole product into chat** | Conversation UX wants a compact toolkit, not a full navigation hierarchy. | Recreating every Telegram screen and transport operation instead of exposing the few things the model can use well in chat. |
| **Blob parameters** | Ambiguous freeform inputs make validation weak and routing worse. | `context_blob`, `extra_filters_json`, or "paste the whole conversation" style parameters instead of named fields like `dialog`, `query`, `sender`, `since`. |
| **Raw payload dumps** | Full backend JSON wastes tokens and hides the fields that matter. | Returning entire Telethon message/entity objects, transport metadata, and cache state when the model only needs sender, time, text snippet, refs, and a few flags. |
| **Thin descriptions** | The model cannot infer safe usage boundaries from terse docs. | Descriptions like "List Telegram messages" or "Search messages" with no usage guidance, ambiguity rules, or distinction between tools. |
| **Excess clarification before value** | Users drop off if the tool behaves like onboarding instead of conversation. | Asking five setup questions before returning any messages, even when the dialog/topic/timeframe is already inferable from the prompt or prior context. |

## Key Inference: Aggregate Jobs, Not Arbitrary Verbs

**Inference from sources:** OpenAI recommends one clear job per tool, while Anthropic also recommends consolidating related operations into fewer tools. These are not contradictory if the unit of design is a **user job**, not a backend method.

Good aggregation:

- A single tool hides dialog resolution, topic lookup, and message retrieval because the user asked one thing: "catch me up on the support topic".
- A single tool returns a concise result plus stable refs the model can reuse later.

Bad aggregation:

- One tool multiplexes unrelated verbs or mirrors the entire SDK behind an `action` parameter.
- One tool tries to be both navigation, search, analytics, and administration at once.

The practical target is a **small set of high-coverage, task-shaped tools** with internal orchestration.

## Messaging-Specific Capability Patterns

| Capability pattern | Good MCP surface | What it hides |
|--------------------|------------------|---------------|
| **Catch up on a dialog** | `GetConversationContext(dialog, topic?, since?, limit?, response_format?)` | Dialog resolution, topic/thread selection, default time windows, pagination internals |
| **Find evidence across chats or within a chat** | `SearchMessages(query, dialog?, sender?, since?, until?, response_format?)` | Backend search syntax, cursor math, entity resolution, raw offsets |
| **Inspect a topic or reply chain** | `GetThreadContext(dialog, thread_ref, around_message_ref?, limit?, response_format?)` | Forum-topic API quirks, reply traversal rules, topic ID lookup |
| **Discover reusable context** | Resources such as `telegram://account/me`, `telegram://dialogs/recent`, `telegram://dialogs/{dialog_ref}/topics` | Repeated list calls for static or slowly changing context |
| **User-triggered investigation workflows** | Prompts such as `catch_up_dialog`, `investigate_incident_thread`, `summarize_topic_decisions` | Repeating long operator instructions in every tool description |

## Example Description Shape

For a complex messaging tool, the description should be closer to this:

> Use this when the user wants to catch up on a Telegram dialog or topic, inspect recent context, or understand what happened within a bounded time range. Accepts either human-readable dialog/topic names or previously returned refs, and resolves ambiguity when possible. Returns concise recent or relevant messages with stable refs for follow-up calls, not raw Telegram transport data. Do not use this for broad keyword search across many chats; use `SearchMessages` instead.

This is longer than conventional API docs on purpose. Current Anthropic guidance says detailed descriptions are one of the strongest levers for tool performance, and OpenAI guidance similarly treats metadata as the main discovery surface.

## Implications for mcp-telegram v1.2

1. Preserve a compact read-only surface.
2. Prefer tools that map to conversational jobs over Telethon/MTProto concepts.
3. Keep identifiers semantic by default and expose rawer detail only on demand.
4. Use structured outputs and machine-reusable refs consistently across tools.
5. Consider resources and prompts to reduce repeated tool calls for static context and guided workflows.
6. Evaluate the surface with direct, indirect, and negative prompt sets before expanding it.

## Sources

Primary sources:

- Model Context Protocol tools spec (2025-06-18): https://modelcontextprotocol.io/specification/2025-06-18/server/tools
- Model Context Protocol resources spec (2025-06-18): https://modelcontextprotocol.io/specification/2025-06-18/server/resources
- Model Context Protocol prompts spec (2025-06-18): https://modelcontextprotocol.io/specification/2025-06-18/server/prompts
- Model Context Protocol tool annotations reference: https://modelcontextprotocol.io/legacy/concepts/tools
- OpenAI Apps SDK, "Define tools": https://developers.openai.com/apps-sdk/plan/tools
- OpenAI Apps SDK, "Optimize Metadata": https://developers.openai.com/apps-sdk/guides/optimize-metadata/
- OpenAI blog, "What makes a great ChatGPT app": https://developers.openai.com/blog/what-makes-a-great-chatgpt-app
- Anthropic docs, "How to implement tool use": https://docs.anthropic.com/en/docs/agents-and-tools/tool-use/implement-tool-use
- Anthropic engineering, "Writing effective tools for AI agents": https://www.anthropic.com/engineering/writing-tools-for-agents

Most important cross-source takeaways:

- MCP gives multiple primitives; use tools, resources, and prompts deliberately, not interchangeably.
- OpenAI guidance strongly favors compact, discoverable tool surfaces and metadata iteration.
- Anthropic guidance strongly favors detailed descriptions, high-signal outputs, semantic identifiers, and job-level aggregation.
- The combined design pattern for a Telegram MCP server is: **few tools, strong descriptions, compact structured results, and hidden transport complexity**.
