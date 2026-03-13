---
phase: 06-telemetry-foundation
plan: 02
subsystem: observability
tags: [telemetry, analytics, instrumentation, sqlite, fire-and-forget, metrics]

requires:
  - phase: 06-telemetry-foundation
    plan: 01
    provides: TelemetryCollector singleton with async queue, analytics.db schema with telemetry_events table

provides:
  - Telemetry instrumentation in all 5 core tool handlers (ListDialogs, ListMessages, SearchMessages, GetMyAccount, GetUserInfo)
  - Exception-safe telemetry recording via try-finally pattern
  - Metrics collection: tool_name, timestamp, duration_ms, result_count, has_cursor, page_depth, has_filter, error_type
  - GetUsageStats tool stub (placeholder for Plan 03 implementation)
  - Comprehensive telemetry test coverage (9 new tests)

affects:
  - Phase 06 Plan 03 (GetUsageStats formatting)
  - Phase 06 Plan 04 (Privacy audit, Load testing)

tech-stack:
  added: []
  patterns:
    - "Telemetry hook pattern: t0 = time.monotonic() at start, try-finally for exception-safe recording"
    - "Lazy-load analytics collector via _get_analytics_collector() helper"
    - "Zero-PII event structure: no entity_id, dialog_id, username in telemetry"
    - "Fire-and-forget telemetry: record_event() never blocks tool execution"

key-files:
  created:
    - tests/test_tools.py (telemetry test functions)
  modified:
    - src/mcp_telegram/tools.py (all 5 tool handlers + GetUsageStats stub)

key-decisions:
  - "Telemetry recorded via finally block to ensure recording even on error"
  - "error_type captured as exception.__class__.__name__ (categorical, no PII)"
  - "GetUsageStats does NOT record telemetry to avoid noise in analytics"
  - "_get_analytics_collector() helper mirrors get_entity_cache() pattern for lazy singleton loading"

requirements-completed: [TEL-04]

metrics:
  duration: 25min
  completed: 2026-03-12
---

# Phase 6 Plan 2: Tool Handler Telemetry Instrumentation Summary

**Telemetry hooks added to 5 core tool handlers with exception-safe try-finally pattern, 9 comprehensive tests, GetUsageStats stub**

## Performance

- **Duration:** 25 min
- **Completed:** 2026-03-12
- **Tasks:** 3 (all auto)
- **Files modified:** 2
- **Tests added:** 9 (all green)

## Accomplishments

- Instrumented ListDialogs, ListMessages, SearchMessages, GetMyAccount, GetUserInfo with telemetry recording
- All telemetry hooks use try-finally pattern for exception-safe recording (error_type captured before re-raise)
- Metrics correctly computed per tool: result_count, has_cursor, page_depth, has_filter
- GetUsageStats tool stub defined (placeholder for Plan 03)
- 37 total tests passing (28 existing + 9 new telemetry tests)
- Zero PII in telemetry events (categorical error types only)

## Task Commits

1. **Task 1 & 2: Add telemetry hooks to 5 tool handlers + comprehensive test suite** - `cb9743c`
   - Added _get_analytics_collector() lazy-loader helper
   - Instrumented list_dialogs, list_messages, search_messages, get_my_account, get_user_info
   - All handlers: try-finally pattern with error_type tracking
   - 9 new tests: test_list_dialogs_records_telemetry, test_list_messages_records_telemetry, test_list_messages_records_cursor, test_list_messages_records_filter, test_search_messages_records_telemetry, test_get_my_account_records_telemetry, test_get_user_info_records_telemetry, test_tool_records_telemetry_on_error, test_get_usage_stats_not_recorded

3. **Task 3: Create GetUsageStats tool stub** - included in commit `cb9743c`
   - GetUsageStats class and handler stub created
   - Placeholder response (implementation in Plan 03)
   - No telemetry recording for this tool

## Files Created/Modified

- `src/mcp_telegram/tools.py` - 735 insertions, 339 deletions
  - Added _get_analytics_collector() helper function (lazy singleton pattern)
  - Modified list_dialogs(): telemetry instrumentation with try-finally
  - Modified list_messages(): telemetry instrumentation + cursor/filter tracking
  - Modified search_messages(): telemetry instrumentation + has_filter=True
  - Modified get_my_account(): telemetry instrumentation
  - Modified get_user_info(): telemetry instrumentation
  - Added GetUsageStats tool class and handler stub

- `tests/test_tools.py` - 9 new test functions
  - mock_analytics_collector fixture for mocking TelemetryCollector
  - test_list_dialogs_records_telemetry() - validates tool_name, result_count, error handling
  - test_list_messages_records_telemetry() - validates basic metrics
  - test_list_messages_records_cursor() - validates has_cursor=True when args.cursor provided
  - test_list_messages_records_filter() - validates has_filter=True when sender filter applied
  - test_search_messages_records_telemetry() - validates has_filter=True (search inherently filtered)
  - test_get_my_account_records_telemetry() - validates result_count=1
  - test_get_user_info_records_telemetry() - validates result_count=1
  - test_tool_records_telemetry_on_error() - validates error_type captured even on exception
  - test_get_usage_stats_not_recorded() - validates no telemetry for GetUsageStats

## Decisions Made

1. **Try-finally pattern for telemetry recording** - Ensures recording happens even when exception raised. Start timing with t0 = time.monotonic(), capture error_type in except, record in finally.

2. **Zero-PII event structure** - error_type is categorical (exception.__class__.__name__) never includes entity_id, dialog_id, message_id, or exception message.

3. **GetUsageStats excluded from telemetry** - Avoids noise in analytics when the tool queries telemetry itself. Stub placeholder for Plan 03 implementation.

4. **Lazy-load pattern via _get_analytics_collector()** - Mirrors existing get_entity_cache() pattern. Singleton loaded on first tool execution, never during import.

5. **Filter tracking in ListMessages** - has_filter=True when sender filter or unread filter applied, not just for query text.

## Deviations from Plan

None - plan executed exactly as written. All telemetry hooks implemented per specification with exception-safe finally blocks, correct metric computation, and comprehensive test coverage.

## Issues Encountered

None - all tests passed on first run. Implementation straightforward following the Pattern 3 specification from RESEARCH.md.

## Next Phase Readiness

- Plan 03 (GetUsageStats formatting) can now query analytics.db with confidence that all tool calls are instrumented
- Plan 04 (Privacy audit + Load testing) can validate telemetry correctness and measure performance overhead
- Telemetry pipeline fully operational: events recorded → async queue → batch flush every 60s or 100 events

---

*Phase: 06-telemetry-foundation*
*Plan: 02*
*Completed: 2026-03-12*
