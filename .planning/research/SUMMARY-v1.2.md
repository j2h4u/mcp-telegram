# Research Summary: v1.2 MCP Surface Research

**Project:** mcp-telegram
**Milestone:** v1.2 MCP Surface Research
**Researched:** 2026-03-13
**Goal:** Ground a future MCP surface refactor in authoritative tool-design guidance and translate that guidance into concrete refactor options for this Telegram server.

## Executive Summary

The research converges on a clear conclusion: `mcp-telegram` should not be treated as a Telethon adapter with nicer names, and it also should not be collapsed into one giant "Telegram" mega-tool. The problem to solve is narrower and more concrete: reduce the amount of Telegram-specific orchestration that the model has to perform while preserving the recovery signals it actually needs.

Across MCP and Anthropic primary sources, the most relevant design pressure is consistent:

1. shape tools around real model tasks, not backend endpoints
2. keep tool descriptions operational and explicit
3. use compact, structured outputs where the model needs to continue work
4. hide low-level transport mechanics unless they directly improve recovery
5. treat schema stability, recoverable errors, and runtime verification as part of the product contract

For this repo, the default recommendation emerging from both the architecture and pitfalls research is a **medium redesign**. That recommendation does not eliminate the need to compare minimal, medium, and maximal paths; it means the evidence currently points to medium as the most defensible default candidate.

## Source Hierarchy

### Authoritative sources

- Model Context Protocol specification and official MCP docs
- Anthropic official tool-use docs and agent research

These sources materially drive the milestone conclusions.

### Supporting secondary sources

- Scalekit brownfield MCP adapter article
- `modelcontextprotocol.info` practical tutorial pages
- third-party ADRs and community writeups

These are useful for translation and implementation heuristics, but they should not override official MCP or Anthropic guidance.

## Key Findings

### Current surface problem

The current server already avoids raw Telegram IDs in many common flows, but the public tool surface still leaks too much agent-facing choreography:

- `ListMessages` bundles dialog resolution, topic resolution, sender filtering, unread mode, and cursor handling into one overloaded contract
- `SearchMessages` uses a different continuation model from `ListMessages`
- `ListTopics` often exists as a preparatory helper step for later reading, which suggests public orchestration leakage
- most recovery signals are prose conventions rather than explicit structured fields
- the server currently exposes every `ToolArgs` subclass by reflection, which weakens the public/internal boundary

### What should change conceptually

The milestone should study and compare redesigns that move the public surface toward:

- capability-oriented tool boundaries
- stable result envelopes
- explicit structured recovery state
- clearer separation between public tools, compatibility tools, and internal helpers
- more legible read-only and error semantics in MCP metadata

### What should not change casually

The research strongly suggests preserving these invariants unless a later milestone explicitly decides otherwise:

- read-only scope
- fresh Telegram reads at call time
- privacy-safe telemetry
- no message-content caching
- recovery-critical signals such as ambiguity candidates, resolution echoes, warnings, and continuation handles

## Implications For v1.2 Deliverables

This milestone should end with a decision-ready memo, not a generic best-practices digest. The final deliverable needs to contain:

- a grounded audit of the current MCP surface against named best-practice sources
- a comparison of **minimal**, **medium**, and **maximal** redesign paths
- a clear statement of which current tools and patterns each path would keep, reshape, merge, demote, or remove
- one Pareto-style recommendation for the smallest safe change set likely to produce outsized model-usage impact
- recommended sequencing and validation criteria for the later implementation milestone

## Working Recommendation

Current evidence points to a **medium redesign** as the leading recommendation because it appears to offer the best trade-off:

- more impact than a documentation-only cleanup
- less migration and debugging risk than a full contract rewrite
- compatible with existing brownfield internals such as resolver, caches, formatter, and telemetry

That said, the milestone should still deliberately articulate:

- what the **minimal** path would deliver quickly
- what the **medium** path fixes that minimal does not
- what the **maximal** path unlocks, and why it is probably not the default next step

## Success Condition For The Research

v1.2 is successful if the next milestone can start with a narrow, evidence-backed answer to:

- what to change first
- what to leave alone
- how to measure whether the new MCP surface is actually better for LLM use
