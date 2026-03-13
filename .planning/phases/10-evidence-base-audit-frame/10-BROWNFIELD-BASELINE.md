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
