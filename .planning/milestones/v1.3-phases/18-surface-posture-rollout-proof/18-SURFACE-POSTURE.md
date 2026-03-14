# Surface Posture Matrix — Phase 18

**Current era:** Medium (v1.3)
**Baseline:** Phase 13 role inventory
**This is the current tool classification, not a speculative future-removal plan.**

| Tool | Classification | Rationale | Evidence |
|------|---------------|-----------|----------|
| ListMessages | primary | Core message-reading workflow; direct dialog access via exact_dialog_id | Phase 17 direct workflows, `FLOW-01` |
| SearchMessages | primary | Core search workflow; direct dialog scoping via signed numeric id | Phase 17 direct workflows, `FLOW-02` |
| GetUserInfo | primary | Direct user-task surface for profile lookup and shared-chat discovery | Phase 13 role inventory |
| ListDialogs | secondary/helper | Navigation/discovery aid; not required for direct read/search | Phase 13 role inventory, Phase 17 choreography reduction |
| ListTopics | secondary/helper | Forum topic discovery; prerequisite for topic-scoped reads only | Phase 13 role inventory |
| GetMyAccount | secondary/helper | Operator/identity surface; used for self-identification only | Phase 13 role inventory |
| GetUsageStats | secondary/helper | Operator/inspection surface; telemetry summary for maintainers | Phase 13 role inventory |

## Source of Truth

The canonical code-level posture is `TOOL_POSTURE` in `src/mcp_telegram/tools.py`.
Posture is reflected in MCP tool descriptions via `[primary]` or `[secondary/helper]` prefixes.
Drift is caught by `tests/test_server.py` (reflection) and `tests/test_tools.py` (coverage).
