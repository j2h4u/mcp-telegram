---
phase: 18
slug: surface-posture-rollout-proof
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-03-14
---

# Phase 18 - Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | `pytest` async/unit tests plus shell-based privacy audit and reflected schema checks |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `test -f .planning/phases/18-surface-posture-rollout-proof/18-RESEARCH.md && rg -n "Summary|Validation Architecture|Recommended Phase Shape|RESEARCH COMPLETE" .planning/phases/18-surface-posture-rollout-proof/18-RESEARCH.md && uv run pytest tests/test_server.py -q && uv run pytest tests/test_tools.py -k "list_messages or search_messages or list_dialogs or list_topics or get_my_account or get_user_info or get_usage_stats or telemetry or schema or posture" -q && uv run pytest tests/test_analytics.py -q && bash tests/privacy_audit.sh` |
| **Full suite command** | `uv run pytest` |
| **Final verification command** | `uv run pytest tests/test_server.py tests/test_analytics.py -q && uv run pytest tests/test_tools.py -k "list_messages or search_messages or list_dialogs or list_topics or get_my_account or get_user_info or get_usage_stats or telemetry or schema or posture" -q && bash tests/privacy_audit.sh && UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools && docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram && docker exec mcp-telegram /opt/venv/bin/python -c "import json; from mcp_telegram.tools import GetMyAccount, GetUsageStats, GetUserInfo, ListDialogs, ListMessages, ListTopics, SearchMessages, tool_description; tool_types = {'GetMyAccount': GetMyAccount, 'GetUsageStats': GetUsageStats, 'GetUserInfo': GetUserInfo, 'ListDialogs': ListDialogs, 'ListMessages': ListMessages, 'ListTopics': ListTopics, 'SearchMessages': SearchMessages}; print(json.dumps({name: {'description': tool_description(tool_types[name]).description, 'schema': tool_description(tool_types[name]).inputSchema} for name in sorted(tool_types)}, ensure_ascii=True, sort_keys=True))"` |
| **Estimated runtime** | ~75 seconds quick, ~120 seconds full, plus container rebuild time for final verification |

---

## Sampling Rate

- **After every task commit:** Run the quick command
- **After every plan wave:** Run the full suite command
- **Before `$gsd-verify-work`:** Run the final verification command
- **Max feedback latency:** 120 seconds before a repo-local signal; container rebuild runs only at the final rollout gate

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 18-01-01 | 01 | 1 | SURF-01 | reflection | `uv run pytest tests/test_server.py -k "list_messages or search_messages or get_user_info or posture or description" -q` | ✅ | ⬜ pending |
| 18-01-02 | 01 | 1 | SURF-01 | regression | `uv run pytest tests/test_tools.py -k "list_messages or search_messages or list_dialogs or list_topics or get_my_account or get_user_info or get_usage_stats or posture or schema" -q` | ✅ | ⬜ pending |
| 18-01-03 | 01 | 1 | SURF-01 | local-reflection | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` | ✅ | ⬜ pending |
| 18-02-01 | 02 | 2 | ROLL-01 | contract | `uv run pytest tests/test_server.py -q && uv run pytest tests/test_tools.py -k "schema or posture or list_messages or search_messages or list_dialogs or list_topics or get_user_info" -q` | ✅ | ⬜ pending |
| 18-02-02 | 02 | 2 | ROLL-02 | privacy | `uv run pytest tests/test_analytics.py -q && bash tests/privacy_audit.sh` | ✅ | ⬜ pending |
| 18-02-03 | 02 | 2 | ROLL-01 | local-reflection | `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` | ✅ | ⬜ pending |
| 18-03-01 | 03 | 3 | ROLL-01 | runtime | `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram && docker exec mcp-telegram /opt/venv/bin/python -c "import json; from mcp_telegram.tools import GetMyAccount, GetUsageStats, GetUserInfo, ListDialogs, ListMessages, ListTopics, SearchMessages, tool_description; tool_types = {'GetMyAccount': GetMyAccount, 'GetUsageStats': GetUsageStats, 'GetUserInfo': GetUserInfo, 'ListDialogs': ListDialogs, 'ListMessages': ListMessages, 'ListTopics': ListTopics, 'SearchMessages': SearchMessages}; print(json.dumps({name: {'description': tool_description(tool_types[name]).description, 'schema': tool_description(tool_types[name]).inputSchema} for name in sorted(tool_types)}, ensure_ascii=True, sort_keys=True))"` | ✅ | ⬜ pending |
| 18-03-02 | 03 | 3 | ROLL-01 | runtime-behavior | `UV_CACHE_DIR=/tmp/.uv-cache uv run python -m devtools.mcp_client.cli call-tool --name GetMyAccount --arguments '{}' -- docker exec -i mcp-telegram mcp-telegram run && UV_CACHE_DIR=/tmp/.uv-cache uv run python -m devtools.mcp_client.cli call-tool --name ListMessages --arguments '{"exact_dialog_id":-1003779402801,"limit":2}' -- docker exec -i mcp-telegram mcp-telegram run && UV_CACHE_DIR=/tmp/.uv-cache uv run python -m devtools.mcp_client.cli call-tool --name SearchMessages --arguments '{"dialog":"-1003779402801","query":"MCP","limit":2}' -- docker exec -i mcp-telegram mcp-telegram run` | ✅ | ⬜ pending |
| 18-03-03 | 03 | 3 | ROLL-02 | privacy | `uv run pytest tests/test_analytics.py -q && bash tests/privacy_audit.sh` | ✅ | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

No Wave 0 setup is required.
Existing pytest coverage, privacy audit tooling, local reflection commands, and live runtime
verification paths already exist in this repo.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Tool descriptions teach primary versus secondary/helper posture clearly enough for maintainers and clients | SURF-01 | Tests can assert keywords, but a human still has to judge whether the surfaced wording is coherent and non-conflicting | Inspect `uv run cli.py list-tools` output and the posture artifact together. Confirm `ListMessages`, `SearchMessages`, and `GetUserInfo` read as primary surfaces, while `ListDialogs`, `ListTopics`, `GetMyAccount`, and `GetUsageStats` read as secondary/helper or operator surfaces |
| The canonical posture artifact is actually the first document a maintainer would cite | SURF-01 | A test can prove file existence, not whether the artifact is the clearest source of truth | Read the final `18-SURFACE-POSTURE.md` and ensure each tool has one stable classification, rationale, and evidence link without forcing a reader to reconstruct Phase 13-17 history |
| Restarted runtime behavior feels aligned with the final contract, not merely importable | ROLL-01 | Reflection proves schema parity, but a live MCP call is still needed to prove the runtime is serving the intended surface and not a stale build | After rebuild/restart, run one representative secondary call (`GetMyAccount`) and the known primary calls from Phase 17 runtime validation (`ListMessages` and `SearchMessages`) against the container, then confirm the responses align with the reflected posture and schema |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or equivalent regression coverage
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all missing references
- [ ] No watch-mode flags
- [ ] Feedback latency < 120s before repo-local proof
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
