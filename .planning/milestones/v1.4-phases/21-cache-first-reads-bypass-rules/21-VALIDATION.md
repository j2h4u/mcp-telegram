---
phase: 21
slug: cache-first-reads-bypass-rules
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 21 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 8.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `uv run pytest tests/test_cache.py tests/test_capability_history.py -x -q` |
| **Full suite command** | `uv run pytest -x -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_cache.py tests/test_capability_history.py -x -q`
- **After every plan wave:** Run `uv run pytest -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 21-01-01 | 01 | 1 | CACHE-05 | unit | `uv run pytest tests/test_cache.py -k "store_messages" -x -q` | ❌ W0 | ⬜ pending |
| 21-01-02 | 01 | 1 | CACHE-06 | unit | `uv run pytest tests/test_cache.py -k "pragma_optimize" -x -q` | ❌ W0 | ⬜ pending |
| 21-02-01 | 02 | 1 | CACHE-03, CACHE-04 | unit | `uv run pytest tests/test_cache.py -k "try_read_page" -x -q` | ❌ W0 | ⬜ pending |
| 21-02-02 | 02 | 1 | BYP-01, BYP-02 | unit | `uv run pytest tests/test_cache.py -k "should_try_cache" -x -q` | ❌ W0 | ⬜ pending |
| 21-03-01 | 03 | 2 | CACHE-03 | integration | `uv run pytest tests/test_capability_history.py -k "cache_hit" -x -q` | ❌ W0 | ⬜ pending |
| 21-03-02 | 03 | 2 | BYP-01, BYP-02, BYP-03 | integration | `uv run pytest tests/test_capability_history.py -k "bypass" -x -q` | ❌ W0 | ⬜ pending |
| 21-03-03 | 03 | 2 | BYP-04 | integration | `uv run pytest tests/test_capability_history.py -k "search_populates" -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cache.py` — stubs for MessageCache.store_messages, try_read_page, PRAGMA optimize
- [ ] `tests/test_capability_history.py` — stubs for cache hit/miss, bypass rules, search population

*Existing test infrastructure (pytest, conftest.py) covers framework needs.*

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
