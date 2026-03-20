---
phase: 22
slug: edit-detection
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 22 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + pytest-asyncio |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_cache.py tests/test_formatter.py -x --tb=short -q` |
| **Full suite command** | `uv run pytest -x --tb=short -q` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_cache.py tests/test_formatter.py -x --tb=short -q`
- **After every plan wave:** Run `uv run pytest -x --tb=short -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 22-01-01 | 01 | 0 | EDIT-02 | unit | `uv run pytest tests/test_cache.py -k "edit_detection or version" -x` | ❌ W0 | ⬜ pending |
| 22-01-02 | 01 | 0 | EDIT-03 | unit | `uv run pytest tests/test_formatter.py -k "edited" -x` | ❌ W0 | ⬜ pending |
| 22-01-03 | 01 | 1 | EDIT-02 | unit | `uv run pytest tests/test_cache.py -k "edit_detection or version" -x` | ❌ W0 | ⬜ pending |
| 22-01-04 | 01 | 1 | EDIT-03 | unit | `uv run pytest tests/test_formatter.py -k "edited" -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cache.py` — add `test_store_messages_records_version_on_text_change`, `test_store_messages_no_version_on_unchanged_text`, `test_store_messages_no_version_on_first_store`
- [ ] `tests/test_formatter.py` — add `test_edited_marker_shown_when_edit_date_set` (int and datetime variants), `test_edited_marker_absent_when_edit_date_none`

*Existing infrastructure covers framework install — append to existing test modules.*

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
