---
phase: 14
slug: boundary-recovery
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-14
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | `pytest` async/unit tests plus restarted-runtime verification |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `test -f .planning/phases/14-boundary-recovery/14-RESEARCH.md && rg -n "server.call_tool|Recommended Plan Split|Validation Architecture|Phase 14 Is Ready For Planning Now" .planning/phases/14-boundary-recovery/14-RESEARCH.md && ( test ! -f tests/test_server.py || uv run pytest tests/test_server.py -q ) && uv run pytest tests/test_tools.py -k "tool_records_telemetry_on_error or get_user_info_fetch_error_returns_action or list_messages_invalid_cursor_returns_error" -q` |
| **Full suite command** | `uv run pytest` |
| **Final verification command** | `uv run pytest tests/test_server.py -q && uv run pytest tests/test_tools.py -k "tool_records_telemetry_on_error or get_user_info_fetch_error_returns_action or list_messages_invalid_cursor_returns_error" -q && uv run pytest` |
| **Estimated runtime** | ~30 seconds quick, ~90 seconds full |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full suite command
- **Before `$gsd-verify-work`:** Run the final verification command and the restarted-runtime check
- **Max feedback latency:** 90 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 14-01-01 | 01 | 1 | ERR-01 | unit | `uv run pytest tests/test_server.py -k "validation or escaped" -q` | ❌ W1 | ⬜ pending |
| 14-01-02 | 01 | 1 | ERR-01 | unit | `uv run pytest tests/test_server.py -k "passthrough or contract" -q` | ❌ W1 | ⬜ pending |
| 14-02-01 | 02 | 2 | ERR-01 | unit | `uv run pytest tests/test_server.py -q` | ❌ W2 | ⬜ pending |
| 14-02-02 | 02 | 2 | ERR-01 | regression | `uv run pytest tests/test_tools.py -k "tool_records_telemetry_on_error or get_user_info_fetch_error_returns_action or list_messages_invalid_cursor_returns_error" -q && uv run pytest tests/test_server.py -q` | ❌ W2 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 stubs are required.
Existing pytest infrastructure is sufficient for the Phase 14 boundary and regression coverage.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Restarted runtime returns actionable escaped-error detail instead of only `Tool <name> failed` | ERR-01 | The live container must be rebuilt/restarted to prove runtime-visible boundary behavior, and the exact failure trigger is environment-specific | Rebuild and restart `mcp-telegram`, trigger one known escaped failure through the live runtime, and confirm the returned error identifies the tool and failing stage with a next-step hint |
| Logs remain privacy-safe while boundary detail improves | ERR-01 | Human review is needed to confirm error text and logs do not start including message content, usernames, or raw payloads | Inspect the container logs produced by the failure trigger and confirm they contain diagnostic context without Telegram message content or identifying payloads |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent regression coverage
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
