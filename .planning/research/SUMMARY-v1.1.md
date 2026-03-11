# Research Summary: mcp-telegram v1.1 (Observability & Completeness)

**Domain:** Extending Telegram MCP server with privacy-safe telemetry, cache improvements, and forum topics
**Researched:** 2026-03-12
**Overall confidence:** HIGH

## Executive Summary

v1.1 adds three interconnected feature areas to the v1.0 API: (1) usage telemetry designed for LLM consumption without PII leakage, (2) SQLite optimizations for concurrency and cache efficiency, and (3) Telegram forum topics support with full edge-case handling. The primary risks cluster around two domains: **privacy by design in telemetry** (side-channel attacks are real and documented for LLM traffic) and **cache invalidation correctness** (dialog state changes faster than expected, reactions change frequently). Successful v1.1 depends on async-first telemetry architecture (separate database, background flush), strict separation between metadata caches (slow-changing, long TTL) and state caches (fast-changing, fetch fresh), and comprehensive testing against real forum groups with edge cases. Topics implementation is straightforward once edge cases are enumerated (topic 0, deleted topics, pagination).

## Key Findings

**Stack:** Python 3.13, Telethon, separate SQLite database (analytics.db) for telemetry; async queue + background flush pattern; no new heavy dependencies

**Architecture:** Telemetry as fire-and-forget events queued in memory, flushed asynchronously (100 events/60s); entity metadata cache with 30d TTL; dialog/message state fetched fresh on every call; topic resolution scoped to dialog; error handling for Telegram API permission_denied

**Critical pitfall:** Timing/cardinality side-channel privacy leaks in telemetry (can reconstruct behavior from timing patterns even if IDs/names redacted); SQLite write contention if telemetry synchronous; cache staleness for dialog list and reactions

## Implications for Roadmap

Based on research, suggested phase structure:

1. **Phase 1: Telemetry Foundation** - Privacy-first design with separate database
   - Addresses: v1.1 requirement "usage telemetry module (SQLite, behavioral events only, zero PII)"
   - Avoids: Pitfalls 1, 5 (side-channel leakage, noisy output)
   - Goals: Telemetry schema (event_type, timestamp, duration, success_flag only); async queue implementation; GetUsageStats tool
   - Blocks: Phase 2 cache improvements depend on separate analytics.db availability

2. **Phase 2: Cache Improvements** - Separate database, indexes, automation
   - Addresses: Dialog list cache, reaction cache, cache invalidation strategy
   - Avoids: Pitfalls 2, 3, 10 (write contention, staleness, unbounded growth)
   - Goals: analytics.db setup; SQLite indexes on hot queries; daily retention cleanup (systemd timer); cache strategy docs
   - Blocks: Phase 4 (topics) can proceed in parallel; Phase 3 (navigation) orthogonal

3. **Phase 3: Navigation (from_beginning)** - Straightforward parameter addition
   - Addresses: v1.1 requirement "ListMessages navigation: from_beginning=true parameter"
   - Avoids: None (no new caching, telemetry, or complex dependencies)
   - Goals: Add `from_beginning` parameter; test pagination boundary cases
   - Can be completed in parallel with Phases 2 & 4

4. **Phase 4: Forum Topics** - Comprehensive edge-case handling
   - Addresses: v1.1 requirement "Forum topics support in ListMessages (filter by topic, show topic name)"
   - Avoids: Pitfalls 4, 11 (edge cases, resolver ambiguity)
   - Goals: ListMessages enhanced with `topic` parameter; topic 0 handling documented; deleted topic fallback; error handling for permission_denied
   - Depends on: Phase 2 complete (separate database available)

**Phase ordering rationale:**
- Phase 1 first: Establishes privacy constraints and async patterns used throughout; telemetry separate database created here
- Phase 2 after Phase 1: Builds on separate database; cache strategy informed by telemetry architecture
- Phases 3 & 4 in parallel: No dependencies; Phase 3 is isolated feature, Phase 4 orthogonal to caching

**Research flags for phases:**
- **Phase 1**: DETAILED RESEARCH COMPLETE — privacy-by-design patterns documented; side-channel risks enumerated; GetUsageStats output format needs iteration with Claude
- **Phase 2**: DETAILED RESEARCH COMPLETE — cache strategies mapped; SQLite optimization paths clear; requires load testing (concurrent calls) to validate concurrency assumptions
- **Phase 3**: NO DEEPER RESEARCH NEEDED — straightforward parameter addition; standard pagination boundaries
- **Phase 4**: DETAILED RESEARCH COMPLETE — topic edge cases enumerated; resolver scoping clear; requires real forum group testing (50+ topics, some deleted, some private)

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Python 3.13 already pinned; no new heavy deps; SQLite + asyncio well-established; separate database pattern standard |
| Features | HIGH | v1.0 already ships 90% of infrastructure; v1.1 adds incremental features (telemetry, topics, caching); requirements well-defined |
| Architecture | HIGH | Async queue pattern (fire-and-forget telemetry) standard in production systems; cache invalidation strategies well-documented; topic edge cases documented in Telegram API |
| Pitfalls | HIGH | Privacy/side-channel attacks documented in research (Whisper Leak 2025); SQLite concurrency limitations well-established; cache invalidation is classic systems problem |

## Gaps to Address

1. **GetUsageStats output format** — Research identified "noisy vs sparse" tradeoff; needs iteration with Claude to find optimal tool output format (HIGH priority for Phase 1)
2. **Load testing infrastructure** — Research flags SQLite concurrency and telemetry overhead; needs concurrent request benchmark (pytest-asyncio with 100+ concurrent calls) to validate assumptions (MEDIUM priority, Phase 2)
3. **Real forum group testing** — Telegram topics edge cases require testing against actual forum groups (not mock data) to verify pagination, permission_denied, deleted topic handling (MEDIUM priority, Phase 4)
4. **Privacy policy / data retention** — Telemetry retention policy (30 days?) needs documentation and configuration; legal/compliance review if deployed to production (LOW priority, Phase 1)

---

*Last updated: 2026-03-12 after v1.1 research*
