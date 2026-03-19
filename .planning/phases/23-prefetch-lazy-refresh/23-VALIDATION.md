---
phase: 23
slug: prefetch-lazy-refresh
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 23 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest (asyncio_mode = "auto") |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_prefetch.py tests/test_capability_history.py -x -q` |
| **Full suite command** | `uv run pytest tests/ -x -q` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_prefetch.py tests/test_capability_history.py -x -q`
- **After every plan wave:** Run `uv run pytest tests/ -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 23-01-01 | 01 | 0 | PRE-01 | integration | `uv run pytest tests/test_prefetch.py::test_first_page_schedules_dual_prefetch -x` | ❌ W0 | ⬜ pending |
| 23-01-02 | 01 | 0 | PRE-02 | integration | `uv run pytest tests/test_prefetch.py::test_subsequent_page_schedules_next_prefetch -x` | ❌ W0 | ⬜ pending |
| 23-01-03 | 01 | 0 | PRE-03 | integration | `uv run pytest tests/test_prefetch.py::test_oldest_page_triggers_forward_prefetch -x` | ❌ W0 | ⬜ pending |
| 23-01-04 | 01 | 0 | PRE-04 | unit | `uv run pytest tests/test_prefetch.py::test_prefetch_task_stores_messages -x` | ❌ W0 | ⬜ pending |
| 23-01-05 | 01 | 0 | PRE-05 | unit | `uv run pytest tests/test_prefetch.py::test_dedup_suppresses_duplicate_schedule -x` | ❌ W0 | ⬜ pending |
| 23-01-06 | 01 | 0 | REF-01 | integration | `uv run pytest tests/test_prefetch.py::test_cache_hit_triggers_delta_refresh -x` | ❌ W0 | ⬜ pending |
| 23-01-07 | 01 | 0 | REF-02 | unit | `uv run pytest tests/test_prefetch.py::test_delta_refresh_uses_min_id -x` | ❌ W0 | ⬜ pending |
| 23-01-08 | 01 | 0 | REF-03 | unit | `uv run pytest tests/test_prefetch.py::test_no_background_timer_refresh -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_prefetch.py` — stubs for PRE-01 through PRE-05, REF-01 through REF-03
- [ ] `src/mcp_telegram/prefetch.py` — PrefetchCoordinator class skeleton (test harness needs it to import)

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
