# State: mcp-telegram

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-11)

**Core value:** LLM can work with Telegram using natural names — zero cold-start friction
**Current focus:** Milestone v1.0 — Core API

## Current Position

Phase: Not started (defining requirements)
Plan: —
Status: Defining requirements
Last activity: 2026-03-11 — Milestone v1.0 started

## Accumulated Context

- Brownfield project; existing tools: ListDialogs, ListMessages, SearchMessages, GetDialog, GetMessage
- `@singledispatch` routing pattern in tools.py
- Pydantic v2 models for tool args and schema generation
- Two-tier cache planned: L1 in-memory (5min TTL), L2 SQLite entity store
- Name resolution via rapidfuzz WRatio (90/60/<60 thresholds)
- All decisions logged in PROJECT.md Key Decisions table
