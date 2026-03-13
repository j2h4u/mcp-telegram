---
phase: 3
slug: new-tools
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-11
---

# Phase 3 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x with pytest-asyncio |
| **Config file** | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode = "auto" |
| **Quick run command** | `uv run pytest tests/test_tools.py -x -q` |
| **Full suite command** | `uv run pytest -x -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_tools.py -x -q`
- **After every plan wave:** Run `uv run pytest -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 3-01-01 | 01 | 0 | TOOL-08 | unit | `uv run pytest tests/test_tools.py -k "get_me" -x` | ❌ W0 | ⬜ pending |
| 3-01-02 | 01 | 0 | TOOL-08 | unit | `uv run pytest tests/test_tools.py -k "get_me_unauthenticated" -x` | ❌ W0 | ⬜ pending |
| 3-01-03 | 01 | 0 | TOOL-09 | unit | `uv run pytest tests/test_tools.py -k "get_user_info" -x` | ❌ W0 | ⬜ pending |
| 3-01-04 | 01 | 0 | TOOL-09 | unit | `uv run pytest tests/test_tools.py -k "get_user_info_not_found" -x` | ❌ W0 | ⬜ pending |
| 3-01-05 | 01 | 0 | TOOL-09 | unit | `uv run pytest tests/test_tools.py -k "get_user_info_ambiguous" -x` | ❌ W0 | ⬜ pending |
| 3-01-06 | 01 | 0 | TOOL-09 | unit | `uv run pytest tests/test_tools.py -k "get_user_info_resolver_prefix" -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_tools.py` — add test stubs for TOOL-08 (GetMe) and TOOL-09 (GetUserInfo)
- [ ] `tests/conftest.py` — extend or configure `mock_client` with `get_me` and `get_entity` stubs per-test

*Existing infrastructure covers all framework needs — pytest, asyncio_mode=auto, mock_client, mock_cache fixtures are in place.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| GetMe returns real account fields via live Telegram | TOOL-08 | Requires live session credentials | Run `uv run python -c "from mcp_telegram.tools import *; ..."` with valid session |
| GetUserInfo returns real common chats for known user | TOOL-09 | Requires live session + shared chat | Invoke GetUserInfo with a known contact name via MCP client |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
