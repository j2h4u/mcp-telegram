---
phase: 2
slug: tool-updates
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-11
---

# Phase 2 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-asyncio (asyncio_mode=auto) |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` — already exists |
| **Quick run command** | `uv run pytest tests/ -x -q` |
| **Full suite command** | `uv run pytest tests/ -v` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/ -x -q`
- **After every plan wave:** Run `uv run pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 2-01-01 | 01 | 0 | TOOL-01 | unit | `uv run pytest tests/test_tools.py::test_list_dialogs_type_field -x` | ❌ W0 | ⬜ pending |
| 2-01-02 | 01 | 0 | TOOL-01 | unit | `uv run pytest tests/test_tools.py::test_list_dialogs_null_date -x` | ❌ W0 | ⬜ pending |
| 2-01-03 | 01 | 0 | TOOL-02 | unit | `uv run pytest tests/test_tools.py::test_list_messages_by_name -x` | ❌ W0 | ⬜ pending |
| 2-01-04 | 01 | 0 | TOOL-02 | unit | `uv run pytest tests/test_tools.py::test_list_messages_not_found -x` | ❌ W0 | ⬜ pending |
| 2-01-05 | 01 | 0 | TOOL-02 | unit | `uv run pytest tests/test_tools.py::test_list_messages_ambiguous -x` | ❌ W0 | ⬜ pending |
| 2-01-06 | 01 | 0 | TOOL-03 | unit | `uv run pytest tests/test_tools.py::test_list_messages_cursor_present -x` | ❌ W0 | ⬜ pending |
| 2-01-07 | 01 | 0 | TOOL-03 | unit | `uv run pytest tests/test_tools.py::test_list_messages_no_cursor_last_page -x` | ❌ W0 | ⬜ pending |
| 2-01-08 | 01 | 0 | TOOL-04 | unit | `uv run pytest tests/test_tools.py::test_list_messages_sender_filter -x` | ❌ W0 | ⬜ pending |
| 2-01-09 | 01 | 0 | TOOL-05 | unit | `uv run pytest tests/test_tools.py::test_list_messages_unread_filter -x` | ❌ W0 | ⬜ pending |
| 2-01-10 | 01 | 0 | TOOL-06 | unit | `uv run pytest tests/test_tools.py::test_search_messages_context -x` | ❌ W0 | ⬜ pending |
| 2-01-11 | 01 | 0 | TOOL-07 | unit | `uv run pytest tests/test_tools.py::test_search_messages_next_offset -x` | ❌ W0 | ⬜ pending |
| 2-01-12 | 01 | 0 | TOOL-07 | unit | `uv run pytest tests/test_tools.py::test_search_messages_no_next_offset -x` | ❌ W0 | ⬜ pending |
| 2-01-13 | 01 | 0 | CLNP-01 | unit | `uv run pytest tests/test_tools.py::test_get_dialog_removed -x` | ❌ W0 | ⬜ pending |
| 2-01-14 | 01 | 0 | CLNP-02 | unit | `uv run pytest tests/test_tools.py::test_get_message_removed -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_tools.py` — 14 stub tests covering TOOL-01 through TOOL-07, CLNP-01, CLNP-02
- [ ] `tests/conftest.py` — `mock_cache` fixture (EntityCache seeded with sample data); mock Telethon client helpers

*Existing test files `test_resolver.py`, `test_formatter.py`, `test_cache.py`, `test_pagination.py` already pass — no changes needed.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| `ListMessages` with `sender` + cursor returns correct pages | TOOL-03 + TOOL-04 | `from_user` switches to Search API backend; pagination interaction requires live session | Call ListMessages with known sender + cursor; verify page 2 has no overlap with page 1 |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
