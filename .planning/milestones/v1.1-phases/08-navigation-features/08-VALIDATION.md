---
phase: 8
slug: navigation-features
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-12
---

# Phase 8 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2+ with pytest-asyncio 1.3.0+ |
| **Config file** | `pyproject.toml` (asyncio_mode = "auto") |
| **Quick run command** | `pytest tests/test_tools.py -v` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~10 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_tools.py -v`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 8-01-01 | 01 | 1 | NAV-01 | unit | `pytest tests/test_tools.py -k reverse -v` | ❌ W0 | ⬜ pending |
| 8-01-02 | 01 | 1 | NAV-01 | integration | `pytest tests/test_tools.py::test_list_messages_from_beginning -v` | ❌ W0 | ⬜ pending |
| 8-01-03 | 01 | 1 | NAV-01 | integration | `pytest tests/test_tools.py::test_list_messages_reverse_pagination_cursor -v` | ❌ W0 | ⬜ pending |
| 8-02-01 | 02 | 1 | NAV-02 | unit | `pytest tests/test_tools.py::test_list_dialogs_archived_default -v` | ❌ W0 | ⬜ pending |
| 8-02-02 | 02 | 1 | NAV-02 | unit | `pytest tests/test_tools.py::test_list_dialogs_exclude_archived -v` | ❌ W0 | ⬜ pending |
| 8-02-03 | 02 | 1 | NAV-02 | integration | `pytest tests/test_cache.py -k archive -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_tools.py` — add stubs for NAV-01 reverse pagination tests
- [ ] `tests/test_tools.py` — add stubs for NAV-02 archived dialogs tests
- [ ] `tests/test_cache.py` — add stub for archived entity cache test

*Existing pytest-asyncio infrastructure covers all phase requirements; only test stubs needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Entity cache populated by archived chats end-to-end | NAV-02 | Requires live Telegram session with archived chats | Run `ListDialogs`, then attempt `ListMessages` on previously-unknown archived chat; verify no "contact not found" error |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
