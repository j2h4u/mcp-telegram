---
phase: 6
slug: telemetry-foundation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-12
---

# Phase 6 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| **Config file** | pyproject.toml `[tool.pytest.ini_options]` |
| **Quick run command** | `pytest tests/test_analytics.py -v -x` |
| **Full suite command** | `pytest tests/ -v` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/test_analytics.py -v -x`
- **After every plan wave:** Run `pytest tests/ -v && bash tests/privacy_audit.sh`
- **Before `/gsd:verify-work`:** Full suite must be green + privacy audit passing
- **Max feedback latency:** 15 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 6-01-01 | 01 | 0 | TEL-01 | unit | `pytest tests/test_analytics.py::test_analytics_db_created -xvs` | ❌ W0 | ⬜ pending |
| 6-01-02 | 01 | 0 | TEL-01 | unit | `pytest tests/test_analytics.py::test_record_event_nonblocking -xvs` | ❌ W0 | ⬜ pending |
| 6-01-03 | 01 | 0 | TEL-01 | unit | `pytest tests/test_analytics.py::test_async_flush_writes_db -xvs` | ❌ W0 | ⬜ pending |
| 6-02-01 | 02 | 1 | TEL-04 | unit | `pytest tests/test_tools.py::test_list_dialogs_records_telemetry -xvs` | ❌ W0 | ⬜ pending |
| 6-02-02 | 02 | 1 | TEL-04 | unit | `pytest tests/test_tools.py::test_list_messages_records_telemetry -xvs` | ❌ W0 | ⬜ pending |
| 6-02-03 | 02 | 1 | TEL-04 | unit | `pytest tests/test_tools.py::test_search_messages_records_telemetry -xvs` | ❌ W0 | ⬜ pending |
| 6-02-04 | 02 | 1 | TEL-04 | unit | `pytest tests/test_tools.py::test_get_me_records_telemetry -xvs` | ❌ W0 | ⬜ pending |
| 6-02-05 | 02 | 1 | TEL-04 | unit | `pytest tests/test_tools.py::test_get_user_info_records_telemetry -xvs` | ❌ W0 | ⬜ pending |
| 6-02-06 | 02 | 1 | TEL-04 | unit | `pytest tests/test_tools.py::test_get_usage_stats_not_recorded -xvs` | ❌ W0 | ⬜ pending |
| 6-03-01 | 03 | 1 | TEL-02 | unit | `pytest tests/test_tools.py::test_get_usage_stats_under_100_tokens -xvs` | ❌ W0 | ⬜ pending |
| 6-03-02 | 03 | 1 | TEL-02 | unit | `pytest tests/test_analytics.py::test_usage_summary_metrics -xvs` | ❌ W0 | ⬜ pending |
| 6-04-01 | 04 | 2 | TEL-03 | integration | `bash tests/privacy_audit.sh` | ❌ W0 | ⬜ pending |
| 6-04-02 | 04 | 2 | LOAD | load | `pytest tests/test_load.py::test_telemetry_load_baseline -xvs` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_analytics.py` — TelemetryCollector unit tests: schema validation, async flush behavior, non-blocking record
- [ ] `tests/privacy_audit.sh` — Grep-based PII audit (entity_id, dialog_id, sender_id, username patterns in analytics.py)
- [ ] `tests/test_load.py` — Load test baseline: 100 concurrent ListMessages, measure p95 latency with/without telemetry
- [ ] Mock TelemetryCollector in existing `tests/test_tools.py` fixtures to avoid DB side effects

*Framework already installed: pytest + pytest-asyncio in pyproject.toml dev dependencies.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| GetUsageStats output reads naturally to a human | TEL-02 | Subjective quality — "actionable patterns" | Call tool after 10+ real interactions, evaluate if summary is meaningful |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 15s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
