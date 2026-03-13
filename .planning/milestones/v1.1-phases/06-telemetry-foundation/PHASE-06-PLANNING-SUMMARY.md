# Phase 6: Telemetry Foundation — Planning Summary

**Date:** 2026-03-12
**Phase:** 6 (Telemetry Foundation)
**Milestone:** v1.1 — Observability & Completeness

---

## Planning Complete

**4 plans created** across 2 waves with clear dependencies and parallel execution paths.

### Wave Structure

| Wave | Plans | Scope | Dependencies |
|------|-------|-------|--------------|
| **Wave 0** | 06-01 | Core infrastructure (TelemetryCollector, analytics.db, test scaffold) | None (foundation) |
| **Wave 1** | 06-02, 06-03 | Tool instrumentation + GetUsageStats feature | Depends on 06-01 |
| **Wave 2** | 06-04 | Validation (privacy audit, load testing) | Depends on 06-01, 06-02, 06-03 |

### Plan Overview

#### Plan 06-01: TelemetryCollector & Analytics Foundation (Wave 0)
**Requirements addressed:** TEL-01
**Tasks:** 2 (TDD pattern)
- Task 1: Create analytics.py with TelemetryCollector singleton, TelemetryEvent schema, async batch flush
- Task 2: Create comprehensive test suite (test_analytics.py) — 12+ tests covering schema, non-blocking record, async flush, singleton pattern

**Outputs:**
- `src/mcp_telegram/analytics.py` — TelemetryCollector, TelemetryEvent, analytics.db setup
- `tests/test_analytics.py` — Complete unit test coverage

**Design highlights:**
- TelemetryEvent: immutable dataclass, frozen=True, zero PII fields
- TelemetryCollector: singleton pattern with lazy instantiation
- Batch queue: 100-event threshold or 60s timeout for flushing
- Async flush: uses asyncio.create_task() with strong reference to prevent GC
- Thread safety: threading.Lock for batch access, run_in_executor for DB writes
- Privacy-first: event schema prevents PII at collection layer (no entity_id, dialog_id, names, etc.)

---

#### Plan 06-02: Tool Instrumentation & GetUsageStats Stub (Wave 1)
**Requirements addressed:** TEL-04
**Tasks:** 3 (TDD pattern for tasks 1-2)
- Task 1: Add telemetry hooks to 5 tool handlers (ListDialogs, ListMessages, SearchMessages, GetMe, GetUserInfo)
- Task 2: Create telemetry tests for tool handlers (12+ tests verifying event metrics per tool)
- Task 3: Create GetUsageStats tool stub (placeholder, completed in 06-03)

**Outputs:**
- `src/mcp_telegram/tools.py` — Telemetry integration in 5 handlers + GetUsageStats stub
- `tests/test_tools.py` — Telemetry recording tests per tool

**Design highlights:**
- Try-finally pattern ensures telemetry recorded even on exception
- Metrics computed per tool: result_count, has_cursor, page_depth, has_filter, error_type
- GetUsageStats NOT instrumented (avoid noise in analytics)
- Error type: categorical (exception class name), never entity IDs or message content
- No blocking: record_event() returns immediately (<1µs latency)

---

#### Plan 06-03: GetUsageStats Tool & Natural Language Formatting (Wave 1)
**Requirements addressed:** TEL-02
**Tasks:** 3 (TDD pattern for tasks 1-2)
- Task 1: Implement format_usage_summary() and complete get_usage_stats() handler
- Task 2: Create test coverage for output size and content (token counting, metric validation)
- Task 3: Manual verification checkpoint (human review of output quality)

**Outputs:**
- `src/mcp_telegram/tools.py` — Complete GetUsageStats handler with format_usage_summary()
- `tests/test_tools.py` — GetUsageStats token count and content tests
- `tests/test_analytics.py` — Integration test for usage summary formatting

**Design highlights:**
- Natural language summary <100 tokens (target 60-80)
- Template-based formatting (no ML models, deterministic output)
- Actionable metrics: tool frequency, deep scroll detection, error rates, latency p95
- 30-day query window (configurable retention policy in Phase 7)
- Graceful fallback: helpful message if DB missing or empty

---

#### Plan 06-04: Privacy Audit & Load Testing (Wave 2)
**Requirements addressed:** TEL-03, TEL-04 (validation)
**Tasks:** 3 (no TDD — pure validation)
- Task 1: Create privacy_audit.sh script for PII pattern detection (grep-based)
- Task 2: Create load test baseline (test_load.py) — 100 concurrent ListMessages, measure overhead
- Task 3: Verification gate (run all checks, document findings)

**Outputs:**
- `tests/privacy_audit.sh` — Automated PII pattern detection (exit 0 = PASS)
- `tests/test_load.py` — Load test baseline (100 concurrent calls, overhead <0.5ms per call)
- `.planning/phases/06-telemetry-foundation/06-AUDIT-REPORT.md` — Final validation report

**Design highlights:**
- Privacy audit: grep patterns for entity_id, dialog_id, sender_id, message_id, username (all must be absent)
- Load test: 100 concurrent ListMessages with mock client (isolates telemetry overhead)
- Latency measurement: average per-call, p95 percentile, batch flush non-blocking verification
- Threshold: <0.5ms telemetry overhead confirmed acceptable for production

---

## Requirements Coverage

| Req ID | Description | Plan | Status |
|--------|-------------|------|--------|
| TEL-01 | analytics.py module with TelemetryCollector, event schema | 06-01 | ✓ Planned |
| TEL-02 | GetUsageStats tool with natural-language summary <100 tokens | 06-03 | ✓ Planned |
| TEL-03 | Privacy audit (zero PII in telemetry) | 06-04 | ✓ Planned |
| TEL-04 | Telemetry hooks in 5 tool handlers | 06-02 | ✓ Planned |

**Coverage:** 100% (all 4 v1.1 requirements for Phase 6 addressed)

---

## Key Design Decisions

### Architecture
1. **Separate analytics.db from entity_cache.db** — Prevents write contention under concurrent loads (Phase 7 validation)
2. **Singleton TelemetryCollector** — Single instance per process, lazy initialization
3. **Async batch queue with 100-event threshold** — Balance between flush frequency and overhead
4. **Thread-safe batch + executor-based DB writes** — Event loop never blocked

### Privacy
1. **Collection-layer enforcement** — Schema prevents PII at source, no redaction needed
2. **Categorical error types** — Only exception class name, never entity IDs or details
3. **30-day retention** — Default policy (configurable, cleanup in Phase 7)
4. **Grep-based audit** — Simple, reproducible, CI/pre-commit friendly

### Testing
1. **TDD pattern for core modules** — test_analytics.py and tool instrumentation tests written before implementation
2. **Mock TelemetryCollector in tests** — Avoid DB side effects, fast feedback
3. **Load test with mock client** — Isolates telemetry overhead from network latency
4. **Wave 0 test scaffold** — All required tests defined before any implementation

---

## Dependencies & Sequencing

### Wave 0 (Foundation)
- No external dependencies (uses stdlib: asyncio, sqlite3, threading)
- Must complete before Wave 1
- Creates analytics.db schema + TelemetryCollector singleton

### Wave 1 (Integration)
- Depends on Wave 0 (imports TelemetryCollector, analytics DB path)
- Two plans (06-02, 06-03) can run in parallel
- 06-02 instruments tools, 06-03 completes GetUsageStats feature
- Both ready for Wave 2 validation

### Wave 2 (Validation)
- Depends on Wave 0 + Wave 1 (needs working telemetry pipeline)
- Privacy audit validates analytics.py + tools.py (06-01, 06-02)
- Load test validates performance (all three prior plans)
- Creates final audit report

---

## Success Criteria

All criteria must be TRUE for Phase 6 completion:

1. ✓ analytics.db created on first startup with telemetry_events table
2. ✓ All 5 tool handlers emit telemetry asynchronously (never blocking)
3. ✓ GetUsageStats returns summary <100 tokens with actionable metrics
4. ✓ Privacy audit confirms zero PII patterns in telemetry code
5. ✓ Load test confirms <0.5ms overhead per tool call

---

## Verification Strategy

**Per-task:** `pytest tests/test_analytics.py -v -x` (after 06-01)
**Per-wave:** Full test suite `pytest tests/ -v` + `bash tests/privacy_audit.sh` (after each wave)
**Phase gate:** All tests green + privacy audit PASS + load test PASS + manual review

---

## Next Steps

Execute Phase 6 plans:
```bash
/gsd:execute-phase 06
```

This will run:
1. Wave 0: Create TelemetryCollector + test scaffold (06-01)
2. Wave 1: Instrument tools + complete GetUsageStats (06-02, 06-03 in parallel)
3. Wave 2: Privacy audit + load testing (06-04)

Expected duration: ~3-4 hours execution time
Expected test count: 57+ existing + 30+ new telemetry tests = 87+ total

---

## Notes

- **No CONTEXT.md for Phase 6** — No locked user decisions; phase is independent with clear research guidance
- **Research confidence: HIGH** — All patterns (singleton, async batch queue, privacy audit) are standard in production telemetry systems
- **Validation completeness: 100%** — All requirements have automated verify commands or Wave 0 test scaffolds
- **Regression risk: LOW** — All telemetry hooks added in try-finally blocks; no changes to tool logic; mocked in tests

---

**Planning Status:** ✓ COMPLETE
**Ready for execution:** Yes
**Date:** 2026-03-12
**Planner:** Claude Code (GSD Planner)
