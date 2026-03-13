---
phase: 1
slug: support-modules
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-11
---

# Phase 1 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest ≥8.0 + pytest-asyncio ≥0.23 |
| **Config file** | `pyproject.toml` `[tool.pytest.ini_options]` — Wave 0 gap |
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
| 1-01-01 | 01 | 0 | RES-01 | unit | `uv run pytest tests/test_resolver.py -x` | ❌ W0 | ⬜ pending |
| 1-01-02 | 01 | 0 | RES-01 | unit | `uv run pytest tests/test_resolver.py::test_numeric_query -x` | ❌ W0 | ⬜ pending |
| 1-01-03 | 01 | 0 | RES-01 | unit | `uv run pytest tests/test_resolver.py::test_ambiguity -x` | ❌ W0 | ⬜ pending |
| 1-01-04 | 01 | 0 | RES-02 | unit | `uv run pytest tests/test_resolver.py::test_sender_resolution -x` | ❌ W0 | ⬜ pending |
| 1-02-01 | 02 | 1 | FMT-01 | unit | `uv run pytest tests/test_formatter.py::test_basic_format -x` | ❌ W0 | ⬜ pending |
| 1-02-02 | 02 | 1 | FMT-01 | unit | `uv run pytest tests/test_formatter.py::test_date_header -x` | ❌ W0 | ⬜ pending |
| 1-02-03 | 02 | 1 | FMT-01 | unit | `uv run pytest tests/test_formatter.py::test_session_break -x` | ❌ W0 | ⬜ pending |
| 1-03-01 | 03 | 1 | CACH-01 | unit | `uv run pytest tests/test_cache.py::test_persistence -x` | ❌ W0 | ⬜ pending |
| 1-03-02 | 03 | 1 | CACH-01 | unit | `uv run pytest tests/test_cache.py::test_ttl_expiry -x` | ❌ W0 | ⬜ pending |
| 1-03-03 | 03 | 1 | CACH-02 | unit | `uv run pytest tests/test_cache.py::test_upsert_update -x` | ❌ W0 | ⬜ pending |
| 1-03-04 | 03 | 1 | CACH-01 | unit | `uv run pytest tests/test_cache.py::test_cross_process -x` | ❌ W0 | ⬜ pending |
| 1-04-01 | 04 | 1 | (cursor) | unit | `uv run pytest tests/test_pagination.py::test_round_trip -x` | ❌ W0 | ⬜ pending |
| 1-04-02 | 04 | 1 | (cursor) | unit | `uv run pytest tests/test_pagination.py::test_cross_dialog_error -x` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/__init__.py` — make tests a package
- [ ] `tests/conftest.py` — shared fixtures: tmp SQLite path, sample entity dict, sample message list
- [ ] `tests/test_resolver.py` — stubs for RES-01, RES-02
- [ ] `tests/test_formatter.py` — stubs for FMT-01
- [ ] `tests/test_cache.py` — stubs for CACH-01, CACH-02
- [ ] `tests/test_pagination.py` — stubs for cursor success and cross-dialog error
- [ ] `pyproject.toml` `[tool.pytest.ini_options]` — `asyncio_mode = "auto"` if async needed
- [ ] `uv add --dev pytest pytest-asyncio` — framework install
- [ ] `uv add rapidfuzz` — library install

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Cyrillic hyphenated name match quality | RES-01 | Edge case depends on rapidfuzz internals; needs live data judgment | Query "Иванов-Петров" against cache with that name; check if score is ≥90 |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 5s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
