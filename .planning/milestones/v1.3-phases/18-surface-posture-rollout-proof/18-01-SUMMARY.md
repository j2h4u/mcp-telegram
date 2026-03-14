---
phase: 18-surface-posture-rollout-proof
plan: 01
subsystem: surface
tags: [posture, classification, primary-vs-secondary, reflection-safe]
completed_date: 2026-03-14
duration: "~15 min"
decision_graph:
  - requires: "Phase 13 role inventory, Phase 17 direct workflows"
  - provides: "Code-level TOOL_POSTURE source of truth, reflected posture markers, brownfield guards"
  - affects: "MCP surface descriptions, tool classification vocabulary"
tech_stack:
  patterns: ["posture classification", "reflected MCP teaching", "assertion-driven drift detection"]
  added: ["TOOL_POSTURE dict", "posture prefix markers", "reflection + brownfield tests"]
key_files:
  created:
    - ".planning/phases/18-surface-posture-rollout-proof/18-SURFACE-POSTURE.md"
  modified:
    - "src/mcp_telegram/tools.py"
    - "tests/test_server.py"
    - "tests/test_tools.py"
---

# Phase 18 Plan 01: Surface Posture Rollout (SUMMARY)

Froze and exposed the current tool-surface posture so maintainers can point to one consistent primary-versus-secondary/helper story across planning docs, code, and reflected tests.

## Execution Summary

All 3 tasks completed successfully. No deviations from plan.

### Task 1: Add bounded posture source of truth at public tool boundary
**Status:** Complete
**Commits:** c1bed65

- Added `TOOL_POSTURE` dict in `src/mcp_telegram/tools.py` classifying all 7 tools
- Updated `tool_description()` to prepend posture tag (`[primary]` or `[secondary/helper]`) to reflected descriptions
- Verified posture markers visible in actual tool descriptions via code-level inspection

### Task 2: Add reflection and brownfield tests
**Status:** Complete
**Commits:** fb773d5

- `test_posture_primary_tools_reflected_in_descriptions()`: ensures ListMessages, SearchMessages, GetUserInfo start with `[primary]`
- `test_posture_secondary_tools_reflected_in_descriptions()`: ensures ListDialogs, ListTopics, GetMyAccount, GetUsageStats start with `[secondary/helper]`
- `test_posture_covers_all_registered_tools()`: validates every registered tool has a posture classification
- `test_tool_posture_covers_all_tool_args_subclasses()`: brownfield guard ensuring ToolArgs coverage matches TOOL_POSTURE

All tests pass (4/4).

### Task 3: Write canonical posture artifact and confirm reflection
**Status:** Complete
**Commits:** 54c6729

- Created `.planning/phases/18-surface-posture-rollout-proof/18-SURFACE-POSTURE.md` with maintainer-facing classification
- Documented all 7 tools: posture, rationale, evidence links
- Confirmed posture markers visible in reflected tool descriptions
- Clarified this is current Medium-era posture, not speculative future-removal plan

## Verification Results

```
✓ uv run pytest tests/test_server.py -k "list_messages or search_messages or get_user_info or posture or description" -q
  6 passed, 4 deselected in 0.48s

✓ uv run pytest tests/test_tools.py -k "list_messages or search_messages or list_dialogs or list_topics or get_my_account or get_user_info or get_usage_stats or schema or posture" -q
  88 passed, 10 deselected in 1.09s
```

## Final Artifact

### TOOL_POSTURE Dict (src/mcp_telegram/tools.py)
```python
TOOL_POSTURE: dict[str, str] = {
    "ListMessages": "primary",
    "SearchMessages": "primary",
    "GetUserInfo": "primary",
    "ListDialogs": "secondary/helper",
    "ListTopics": "secondary/helper",
    "GetMyAccount": "secondary/helper",
    "GetUsageStats": "secondary/helper",
}
```

### Reflected Surface
- All primary tools now display as `[primary] description...`
- All secondary/helper tools now display as `[secondary/helper] description...`
- Drift between planning docs, code, and tests is now a test failure (assertion-driven)

### Planning Artifact
- `.planning/phases/18-surface-posture-rollout-proof/18-SURFACE-POSTURE.md`
- Canonical classification table with rationale and evidence links
- Single source of truth for maintainers to cite when explaining surface posture

## Deviations from Plan

None — plan executed exactly as written.

## Key Decisions

1. **Posture as code-level constant**: TOOL_POSTURE defined in tools.py near constants, easily discoverable
2. **Reflected teaching via prefix**: posture tags prepended to descriptions so the reflected MCP surface teaches posture without separate mapping
3. **Assertion-driven drift detection**: three focused tests catch posture drift at reflection and brownfield levels
4. **No workflow redesign**: posture classification is orthogonal to tool behavior; Phase 17 workflows remain unchanged

## Success Criteria Met

- [x] SURF-01 anchored in planning docs, code, and tests
- [x] Current seven-tool surface has one consistent posture vocabulary
- [x] Local reflection and planning artifacts agree on intended posture
- [x] No new workflow-shape redesign introduced while landing posture markers
