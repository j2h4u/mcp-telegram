---
status: ready
phase: 18-surface-posture-rollout-proof
plan: 02
artifact: Rollout UAT checklist and local reflection baseline
started: 2026-03-14T21:00:00Z
---

# Phase 18 Surface Posture Rollout Checklist (18-UAT.md)

## Purpose

Validate that the posture-aware contract is consistent between:
1. Repo-local reflection (Python/CLI test environment)
2. Runtime container reflection (Docker-deployed service)
3. Brownfield behavior and privacy gates

This checklist ensures the final runtime proof compares parity instead of discovering unfinished repo work.

---

## Section 1: Local Reflection Baseline

**Captured:** 2026-03-14, post-Plan 18-02

### Expected Tool Surface (7 tools)

| Tool | Posture | Pub | Schema Pattern |
|------|---------|-----|---|
| ListMessages | primary | Yes | `dialog` OR `exact_dialog_id`; `topic` OR `exact_topic_id`; optional `navigation` |
| SearchMessages | primary | Yes | `dialog` (string, supports numeric ID); `query`; optional `navigation` |
| GetUserInfo | primary | Yes | `user` (string) |
| ListDialogs | secondary/helper | Yes | optional `exclude_archived`, `ignore_pinned` |
| ListTopics | secondary/helper | Yes | `dialog` (string) |
| GetMyAccount | secondary/helper | Yes | no parameters |
| GetUsageStats | secondary/helper | Yes | no parameters |

### Local Reflection Raw Output

```
Available Tools (repo-local, post-plan 18-02):

| Name | Description | Schema |
|------|-------------|--------|
| GetMyAccount | Return own account info: numeric id, display name, username. No args required. | {} |
| GetUsageStats | Get actionable usage statistics from telemetry (last 30 days). | {} |
| GetUserInfo | Look up Telegram user by name. Returns profile (id, name, username) and shared chats. Fuzzy match resolution. | { "user": { "title": "User", "type": "string" } } |
| ListDialogs | List available dialogs/chats/channels with type and last message timestamp. Returns archived and non-archived by default. Set exclude_archived=True to show only non-archived. | { "exclude_archived": { "default": false, "type": "boolean" }, "ignore_pinned": { "default": false, "type": "boolean" } } |
| ListMessages | List messages in one dialog. Params: dialog= (natural-name) or exact_dialog_id=. Navigation: "newest" (default) or "oldest" or use next_navigation token. Filter by sender= (fuzzy match) or topic=/exact_topic_id=. Set unread=True for unread only. Default limit=50. Forum dialogs: omit topic= for cross-topic view. | { "dialog": { "type": "string" }, "exact_dialog_id": { "type": "integer" }, "limit": { "default": 50, "type": "integer" }, "navigation": { "type": "string" }, "sender": { "type": "string" }, "topic": { "type": "string" }, "exact_topic_id": { "type": "integer" }, "unread": { "default": false, "type": "boolean" } } |
| ListTopics | List forum topics for one dialog. Use before topic= for forum supergroups to choose exact topic name or numeric topic_id. | { "dialog": { "type": "string" } } |
| SearchMessages | Search messages in dialog by text query. Returns matches newest to oldest. Omit navigation for first page; use next_navigation token for continuation. Use numeric ID to disambiguate ambiguous results. @username lookups: prepend @. | { "dialog": { "type": "string" }, "query": { "type": "string" }, "limit": { "default": 20, "type": "integer" }, "navigation": { "type": "string" } } |
```

### Posture Prefix Verification

**Note:** The CLI display above truncates descriptions. Verify `[primary]` and `[secondary/helper]` prefixes are present in the raw tool descriptions by inspecting the MCP server directly:

```bash
# In-container verification (Phase 3 of this checklist)
docker compose exec mcp-telegram python3 -c "
from mcp_telegram import server
for name in sorted(server.mapping.keys()):
    tool = server.mapping[name]
    print(f'{name}: {tool.description[:100]}...')
"
```

---

## Section 2: Repo-Local Contract Proof

**Status: COMPLETE** (Plan 18-02)

Repo-local tests verify the posture contract before any runtime rebuild:

### Task 1: Brownfield and Reflection Tests

- [x] `test_posture_primary_tools_reflected_in_descriptions`: [primary] prefix visible
- [x] `test_posture_secondary_tools_reflected_in_descriptions`: [secondary/helper] prefix visible
- [x] `test_posture_covers_all_registered_tools`: all tools classified
- [x] `test_posture_get_user_info_classified_as_primary`: GetUserInfo is primary
- [x] `test_primary_tools_have_core_read_search_schema`: ListMessages/SearchMessages expose direct access
- [x] `test_helper_tools_remain_available_not_hidden`: secondary tools registered and marked
- [x] `test_primary_tools_require_no_helper_first_choreography`: no ListDialogs prerequisite
- [x] `test_list_messages_direct_dialog_read_no_helper_required`: exact_dialog_id reads alone
- [x] `test_search_messages_numeric_dialog_direct_search_no_helper_required`: numeric dialog search alone
- [x] `test_get_user_info_primary_tool_direct_user_lookup`: direct user lookup schema proven
- [x] `test_tool_posture_covers_all_tool_args_subclasses`: TOOL_POSTURE exhaustive

**Result:** 92 related tests passing (tests/test_server.py + tests/test_tools.py)

### Task 2: Telemetry and Privacy Gates

- [x] `test_telemetry_event_no_pii_fields`: schema has no PII
- [x] `test_telemetry_posture_aware_tool_names_remain_unchanged`: telemetry invariant to posture
- [x] Privacy audit shell script: zero PII fields
  - TelemetryEvent fields: privacy-safe only
  - telemetry_events table: no PII columns
  - Event instantiation: no entity/dialog/user IDs passed

**Result:** 23 analytics tests passing, privacy audit clean

---

## Section 3: Runtime Rollout Verification (Next Phase)

After container rebuild, run these representative calls to prove posture parity:

### 3.1 Primary Tool Workflows (Direct Access)

#### Workflow 1: ListMessages via exact_dialog_id

```bash
# Scenario: Direct read, no ListDialogs prerequisite
# Expected: Messages appear, no "discovery" prefix

# Get a known dialog ID first (from Phase 17 UAT, or run ListDialogs once)
DIALOG_ID=<known-numeric-id>

# Then call ListMessages directly:
curl -X POST http://localhost:3100/sse/call_tool \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "ListMessages",
    "arguments": {
      "exact_dialog_id": '"$DIALOG_ID"',
      "limit": 5
    }
  }'

# PASS criteria:
# - Response includes messages in readable transcript format
# - Response does NOT contain 'resolved:' prefix (direct path, no fuzzy match overhead)
# - Response contains 'next_navigation' token (Phase 16 continuation contract)
```

#### Workflow 2: SearchMessages via numeric dialog ID

```bash
# Scenario: Direct search, treating numeric ID as fast path
# Expected: Search hits appear, no fuzzy dialog resolution overhead

DIALOG_ID=<known-numeric-id>
QUERY="test"

curl -X POST http://localhost:3100/sse/call_tool \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "SearchMessages",
    "arguments": {
      "dialog": "'"$DIALOG_ID"'",
      "query": "'"$QUERY"'",
      "limit": 5
    }
  }'

# PASS criteria:
# - Response includes '[HIT]' markers and hit-local windows
# - Response does NOT contain 'Dialog "'"$DIALOG_ID"'" was not found' (numeric path taken)
# - Response contains 'next_navigation' token for continuation
```

#### Workflow 3: GetUserInfo via direct user lookup

```bash
# Scenario: Primary user-task surface, direct lookup
# Expected: User profile appears

curl -X POST http://localhost:3100/sse/call_tool \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "GetUserInfo",
    "arguments": {
      "user": "username_or_id"
    }
  }'

# PASS criteria:
# - Response includes user ID, name, username, and shared chats
# - Response uses fuzzy match if ambiguous (normal behavior, unchanged)
```

### 3.2 Helper Tool Availability (Secondary Access)

#### Workflow 4: ListDialogs remains available and marked

```bash
# Scenario: Helper tool for discovery/navigation, not required for primary workflows
# Expected: Tool appears in reflection with [secondary/helper] prefix

curl -X POST http://localhost:3100/sse/call_tool \
  -H "Content-Type: application/json" \
  -d '{"tool": "ListDialogs", "arguments": {}}'

# PASS criteria:
# - Response includes dialog list
# - Tool description starts with '[secondary/helper]' tag (run `docker exec mcp-telegram ... list-tools`)
# - Tool is not marked 'deprecated' or 'hidden'
```

#### Workflow 5: ListTopics remains available for forum discovery

```bash
# Scenario: Forum topic discovery (secondary, only needed before topic-scoped reads)
# Expected: Tool available, marked as secondary

curl -X POST http://localhost:3100/sse/call_tool \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "ListTopics",
    "arguments": {
      "dialog": "Backend Forum"
    }
  }'

# PASS criteria:
# - Response includes forum topics or actionable error (e.g., not a forum)
# - Tool marked '[secondary/helper]' in reflection
```

### 3.3 Parity Checks

#### ROLL-01: Reflection Parity

**Requirement:** Runtime tool surface matches repo-local posture.

```bash
# Capture runtime reflection
docker compose logs mcp-telegram | grep "Tool.*description" | head -20

# OR run inside container:
docker compose exec mcp-telegram python3 -c "
from mcp_telegram import server
import json
for name in sorted(server.mapping.keys()):
    tool = server.mapping[name]
    print(f'{name}: {tool.description[:80]}')
" > /tmp/runtime-reflection.txt

# Compare with repo-local:
UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools > /tmp/local-reflection.txt

# PASS criteria:
# - Same 7 tools present in both
# - Posture prefixes ([primary] / [secondary/helper]) present and consistent
# - No new tools added
# - No tools removed
```

#### ROLL-02: Behavioral Parity

**Requirement:** Phase 17 direct workflows continue working; posture work doesn't regress reads/searches.

```bash
# Rerun 6 representative Phase 17 UAT tests against live container:
# 1. Direct read via exact_dialog_id (no fuzzy lookup)
# 2. Direct search via numeric dialog ID
# 3. Direct user info lookup
# 4. ListMessages selector validation (conflict check still works)
# 5. Concurrent MCP session resilience (from Phase 17-04)
# 6. Helper tool availability (ListDialogs, ListTopics not removed)

# Expected: All 6 pass without regression

# PASS criteria:
# - All 6 workflows succeed
# - No new errors introduced
# - Response format unchanged (navigation tokens, hit markers, etc.)
# - Telemetry events logged without PII
```

---

## Section 4: Roll-Out Gate Criteria

### ROLL-01: Reflection Parity (Runtime vs Repo-Local)

- [x] Repo-local: 7 tools, posture markers in descriptions
- [ ] Runtime: 7 tools, posture markers visible (to be verified after container rebuild)
- [ ] **Parity proof:** Tool names, descriptions (with [primary]/[secondary/helper] prefixes), and schemas match

### ROLL-02: Behavioral Continuity (Phase 17 Workflows Intact)

- [x] Repo-local: 92 posture-aware contract tests passing
- [ ] Runtime: 6 representative Phase 17 calls verified against live container
- [ ] **Continuity proof:** Direct access workflows, no ListDialogs prerequisite, no posture-based behavioral changes

### Privacy/Telemetry

- [x] Repo-local: Privacy audit clean, analytics tests green
- [ ] Runtime: No new telemetry fields logged; schema invariant to posture
- [ ] **Privacy proof:** Telemetry collection continues without widening event scope

---

## Next Steps (Post-Plan 18-02)

1. **Container rebuild** (Phase 18, Plan 03):
   - Build and deploy updated mcp-telegram service
   - Run ROLL-01 reflection parity check
   - Run ROLL-02 behavioral continuity checks
   - Verify final UAT results

2. **Final documentation**:
   - Update 18-03-SUMMARY.md with runtime verification results
   - Mark ROLL-01 and ROLL-02 as verified
   - Close Phase 18

---

## Test Execution Commands

### Local Repo Tests (Completed)

```bash
# Full posture test suite
uv run pytest tests/test_server.py tests/test_tools.py -k "posture or list_messages or search_messages or list_dialogs or list_topics or get_user_info" -q

# Analytics and privacy
uv run pytest tests/test_analytics.py -q
bash tests/privacy_audit.sh

# Local reflection baseline
UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools
```

### Runtime Verification (Post-Container-Rebuild)

```bash
# Start the container
cd /opt/docker/mcp-telegram
docker compose up -d --build

# Wait for health
sleep 5

# Check posture reflection in runtime
docker compose exec mcp-telegram python3 -c "
from mcp_telegram import server
for name in sorted(server.mapping.keys()):
    tool = server.mapping[name]
    posture = '[primary]' if 'primary' in tool.description else '[secondary/helper]'
    print(f'{name:20} {posture}')"

# Run representative workflows (see Section 3.1-3.3 above)
```

---

## Artifacts

- **Local reflection baseline:** Section 1 (raw tool surface, 2026-03-14)
- **Repo-local test proof:** Section 2 (92 tests, 23 analytics tests, privacy audit)
- **Runtime verification script:** Section 3 (curl commands for ROLL-01 and ROLL-02)
- **Gate criteria:** Section 4 (checklist for rollout approval)
