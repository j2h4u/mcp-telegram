# Phase 10 Brownfield Baseline: Current MCP Surface

Last verified: 2026-03-13

This document freezes what the model-facing `mcp-telegram` surface looks like today from runtime
reflection, source, and tests. It is a Phase 11 audit input, not a redesign proposal.

## Public Surface Snapshot

### Reflected Tool Inventory

The current reflected public surface is seven tools, not the six-tool list found in older notes.
`ListTopics` is part of the shipped surface.

| Tool | Evidence |
|------|----------|
| `GetMyAccount` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13 |
| `GetUsageStats` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13 |
| `GetUserInfo` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13 |
| `ListDialogs` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13 |
| `ListMessages` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13 |
| `ListTopics` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13; [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1042) |
| `SearchMessages` | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` on 2026-03-13 |

Runtime cross-check:
`UV_CACHE_DIR=/tmp/.uv-cache uv run python -c "from mcp_telegram.server import enumerate_available_tools; print([name for name, _ in enumerate_available_tools()])"`

### Discovery and Metadata Path

The discovery path is reflection-based. [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L29)
iterates `inspect.getmembers(tools, inspect.isclass)`, keeps `ToolArgs` subclasses, and turns each
one into an MCP `Tool` via `tools.tool_description()`.

The tool map is snapshotted at process start rather than refreshed dynamically. `server.py` builds
`mapping = dict(enumerate_available_tools())` once at import time, and `enumerate_available_tools()`
itself is cached. [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L29)

Tool descriptions come from docstrings plus Pydantic schema, sanitized before exposure. The
metadata path is `ToolArgs` subclass docstring -> `args.model_json_schema()` ->
`_sanitize_tool_schema()` -> MCP `Tool`. [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L147)

Prompts, resources, and resource templates are currently empty. `list_prompts()`,
`list_resources()`, and `list_resource_templates()` all return `[]`.
[src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L43)

Unhandled handler failures are collapsed at the server boundary to generic `Tool <name> failed`
errors. The original exception is logged, but the model-facing failure is wrapped as
`RuntimeError(f"Tool {name} failed")`. [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L72)

## Workflow Baseline

### Result Shape and Recovery Style

Result bodies are text-first and usually a single `TextContent`. The server contract only accepts
tool handler returns shaped as `Sequence[TextContent | ImageContent | EmbeddedResource]`, and the
current handlers predominantly emit one `TextContent(type="text", text=...)` body.
[src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py#L72)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1014)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1567)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1766)

Message rendering is human-readable text with date headers, session breaks, and optional inline
topic labels. That formatting contract lives in `format_messages()`.
[src/mcp_telegram/formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py#L9)
[tests/test_formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_formatter.py)

Recovery is action-oriented rather than opaque. Missing dialog, ambiguous dialog, missing sender,
missing topic, deleted topic, inaccessible topic, invalid cursor, and empty telemetry all return
next-step instructions rather than bare failures. Representative anchors:
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L507)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L543)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L557)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L600)
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L659)

Ambiguity handling is resolver-driven. Dialog, sender, topic, and user queries all flow through
`resolve(...)`, and fuzzy matches intentionally return `Candidates` so the model is guided to retry
with an exact choice rather than silently auto-picking. [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L248)
[tests/test_resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_resolver.py#L67)
[tests/test_resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_resolver.py#L143)

### Workflow Burden and Pagination

Phase 11 should treat workflow choreography as part of the public contract, not just handler internals.

| Surface area | Frozen behavior | Evidence |
|--------------|-----------------|----------|
| Discovery flow | `ListDialogs` is the starting point for name discovery and cache warmup. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L975) |
| Forum flow | Forum usage is often `ListDialogs -> ListTopics -> ListMessages`. `ListTopics` explicitly tells the model to use it before `topic=`. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1042) |
| Archived scope | `ListDialogs` defaults to mixed archived + non-archived scope, with `exclude_archived` and `ignore_pinned` as explicit knobs. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L962), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2468) |
| Topic status | `ListTopics` exposes stable status labels including `general`, `active`, and `previously_inaccessible`. Deleted topics exist in cached metadata but do not appear in active listings. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L699), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1561) |
| Message reading mode | `ListMessages` supports backward cursor pagination and forward-in-time pagination via `from_beginning=True`. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1140), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2371) |
| Topic recovery | `ListMessages` preserves deleted-topic and inaccessible-topic recovery paths instead of pretending the filter succeeded. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1281), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1110), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1162) |
| Search shape | `SearchMessages` returns hit-centric groups with `+-3` context windows and explicit `[HIT]` marking. | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1681), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1674), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1714) |
| Pagination split | The current surface mixes `next_cursor` (`ListMessages`) and `next_offset` (`SearchMessages`). | [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1559), [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1766) |

Additional behavior worth preserving in the audit:

- `ListMessages` without `topic=` in a forum dialog can return a cross-topic page with inline
  `[topic: ...]` labels. [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1526)
- Deleted-topic tombstones are retained so recovery can explain that a topic existed but can no
  longer be fetched. [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L484)
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1520)
- Topic metadata can record a `previously_inaccessible` state after Telegram rejects access, which
  is later surfaced in topic listings. [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L699)
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1279)

## Preserved Invariants and Stateful Constraints

These are the default-preserve invariants for later redesign work unless stronger evidence justifies
breaking them.

| Invariant | Why it matters | Evidence |
|-----------|----------------|----------|
| Read-only Telegram access | Read-only Telegram scope is a shipped invariant. The public surface is entirely read-oriented: list, search, lookup, and telemetry-only observation. No mutating tool is reflected. | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools`; [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L962) |
| Stateful runtime | The system is not stateless. Telegram client creation is cached, and XDG-backed cache and analytics DBs persist across calls. | [src/mcp_telegram/telegram.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/telegram.py#L56), [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L195), [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L204) |
| Recovery-critical cached metadata | Entity, reaction, and topic caches materially reduce agent burden and preserve recovery context such as deleted topics and prior topic access failures. | [src/mcp_telegram/cache.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/cache.py#L14), [src/mcp_telegram/cache.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/cache.py#L109), [src/mcp_telegram/cache.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/cache.py#L188) |
| Privacy-safe telemetry | Telemetry is aggregate and intentionally avoids message-content logging or user-identifying event fields. The `privacy_audit` shell check backs that invariant. | [src/mcp_telegram/analytics.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/analytics.py#L30), [tests/privacy_audit.sh](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/privacy_audit.sh#L1), [tests/test_analytics.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_analytics.py) |
| Tests are contract evidence | Formatter, resolver, analytics, `privacy_audit`, and tool tests are evidence for shipped behavior, not optional implementation detail. | [tests/test_formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_formatter.py), [tests/test_resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_resolver.py), [tests/test_analytics.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_analytics.py), [tests/privacy_audit.sh](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/privacy_audit.sh), [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py) |

## Mismatches Between Stale Notes and Runtime Reality

- Older notes that described a six-tool public surface are stale. Live reflection on 2026-03-13
  exposes seven tools, including `ListTopics`.
- The brownfield contract is broader than a plain tool inventory. The current surface already bakes
  in workflow burden, action-oriented recovery, topic-state semantics, and mixed pagination
  conventions that later phases must audit explicitly.
