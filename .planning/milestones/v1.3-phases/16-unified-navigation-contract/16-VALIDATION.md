---
phase: 16
slug: unified-navigation-contract
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-14
---

# Phase 16 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | `pytest` async/unit tests |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `test -f .planning/phases/16-unified-navigation-contract/16-RESEARCH.md && rg -n "Recommended Contract Direction|Recommended Plan Split|Validation Architecture|Phase 16 Is Ready For Planning Now" .planning/phases/16-unified-navigation-contract/16-RESEARCH.md && uv run pytest tests/test_capabilities.py -k "history or search or cursor or offset or navigation" -q && uv run pytest tests/test_tools.py -k "list_messages or search_messages or cursor or offset or from_beginning or topic" -q && uv run pytest tests/test_server.py -q` |
| **Full suite command** | `uv run pytest` |
| **Final verification command** | `uv run pytest tests/test_capabilities.py -k "history or search or cursor or offset or navigation" -q && uv run pytest tests/test_tools.py -k "list_messages or search_messages or cursor or offset or from_beginning or topic or telemetry" -q && uv run pytest tests/test_server.py tests/test_pagination.py tests/test_analytics.py -q && uv run cli.py list-tools && uv run pytest` |
| **Estimated runtime** | ~60 seconds quick, ~90 seconds full |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full suite command
- **Before `$gsd-verify-work`:** Run the final verification command
- **Max feedback latency:** 90 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 16-01-01 | 01 | 1 | NAV-01 | unit | `uv run pytest tests/test_capabilities.py -k "history or search or cursor or offset or navigation" -q` | ✅ | ⬜ pending |
| 16-01-02 | 01 | 1 | NAV-01 | contract | `uv run pytest tests/test_tools.py -k "schema or list_messages or search_messages or cursor or offset" -q && uv run pytest tests/test_server.py -q` | ✅ | ⬜ pending |
| 16-02-01 | 02 | 2 | NAV-01 | regression | `uv run pytest tests/test_tools.py -k "list_messages or from_beginning or cursor or topic" -q && uv run pytest tests/test_capabilities.py -k "history or topic" -q` | ✅ | ⬜ pending |
| 16-02-02 | 02 | 2 | NAV-02 | regression | `uv run pytest tests/test_tools.py -k "topic or ambiguous or deleted or inaccessible or sender" -q` | ✅ | ⬜ pending |
| 16-03-01 | 03 | 3 | NAV-01 | regression | `uv run pytest tests/test_tools.py -k "search_messages or offset or navigation" -q && uv run pytest tests/test_capabilities.py -k "search" -q` | ✅ | ⬜ pending |
| 16-03-02 | 03 | 3 | NAV-02 | privacy/runtime | `uv run pytest tests/test_server.py tests/test_pagination.py tests/test_analytics.py -q && uv run cli.py list-tools` | ✅ | ⬜ pending |
| 16-03-03 | 03 | 3 | NAV-01 | runtime | `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram && docker exec mcp-telegram /opt/venv/bin/python -c "import mcp_telegram.tools as t; print(hasattr(t, 'ListMessages'), hasattr(t, 'SearchMessages'))"` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 setup is required.
Existing pytest infrastructure, server reflection tests, and CLI tooling already cover the Phase 16
contract work.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Shared navigation vocabulary is genuinely coherent rather than two legacy concepts behind new labels | NAV-01 | Passing tests can miss conceptual drift if the public surface keeps split meanings under renamed fields | Inspect the final `ListMessages` and `SearchMessages` docstrings, reflected schemas, and output footers together and confirm callers learn one continuation model rather than separate read/search rules |
| Topic-scoped read output remains readable after the navigation change | NAV-02 | Human review is needed to confirm transcript readability and footer placement are still usable for LLMs | Run the relevant `ListMessages` topic fixtures and inspect the resulting text for preserved `[topic: ...]`, `Action:` guidance, and non-confusing navigation footer placement |
| Restarted runtime exposes the new schema instead of the previous container state | NAV-01 | This repo uses a long-lived runtime, so repo-local tests do not prove the active container is current | Rebuild/restart `mcp-telegram`, then compare local `uv run cli.py list-tools` output with the restarted runtime's exposed tool schema for `ListMessages` and `SearchMessages` |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent regression coverage
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
