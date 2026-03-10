---
phase: 02-tool-updates
plan: "01"
subsystem: testing
tags: [pytest, pytest-asyncio, tdd, mocking, telethon, unittest.mock]

# Dependency graph
requires:
  - phase: 01-support-modules
    provides: EntityCache, resolve(), encode_cursor/decode_cursor, format_messages()
provides:
  - 14 failing RED test stubs in tests/test_tools.py covering TOOL-01 through TOOL-07, CLNP-01, CLNP-02
  - mock_cache fixture: EntityCache seeded with entity 101 (Иван Петров)
  - make_mock_message fixture: factory for MagicMock Telethon messages
  - mock_client fixture: AsyncMock TelegramClient as async context manager
  - async_iter helper: async generator for mocking Telethon iterables
affects: [02-02, 02-03, 02-04, 02-05, 02-06, 02-07]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - "TDD RED phase: test stubs fail with pytest.fail('not implemented') not import errors"
    - "async_iter module-level helper avoids per-test async generator boilerplate"
    - "mock_client uses __aenter__/__aexit__ AsyncMock for async context manager protocol"
    - "Fixtures scoped at function level — each test gets isolated EntityCache"

key-files:
  created:
    - tests/test_tools.py
  modified:
    - tests/conftest.py

key-decisions:
  - "async_iter defined at module level in conftest (not inside fixture) so it can be referenced by mock_client.iter_dialogs/iter_messages"
  - "mock_cache uses function-scoped tmp_db_path to ensure SQLite isolation per test"
  - "test_get_dialog_removed and test_get_message_removed are sync (no async) — they only inspect the module, no client needed"

patterns-established:
  - "RED stubs: pytest.fail ensures FAILED not ERROR — confirms fixture wiring and collection are sound before implementation"
  - "Fixture chain: tmp_db_path -> mock_cache ensures proper DB path isolation"

requirements-completed: [TOOL-01, TOOL-02, TOOL-03, TOOL-04, TOOL-05, TOOL-06, TOOL-07, CLNP-01, CLNP-02]

# Metrics
duration: 5min
completed: 2026-03-11
---

# Phase 2 Plan 01: Test Stubs (RED) Summary

**14 pytest-asyncio stub tests establishing the Phase 2 test contract with mock_cache/mock_client/make_mock_message fixtures in conftest.py**

## Performance

- **Duration:** ~5 min
- **Started:** 2026-03-10T23:04:29Z
- **Completed:** 2026-03-10T23:09:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Extended conftest.py with 3 new fixtures (mock_cache, make_mock_message, mock_client) and async_iter helper
- Created tests/test_tools.py with 14 stub tests covering all Phase 2 requirements
- Verified RED phase: all 14 tests FAIL with "not implemented", zero collection errors
- Existing 22 tests remain green (22 passed, 14 failed in full suite)

## Task Commits

Each task was committed atomically:

1. **Task 1: Write conftest fixtures for tool tests** - `a043d2b` (test)
2. **Task 2: Write 14 failing stub tests in test_tools.py** - `b9622ad` (test)

_Note: TDD RED phase — both commits are test commits establishing the failing contract_

## Files Created/Modified

- `tests/conftest.py` - Added mock_cache, make_mock_message, mock_client fixtures and async_iter helper
- `tests/test_tools.py` - New file: 14 stub tests for TOOL-01 through TOOL-07, CLNP-01, CLNP-02

## Decisions Made

- async_iter defined at module level (not inside fixture) so it can be passed directly to mock_client.iter_dialogs/iter_messages as a return value factory
- mock_cache seeded with only entity 101 ("Иван Петров", username="ivan") matching the plan spec — minimal seed for name resolution tests
- CLNP tests are sync (no async fixtures injected) — they verify module-level class absence, not runtime behavior

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None.

## Next Phase Readiness

- Test contract established: 14 named tests match 02-VALIDATION.md exactly
- Fixture wiring verified: mock_cache, make_mock_message, mock_client available across all async test functions
- Implementation phases (02-02 through 02-07) can now run pytest to measure GREEN progress
- No blockers

---
*Phase: 02-tool-updates*
*Completed: 2026-03-11*
