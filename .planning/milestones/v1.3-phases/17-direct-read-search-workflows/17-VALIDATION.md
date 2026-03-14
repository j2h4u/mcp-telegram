---
phase: 17
slug: direct-read-search-workflows
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-14
---

# Phase 17 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | `pytest` async/unit tests |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `test -f .planning/phases/17-direct-read-search-workflows/17-RESEARCH.md && rg -n "Recommended Contract Direction|Recommended Plan Split|Validation Architecture|Phase 17 Is Ready For Planning Now" .planning/phases/17-direct-read-search-workflows/17-RESEARCH.md && uv run pytest tests/test_capabilities.py -k "history or search or direct or topic or navigation" -q && uv run pytest tests/test_tools.py -k "list_messages or search_messages or direct or topic or ambiguity or navigation" -q && uv run pytest tests/test_server.py -q` |
| **Full suite command** | `uv run pytest` |
| **Final verification command** | `uv run pytest tests/test_capabilities.py -k "history or search or direct or topic or navigation" -q && uv run pytest tests/test_tools.py -k "list_messages or search_messages or direct or topic or ambiguity or navigation or telemetry" -q && uv run pytest tests/test_server.py tests/test_analytics.py -q && uv run cli.py list-tools && uv run pytest` |
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
| 17-01-01 | 01 | 1 | FLOW-01 | unit | `uv run pytest tests/test_capabilities.py -k "history or direct or topic or navigation" -q` | ✅ | ⬜ pending |
| 17-01-02 | 01 | 1 | FLOW-01 | contract | `uv run pytest tests/test_tools.py -k "list_messages or direct or topic or ambiguity" -q` | ✅ | ⬜ pending |
| 17-02-01 | 02 | 2 | FLOW-01 | regression | `uv run pytest tests/test_tools.py -k "list_messages and (topic or unread or ambiguity or direct)" -q && uv run pytest tests/test_capabilities.py -k "history or topic" -q` | ✅ | ⬜ pending |
| 17-02-02 | 02 | 2 | FLOW-01 | schema | `uv run pytest tests/test_server.py -q && uv run cli.py list-tools` | ✅ | ⬜ pending |
| 17-03-01 | 03 | 3 | FLOW-02 | regression | `uv run pytest tests/test_capabilities.py -k "search or direct or navigation" -q && uv run pytest tests/test_tools.py -k "search_messages or hit or context or navigation or direct" -q` | ✅ | ⬜ pending |
| 17-03-02 | 03 | 3 | FLOW-02 | schema | `uv run pytest tests/test_server.py -q && uv run cli.py list-tools` | ✅ | ⬜ pending |
| 17-03-03 | 03 | 3 | FLOW-02 | privacy | `uv run pytest tests/test_analytics.py -q && uv run pytest tests/test_tools.py -k "search_messages or telemetry" -q` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 setup is required.
Existing pytest infrastructure, capability tests, contract tests, analytics checks, and local
reflection tooling already cover the Phase 17 workflow-shape work.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Exact-target read/search selectors feel like direct workflow lanes rather than alias clutter | FLOW-01, FLOW-02 | Passing tests can still leave the public contract conceptually messy if the new fields duplicate existing behavior without a clear caller story | Inspect the final `ListMessages` and `SearchMessages` docstrings plus reflected schemas and confirm the direct path is explicit, bounded, and not framed as a replacement for ambiguity-safe natural-name use |
| Forum-read readability remains intact after exact-target support lands | FLOW-01 | Human review is needed to confirm `[topic: ...]`, inline topic labels, `Action:` guidance, and `next_navigation` footer placement still read cleanly | Run representative forum read fixtures or manual calls for cross-topic, single-topic, deleted-topic, and inaccessible-topic scenarios and inspect the resulting text |
| Search hit windows remain easy for an LLM to parse after workflow assembly moves | FLOW-02 | Tests can prove presence of markers and context but not whether the grouped output still reads naturally | Run representative search fixtures or manual calls and inspect `--- hit N/M ---`, `[HIT]`, before/after context ordering, and empty-state wording together |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent regression coverage
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
