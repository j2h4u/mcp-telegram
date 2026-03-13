---
phase: 5
slug: cache-error-hardening
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-11
---

# Phase 5 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x + pytest-asyncio 1.3.x |
| **Config file** | `pyproject.toml` — `[tool.pytest.ini_options]` asyncio_mode="auto" |
| **Quick run command** | `~/.local/bin/uv run pytest tests/test_cache.py tests/test_tools.py -x -q` |
| **Full suite command** | `~/.local/bin/uv run pytest tests/ -q` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `~/.local/bin/uv run pytest tests/test_cache.py tests/test_tools.py -x -q`
- **After every plan wave:** Run `~/.local/bin/uv run pytest tests/ -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** ~5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 5-01-01 | 01 | 0 | CACH-01 | unit | `~/.local/bin/uv run pytest tests/test_cache.py -k "ttl" -x` | ❌ W0 | ⬜ pending |
| 5-01-02 | 01 | 0 | CACH-01 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "stale" -x` | ❌ W0 | ⬜ pending |
| 5-01-03 | 01 | 0 | CACH-02 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_upsert" -x` | ❌ W0 | ⬜ pending |
| 5-01-04 | 01 | 0 | TOOL-03 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "cursor_error" -x` | ❌ W0 | ⬜ pending |
| 5-02-01 | 02 | 1 | CACH-01 | unit | `~/.local/bin/uv run pytest tests/test_cache.py -k "ttl" -x` | ✅ W0 | ⬜ pending |
| 5-02-02 | 02 | 1 | CACH-02 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "search_upsert" -x` | ✅ W0 | ⬜ pending |
| 5-02-03 | 02 | 1 | TOOL-03 | unit | `~/.local/bin/uv run pytest tests/test_tools.py -k "cursor_error" -x` | ✅ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cache.py` — new test: `test_all_names_with_ttl_excludes_stale` — covers CACH-01; use existing monkeypatch time pattern
- [ ] `tests/test_cache.py` — new test: `test_all_names_with_ttl_user_vs_group_different_ttl` — verifies user/group TTLs applied independently
- [ ] `tests/test_tools.py` — new test: `test_list_messages_stale_entity_excluded` — verifies TTL-filtered resolver call in `list_messages`
- [ ] `tests/test_tools.py` — new test: `test_search_messages_upserts_sender` — verifies `cache.upsert` called for hit message sender
- [ ] `tests/test_tools.py` — new test: `test_list_messages_invalid_cursor_returns_error` — verifies friendly TextContent on bad cursor

*Existing test infrastructure fully covers all test types — no framework installs required.*

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
