---
phase: 20
slug: cache-foundation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-20
---

# Phase 20 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` |
| **Quick run command** | `uv run pytest tests/test_cache.py -x -q` |
| **Full suite command** | `uv run pytest -x -q` |
| **Estimated runtime** | ~8 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_cache.py -x -q`
- **After every plan wave:** Run `uv run pytest -x -q`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 20-01-01 | 01 | 1 | CACHE-01 | unit | `uv run pytest tests/test_cache.py -k "message_cache" -x` | ❌ W0 | ⬜ pending |
| 20-01-02 | 01 | 1 | CACHE-01 | unit | `uv run pytest tests/test_cache.py -k "message_cache_pk" -x` | ❌ W0 | ⬜ pending |
| 20-01-03 | 01 | 1 | CACHE-01 | unit | `uv run pytest tests/test_cache.py -k "message_cache_index" -x` | ❌ W0 | ⬜ pending |
| 20-01-04 | 01 | 1 | CACHE-07 | unit | `uv run pytest tests/test_cache.py -k "same_db" -x` | ❌ W0 | ⬜ pending |
| 20-02-01 | 02 | 1 | CACHE-02 | unit | `uv run pytest tests/test_cache.py -k "cached_message" -x` | ❌ W0 | ⬜ pending |
| 20-02-02 | 02 | 1 | CACHE-02 | unit | `uv run pytest tests/test_formatter.py -k "cached_message" -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cache.py` — new tests for `message_cache` schema (CACHE-01), same-DB check (CACHE-07), `CachedMessage` round-trip (CACHE-02). Add to existing file.
- [ ] `tests/test_formatter.py` — one smoke test: `format_messages([CachedMessage(...)], {})` returns non-empty string (CACHE-02 transparency guarantee)

*Existing `tests/conftest.py` fixtures: `tmp_db_path` is directly reusable; no new fixtures needed for schema tests.*

---

## Manual-Only Verifications

All phase behaviors have automated verification.

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
