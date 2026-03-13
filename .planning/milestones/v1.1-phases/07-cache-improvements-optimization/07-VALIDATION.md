---
phase: 7
slug: cache-improvements-optimization
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-12
---

# Phase 7 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest |
| **Config file** | pytest.ini or pyproject.toml |
| **Quick run command** | `pytest tests/ -x -q` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~30 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/ -x -q`
- **After every plan wave:** Run `pytest tests/ -v`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 30 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 7-01-01 | 01 | 1 | CACHE-01 | unit | `pytest tests/test_cache.py -x -q -k "index"` | ❌ W0 | ⬜ pending |
| 7-01-02 | 01 | 1 | CACHE-01 | integration | `pytest tests/test_cache.py -x -q -k "explain_query"` | ❌ W0 | ⬜ pending |
| 7-02-01 | 02 | 1 | CACHE-02 | unit | `pytest tests/test_cache.py -x -q -k "ttl"` | ❌ W0 | ⬜ pending |
| 7-02-02 | 02 | 1 | CACHE-02 | unit | `pytest tests/test_cache.py -x -q -k "invalidation"` | ❌ W0 | ⬜ pending |
| 7-03-01 | 03 | 2 | CACHE-03 | integration | `pytest tests/test_cleanup.py -x -q` | ❌ W0 | ⬜ pending |
| 7-03-02 | 03 | 2 | CACHE-03 | load | `pytest tests/test_load.py -x -q -k "concurrent_list_messages"` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cache.py` — stubs for CACHE-01 (index verification), CACHE-02 (TTL/invalidation)
- [ ] `tests/test_cleanup.py` — stubs for CACHE-03 (retention/vacuum)
- [ ] `tests/test_load.py` — load test for concurrent ListMessages (p95 <250ms)
- [ ] `tests/conftest.py` — shared fixtures (in-memory SQLite, mock telethon client)

*Existing pytest infrastructure may partially cover these; Wave 0 fills any gaps.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Daily systemd timer fires correctly | CACHE-03 | Requires systemd environment | `systemctl status mcp-telegram-cleanup.timer` + check logs after trigger |
| Dialog list always fresh on ListDialogs | CACHE-02 | Requires live Telegram session | Call ListDialogs twice, verify no stale data |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 30s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
