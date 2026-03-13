---
created: 2026-03-13T09:41:11Z
title: Refactor MCP tool surface around capability-oriented best practices
area: planning
files:
  - src/mcp_telegram/tools.py
  - src/mcp_telegram/server.py
  - .planning/ROADMAP.md
  - .planning/REQUIREMENTS.md
---

## Problem

The current Telegram MCP surface is functional, but parts of it still expose agent-facing complexity that looks closer to an adapted API than a capability-oriented macro-tool surface. The discussion in this session highlighted several areas to revisit before a larger refactor:

- Tool descriptions still mix technical reference material with agent instructions.
- Some workflows require low-level paging, disambiguation, or topic discovery steps that may be better absorbed into higher-level tools.
- The public contract should be reviewed against MCP and Anthropic guidance for context efficiency, tool descriptions, and capability aggregation instead of assuming the current surface is the long-term design.

Source material explicitly called out for the future refactor:

1. https://modelcontextprotocol.info/docs/tutorials/writing-effective-tools/
2. https://www.scalekit.com/blog/wrap-mcp-around-existing-api
3. https://www.anthropic.com/research/building-effective-agents
4. https://anthropic.mintlify.app/en/docs/agents-and-tools/tool-use/implement-tool-use
5. https://github.com/vishnu2kmohan/mcp-server-langgraph/blob/main/adr/adr-0023-anthropic-tool-design-best-practices.md

## Solution

Start a future milestone focused on tool-surface redesign rather than incremental patching.

Expected scope:

- Audit the current tool catalog by user task, not by Telethon endpoint shape.
- Define target macro-tools around real agent capabilities such as discovery, scoped reading, and contextual search.
- Rewrite tool descriptions/schema guidance so each tool behaves like an instruction to the model.
- Minimize agent-visible cursor, ID, and multi-step orchestration burden where it is safe to hide implementation detail.
- Preserve read-only security constraints and keep Telegram-specific complexity behind stable tool contracts.
