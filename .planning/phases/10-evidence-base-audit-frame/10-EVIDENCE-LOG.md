# Phase 10 Evidence Log

## Methodology

This evidence log keeps only sources that materially shape the later audit or redesign conclusions
for `mcp-telegram`; it is an audit input, not a general MCP literature review.

### Source tiers

- `Primary external`: normative external guidance from official MCP and Anthropic tool-use docs.
- `Brownfield authority`: live reflection, source code, and tests that define current
  `mcp-telegram` behavior.
- `Supporting official`: official but non-normative clarifications such as SDK docs or maintainer
  commentary.
- `Context only`: weaker community or secondary material used only when it adds explanatory context.

### Retention rule

- Official MCP and Anthropic documents are normative for external guidance.
- Live reflection, source code, and tests are authoritative for current `mcp-telegram` behavior.
- Retain a source only when later Phases 11-13 would cite it directly in an audit finding,
  redesign comparison, or sequencing decision.
- If no source from a weaker tier is retained, say so explicitly instead of silently collapsing the
  tier.

## Evidence matrix

| Source | Tier | Area informed | Why it applies to `mcp-telegram` | Later consumers |
| --- | --- | --- | --- | --- |
| MCP Tools specification | Primary external | Discovery and invocation contract | Defines `tools/list` and `tools/call`, which is the protocol contract behind the server's reflection-based discovery path. | 11, 12, 13 |
| Anthropic implement-tool-use doc | Primary external | Tool descriptions and input schemas | Applies because `ToolArgs` docstrings and Pydantic schemas are the current steering surface the model sees when choosing tools. | 11, 12 |
| Anthropic tool-use overview | Primary external | Structured-output expectations and tool contract framing | Applies because the current surface is text-first on output, so later phases need a named comparison point for model recovery burden. | 11, 12 |
| Live reflected tool list (`UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`) | Brownfield authority | Public runtime inventory | Applies because stale notes already drifted from runtime reality, and the reflected surface confirms that `ListTopics` is exposed today. | 11, 12, 13 |
| `src/mcp_telegram/server.py` | Brownfield authority | Discovery path and server boundary | Applies because it freezes reflection-based tool exposure, empty prompts/resources/templates, and generic `Tool <name> failed` wrapping. | 11, 12 |
| `src/mcp_telegram/telegram.py` | Brownfield authority | Runtime statefulness and auth/session storage | Applies because `create_client()` is process-cached and stores Telegram session state under XDG paths, so the server is read-only but not stateless. | 11, 12, 13 |
| `src/mcp_telegram/tools.py` | Brownfield authority | Public contract, pagination, recovery, workflow burden | Applies because most user-facing behavior lives here: text-first results, mixed pagination styles, topic workflows, and action-oriented recovery text. | 11, 12, 13 |
| `src/mcp_telegram/resolver.py` | Brownfield authority | Ambiguity recovery | Applies because exact-match-only auto-resolution and candidate lists directly control continuation burden when names are fuzzy. | 11, 12 |
| `src/mcp_telegram/formatter.py` | Brownfield authority | Output rendering conventions | Applies because the model consumes human-readable transcripts with date/session separators and inline topic labels instead of structured result objects. | 11, 12 |
| `src/mcp_telegram/cache.py` | Brownfield authority | Durable metadata and recovery-critical state | Applies because cached entities, reactions, and forum-topic tombstones reduce recovery burden and preserve deleted or inaccessible topic state across calls. | 11, 12, 13 |
| `src/mcp_telegram/analytics.py` | Brownfield authority | Privacy-safe telemetry invariant | Applies because later recommendations must preserve aggregate telemetry without logging message content or other Telegram identifiers. | 11, 12, 13 |
| `tests/test_formatter.py` | Brownfield authority | Locked transcript shape | Applies because it verifies the text-first rendering conventions the model actually reads, including separators and message formatting details. | 11 |
| `tests/test_resolver.py` | Brownfield authority | Locked ambiguity behavior | Applies because it confirms ambiguous fuzzy matches remain explicit candidates instead of silent auto-resolution, which affects recovery burden. | 11 |
| `tests/test_analytics.py` | Brownfield authority | Telemetry guarantees | Applies because it verifies the analytics schema, singleton behavior, and privacy-safe event model that constrain redesign options. | 11, 12, 13 |
| `tests/privacy_audit.sh` | Brownfield authority | Repo-level privacy guardrail | Applies because it enforces that telemetry fields stay aggregate and do not capture message content, names, usernames, or IDs. | 11, 12, 13 |
| `tests/test_tools.py` | Brownfield authority | Locked tool behavior and workflow burden | Applies because it captures forum-topic paths, `from_beginning` pagination, `next_offset` search pagination, action-oriented errors, and `[HIT]` search formatting. | 11, 12, 13 |

## Brownfield runtime note

The live reflected tool list is retained as brownfield authority because it is the closest view of
what an MCP client sees today. On 2026-03-13 the reflected surface was:
`GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`, and
`SearchMessages`.

`ListTopics` is called out explicitly because earlier notes were already stale on the public
inventory; later phases should trust reflection, code, and tests over inherited planning summaries.
