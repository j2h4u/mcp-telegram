---
phase: 4
slug: search-context-window
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-11
---

# Phase 4 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + pytest-asyncio 1.3.x |
| **Config file** | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode="auto" |
| **Quick run command** | `~/.local/bin/uv run pytest tests/test_tools.py -x -q` |
| **Full suite command** | `~/.local/bin/uv run pytest tests/ -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `~/.local/bin/uv run pytest tests/test_tools.py -x -q`
- **After every plan wave:** Run `~/.local/bin/uv run pytest tests/ -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 04-01-01 | 01 | 0 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_context_window" -x` | ❌ W0 | ⬜ pending |
| 04-01-02 | 01 | 0 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_context_after_hit" -x` | ❌ W0 | ⬜ pending |
| 04-01-03 | 01 | 0 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_hit_marker" -x` | ❌ W0 | ⬜ pending |
| 04-01-04 | 01 | 0 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages_reaction_names" -x` | ❌ W0 | ⬜ pending |
| 04-01-05 | 01 | 0 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/test_tools.py::test_search_messages_context -x` | ✅ update | ⬜ pending |
| 04-02-01 | 02 | 1 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_messages" -x -q` | ✅ W0 | ⬜ pending |
| 04-02-02 | 02 | 1 | TOOL-06 | unit | `~/.local/bin/uv run pytest tests/ -q` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_tools.py` — new test `test_search_messages_context_window`: asserts 3 messages before hit appear in output (TOOL-06)
- [ ] `tests/test_tools.py` — new test `test_search_messages_context_after_hit`: asserts 3 messages after hit appear in output (TOOL-06)
- [ ] `tests/test_tools.py` — new test `test_search_messages_hit_marker`: asserts hit message line is visually distinct (TOOL-06)
- [ ] `tests/test_tools.py` — new test `test_search_messages_reaction_names_fetched`: asserts reactions fetched for hits (TOOL-06)
- [ ] `tests/test_tools.py` — update existing `test_search_messages_context`: add `mock_client.get_messages = AsyncMock(return_value=[])` to handle context fetch mock

---

## Manual-Only Verifications

*All phase behaviors have automated verification.*

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
