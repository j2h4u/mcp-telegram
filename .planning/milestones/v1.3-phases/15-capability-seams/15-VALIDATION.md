---
phase: 15
slug: capability-seams
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-14
---

# Phase 15 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | `pytest` async/unit tests |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `test -f .planning/phases/15-capability-seams/15-RESEARCH.md && rg -n "Recommended Capability Boundaries|Recommended Plan Split|Validation Architecture|Phase 15 Is Ready For Planning Now" .planning/phases/15-capability-seams/15-RESEARCH.md && ( test ! -f tests/test_capabilities.py || uv run pytest tests/test_capabilities.py -q ) && uv run pytest tests/test_tools.py -k "list_topics or list_messages or search_messages or topic or cursor or offset" -q && uv run pytest tests/test_pagination.py tests/test_cache.py -q` |
| **Full suite command** | `uv run pytest` |
| **Final verification command** | `uv run pytest tests/test_capabilities.py -q && uv run pytest tests/test_tools.py -k "list_topics or list_messages or search_messages or topic or cursor or offset or reaction or telemetry" -q && uv run pytest tests/test_pagination.py tests/test_cache.py -q && uv run pytest` |
| **Estimated runtime** | ~45 seconds quick, ~90 seconds full |

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
| 15-01-01 | 01 | 1 | CAP-01 | unit | `uv run pytest tests/test_capabilities.py -q` | ❌ W1 | ⬜ pending |
| 15-01-02 | 01 | 1 | CAP-01 | regression | `uv run pytest tests/test_tools.py -k "list_topics or list_messages or search_messages" -q && uv run pytest tests/test_pagination.py tests/test_cache.py -q` | ✅ | ⬜ pending |
| 15-02-01 | 02 | 2 | CAP-01 | unit | `uv run pytest tests/test_capabilities.py -q && uv run pytest tests/test_tools.py -k "topic or cursor or unread or from_beginning" -q` | ✅ | ⬜ pending |
| 15-02-02 | 02 | 2 | CAP-01 | regression | `uv run pytest tests/test_tools.py -k "list_topics or list_messages" -q && uv run pytest tests/test_pagination.py tests/test_cache.py -q` | ✅ | ⬜ pending |
| 15-03-01 | 03 | 3 | CAP-01 | unit | `uv run pytest tests/test_tools.py -k "search_messages or offset or reaction" -q` | ✅ | ⬜ pending |
| 15-03-02 | 03 | 3 | CAP-01 | regression | `uv run pytest tests/test_capabilities.py -q && uv run pytest tests/test_tools.py -k "list_topics or list_messages or search_messages or topic or cursor or offset or reaction or telemetry" -q && uv run pytest tests/test_pagination.py tests/test_cache.py -q` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 setup is required.
Existing pytest infrastructure and brownfield fixtures already cover the Phase 15 seam work.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Public tool adapters remain inspectably thin after extraction | CAP-01 | Code review is needed to confirm the seam is visible in structure, not only inferred from passing behavior tests | Inspect the final `ListTopics`, `ListMessages`, and `SearchMessages` entrypoints and confirm they primarily delegate to shared capability helpers/modules rather than re-owning orchestration logic |
| Restarted runtime reflects the latest Phase 15 code after every runtime-affecting plan | CAP-01 | This repo uses a long-lived container, so green tests do not prove the active runtime is current | After each plan that changes `src/`, run `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram` and then verify inside the container that the updated code imports and the expected tool surface is still present |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent regression coverage
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
