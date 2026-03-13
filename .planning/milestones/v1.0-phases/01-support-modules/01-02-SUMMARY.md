---
phase: 01-support-modules
plan: "02"
subsystem: resolver
tags: [rapidfuzz, fuzzy-matching, python, tdd, wratio]

requires:
  - phase: 01-support-modules
    plan: "01"
    provides: "rapidfuzz installed, test stub files created (tests/test_resolver.py with 6 stubs)"

provides:
  - "resolve() pure function: WRatio fuzzy match with AUTO_THRESHOLD=90 / CANDIDATE_THRESHOLD=60"
  - "Tagged-union result types: Resolved, Candidates, NotFound (frozen dataclasses)"
  - "ResolveResult type alias for type-safe pattern matching"
  - "Numeric query bypass: isdigit() → id lookup without fuzzy matching"
  - "Ambiguity detection: >=2 candidates above AUTO_THRESHOLD → Candidates (not Resolved)"
  - "All 6 test_resolver.py tests green (RES-01, RES-02)"

affects:
  - "Phase 2 tool wiring (resolver called from ListDialogs, ListMessages sender filter)"
  - "01-03-PLAN, 01-04-PLAN (parallel wave 1 — not blocked by resolver)"

tech-stack:
  added: []
  patterns:
    - "Tagged union via frozen dataclasses: Resolved | Candidates | NotFound"
    - "process.extract(choices.keys(), score_cutoff=CANDIDATE_THRESHOLD, limit=None) then inspect above_auto list"
    - "TDD RED: import inside function body so collection succeeds; GREEN: module-level import after implementation"

key-files:
  created:
    - src/mcp_telegram/resolver.py
  modified:
    - tests/test_resolver.py

key-decisions:
  - "Pass name_to_id.keys() (list of strings) not the dict itself to process.extract — avoids TypeError with rapidfuzz dict dispatch path"
  - "Ambiguity check on above_auto list after extract — single >=2 count check handles both 2 and N-way ties"

patterns-established:
  - "Resolver is a pure function: takes pre-loaded dict[int, str], makes no API calls — callers own data loading"
  - "Same resolve() for both dialog and sender resolution (RES-02 is a second call site, not a second function)"

requirements-completed: [RES-01, RES-02]

duration: 2min
completed: 2026-03-10
---

# Phase 1 Plan 02: Fuzzy Name Resolver Summary

**WRatio fuzzy resolver with Resolved/Candidates/NotFound tagged union, numeric bypass, and ambiguity detection — 6 tests green via TDD RED-GREEN-REFACTOR cycle**

## Performance

- **Duration:** ~2 min
- **Started:** 2026-03-10T22:26:22Z
- **Completed:** 2026-03-10T22:28:30Z
- **Tasks:** 3 (RED, GREEN, REFACTOR)
- **Files modified:** 2 (1 created, 1 modified)

## Accomplishments

- `src/mcp_telegram/resolver.py` implemented with `resolve()` function and frozen dataclass result types covering all threshold cases
- All 6 `test_resolver.py` tests pass: exact match, numeric bypass (id present and absent), ambiguity, sender resolution, not-found, below-candidate-threshold
- Module-level imports in test file after refactor; code style compliant (no `Optional`, `from __future__ import annotations`, absolute imports)

## Task Commits

Each TDD phase was committed atomically:

1. **RED: Add failing resolver tests** - `564672a` (test)
2. **GREEN: Implement fuzzy name resolver** - `ad16db8` (feat)
3. **REFACTOR: Move imports to module level** - `11920e6` (refactor)

## Files Created/Modified

- `src/mcp_telegram/resolver.py` - resolve() function + Resolved/Candidates/NotFound/ResolveResult; AUTO_THRESHOLD=90, CANDIDATE_THRESHOLD=60
- `tests/test_resolver.py` - Full assertions replacing stub pytest.fail() calls; module-level imports after refactor

## Decisions Made

- Pass `name_to_id.keys()` (iterable of strings) to `process.extract` rather than the dict itself — passing a dict triggers a different rapidfuzz code path that expects string values and raises `TypeError: sentence must be a String`
- Ambiguity check: collect `above_auto` list after extract, check `len >= 2` — this naturally handles 2-way and N-way ties without separate logic

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed rapidfuzz dict dispatch TypeError**
- **Found during:** GREEN phase (first test run)
- **Issue:** Initial implementation passed `name_to_id` dict directly to `process.extract`. rapidfuzz dispatches on the type of `choices` argument; when given a dict it iterates values (not keys) expecting string values, but received int entity_ids → `TypeError: sentence must be a String`
- **Fix:** Changed to pass `name_to_id.keys()` (list of strings); third element in hits becomes list index; lookup entity_id via `name_to_id[name]` after match
- **Files modified:** `src/mcp_telegram/resolver.py`
- **Verification:** All 6 tests pass after fix
- **Committed in:** `ad16db8` (GREEN commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 — bug)
**Impact on plan:** Fix required for any test to pass. RESEARCH.md Pattern 1 shows the correct API pattern; initial implementation deviated from it. No scope creep.

## Issues Encountered

- rapidfuzz `process.extract` dict dispatch path: when `choices` is a dict, rapidfuzz expects dict values to be strings (it matches against them). The correct pattern from RESEARCH.md is to pass `dict.keys()` and look up entity_id by name after matching.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- Plans 01-03 (formatter) and 01-04 (cache/pagination) can proceed in parallel — resolver is independent
- Phase 2 tool wiring can call `resolve(query, {eid: name for eid, name in dialogs})` directly
- Concern from STATE.md still open: transliterate for Ukrainian/Belarusian names — Latin↔Cyrillic cross-script matching is out of WRatio scope; document in resolver docstring when Phase 2 wires it

## Self-Check: PASSED

- `src/mcp_telegram/resolver.py` — confirmed present on disk
- `tests/test_resolver.py` — confirmed present with module-level imports
- Commits `564672a`, `ad16db8`, `11920e6` — confirmed in git log

---
*Phase: 01-support-modules*
*Completed: 2026-03-10*
