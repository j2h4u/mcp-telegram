---
created: 2026-03-13T09:41:11Z
title: Carry deferred v1.1 cleanup and forum validation
area: planning
files:
  - .planning/ROADMAP.md
  - .planning/REQUIREMENTS.md
  - .planning/phases/09-forum-topics-support/09-MANUAL-VALIDATION.md
  - src/mcp_telegram/tools.py
  - src/mcp_telegram/formatter.py
---

## Problem

`v1.1` is being closed with the shipped feature scope complete, but a small amount of non-blocking follow-up work was intentionally deferred out of the milestone:

- Run the rewritten Phase 9 checklist against a real forum with 100+ topics and capture final live evidence.
- Remove `EntityCache.all_names()` if it is still orphaned.
- Remove dead imports in `src/mcp_telegram/tools.py`.
- Make `format_messages()` timezone handling internally consistent with its callers.

This work should not block milestone archival, but it should not be lost when the active `REQUIREMENTS.md` is archived and reset.

## Solution

Treat this as backlog input for the next cleanup-oriented planning pass.

Expected scope:

- Decide whether the large-forum validation is a standalone verification task or part of a broader forum-topics hardening phase.
- Re-scope the deferred code cleanup items into a compact tech-debt phase instead of keeping them as an unstarted milestone tail.
- Use the archived v1.1 milestone docs plus this todo as the handoff source when planning the next milestone.
