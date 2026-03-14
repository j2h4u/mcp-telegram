---
created: 2026-03-14T11:08:42.631Z
title: Harden SQLite cache against parallel MCP sessions
area: general
files:
  - src/mcp_telegram/cache.py
  - src/mcp_telegram/tools.py
  - .planning/phases/17-direct-read-search-workflows/17-UAT.md
---

## Problem

During Phase 17 runtime verification against the live `mcp-telegram` container, parallel independent MCP client sessions exposed a cache-layer concurrency failure that does not appear in sequential calls.

Observed behavior:

- One session successfully completed `ListTopics` or `ListMessages`.
- A concurrent `ListMessages` call in a separate MCP stdio session failed with `sqlite3.OperationalError: database is locked`.
- The stack trace pointed to `EntityCache.__init__()` in `src/mcp_telegram/cache.py`, specifically the `PRAGMA optimize` path during cache connection setup.

This did not block the direct read/search workflow verification itself because the same scenarios passed when run sequentially, but it is a real runtime hardening gap: multiple external clients or overlapping MCP tool invocations can hit the shared SQLite-backed cache at the same time.

## Solution

Investigate and harden cache initialization and shared SQLite usage for parallel MCP sessions.

Expected scope:

- Reproduce the lock deterministically with two parallel MCP client sessions against the live container.
- Confirm whether the lock is caused by `PRAGMA optimize`, connection setup timing, WAL mode negotiation, or another initialization-side write on a shared database file.
- Make cache initialization safe under concurrent process starts, or move write-heavy setup work out of the hot path.
- Add regression coverage for concurrent multi-session access if it can be made deterministic enough for CI; otherwise add a targeted runtime verification script.
- Re-check both `entity_cache.db` and `analytics.db` interaction patterns so fixing one startup path does not leave another lock-prone path behind.
