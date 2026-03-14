# Phase 14: Boundary Recovery - Research

**Researched:** 2026-03-14

## Summary

Phase 14 should stay tightly scoped to the real server-boundary failure collapse in
`src/mcp_telegram/server.py`. The current brownfield problem is narrow and explicit: escaped
exceptions from `tools.tool_args(...)` and `tools.tool_runner(...)` are logged and then flattened
into `RuntimeError("Tool <name> failed")`, which discards the actionable detail maintainers need
to decide the next recovery step.

This phase should not rewrite tool-level recovery. The existing handlers already contain useful
recovery behavior in several places:

- recoverable topic and cursor paths in `ListMessages` return action-oriented `TextContent`
- `GetUserInfo` already returns actionable fetch-failure text
- `GetUsageStats` already absorbs query failures locally
- tool telemetry already records `error_type` on unexpected exceptions

The planning job is therefore to make escaped failures safer and more informative at the server
boundary without widening logs, leaking stack traces into tool responses, or spreading a new error
framework across every tool.

## Research Question

What does the planner need in order to create executable Phase 14 plans that remove generic
server-boundary failure collapse while preserving the existing brownfield recovery strengths?

## Brownfield Findings

### 1. The actual collapse point is isolated

`server.call_tool()` is the only place where escaped tool failures are turned into the generic
boundary wrapper:

- argument shape validation happens before the wrapper for non-dict arguments and unknown tool names
- tool-argument construction via `tools.tool_args(...)` happens inside the wrapper
- tool execution via `tools.tool_runner(...)` also happens inside the wrapper
- any exception from those inner steps is logged and re-raised as `RuntimeError("Tool <name> failed")`

This means Phase 14 can stay bounded around `server.py` instead of treating every tool as a
separate recovery design problem.

Control cases matter here: `unknown tool` and non-dict `arguments` already raise directly before
the generic wrapper. They are useful regression anchors, but they are not the main ERR-01 target
because they are not escaped tool failures losing handler-local detail.

### 2. Handler-local recovery is already a preserved strength

The brownfield tool layer already does important local recovery work that should remain intact:

- `ListMessages` converts invalid cursor, inaccessible topic, and topic refresh failures into
  explicit user-facing recovery text instead of raising blindly
- `GetUserInfo` turns fetch failures into actionable text instead of boundary-level crashes
- `GetUsageStats` already converts DB query failures into action text
- telemetry finally-blocks still record `error_type` on unexpected exceptions

Phase 14 should connect escaped failures to a better boundary surface, not replace these local
recovery paths with one generic global policy.

### 3. The missing contract is stage-aware escaped-error rendering

The current boundary collapses two different failure stages into the same generic wrapper:

- tool-argument validation/construction failures
- unexpected runtime failures after handler execution starts

The planner should treat this as the core contract gap. Phase 14 needs a safe escaped-error shape
that preserves:

- tool name
- failing stage
- concise detail or stable error category
- one actionable next step

It should not expose raw stack traces, raw Telegram payloads, or sensitive message content.

### 4. Tests do not currently anchor the real boundary

The repo has strong brownfield coverage for handler-local recovery and telemetry-on-error, but not
for this server boundary specifically:

- `tests/test_tools.py` covers many action-text recoveries plus telemetry recording on error
- `tests/test_mcp_test_client.py` only exercises a fake MCP server fixture
- there is no dedicated test file anchoring `server.call_tool()` error shaping in this repo

Phase 14 therefore needs boundary-specific tests before or alongside implementation so later
surface work does not reintroduce generic collapse.

## Locked Planning Constraints

The Phase 14 plans should treat these as fixed inputs:

- the scope is `ERR-01`, not broader Medium-contract redesign
- read-only Telegram scope remains a hard invariant
- privacy-safe telemetry remains mandatory; no message content or identifying payloads enter error
  surfaces or telemetry
- explicit ambiguity handling, topic fidelity, and cache-backed recovery remain preserved strengths
- runtime-affecting changes must be verified against the restarted runtime on this machine, not
  just repository tests
- the phase should prefer one small boundary mechanism over tool-by-tool exception reshaping

## Recommended Plan Split

Phase 14 is best planned as two executable plans in a single dependency chain.

### Plan 01: Boundary Contract Tests

Purpose:
- add brownfield tests that describe the intended escaped-error surface at `server.call_tool()`
- lock the distinction between validation-stage failures and runtime-stage failures
- prove action-text recovery paths still bypass boundary collapse because they do not escape

Primary artifact:
- a dedicated server-boundary test module, likely centered on `server.call_tool()`

Why first:
- the current failure is small enough that tests can define the exact contract before the recovery
  mapper lands
- later Medium phases will be safer if the boundary contract is explicit now

### Plan 02: Boundary Recovery Implementation and Runtime Proof

Purpose:
- replace generic `Tool <name> failed` collapse with a safe, actionable escaped-error surface
- keep tool telemetry behavior intact
- verify the changed boundary in both repository tests and the restarted runtime

Primary artifacts:
- `src/mcp_telegram/server.py`
- optional minimal helper support only if `server.py` alone cannot express the safe recovery shape

Why second:
- once the contract is test-anchored, the implementation can stay bounded and avoid speculative
  architecture
- runtime proof belongs in the implementation plan because this boundary is visible only when the
  real server process is exercised

## Risks To Plan Around

### Risk 1: The phase leaks too much error detail

Replacing the generic wrapper does not justify returning raw stack traces, raw Telegram payloads,
or exception dumps. The plan should require safe detail only.

### Risk 2: The phase diffuses into tool-by-tool error rewrites

The brownfield collapse point is in `server.py`. If the plan starts spreading new exception
wrappers across every tool, the scope will drift past ERR-01.

### Risk 3: Local recovery paths get overwritten by a generic boundary policy

Known action-text recoveries already exist and should remain authoritative. The new boundary should
only shape escaped exceptions.

### Risk 4: Runtime verification is deferred too late

This boundary is runtime-visible. The restarted container must prove the changed behavior before
the phase is considered complete.

## Validation Architecture

### Test infrastructure

- Primary validation mode: `pytest` with focused async tests against `server.call_tool()` and the
  existing tool-level regression anchors
- Brownfield anchors:
  - `src/mcp_telegram/server.py`
  - `tests/test_tools.py`
  - `tests/test_mcp_test_client.py` for client-facing error expectations reference only
- Runtime anchor:
  - rebuilt and restarted `mcp-telegram` container on this machine

### Required verification themes

The Phase 14 plans should map their tasks to these verification themes:

1. boundary contract coverage for escaped validation and runtime failures
2. safe detail rules that preserve actionable recovery direction without stack-trace leakage
3. telemetry continuity so unexpected errors still record `error_type`
4. handler-recovery preservation so existing action-text flows remain untouched
5. restarted-runtime proof for the changed boundary behavior

### Expected validation commands

- `uv run pytest tests/test_server.py -q`
- `uv run pytest tests/test_tools.py -k "tool_records_telemetry_on_error or get_user_info_fetch_error_returns_action or list_messages_invalid_cursor_returns_error" -q`
- `uv run pytest`
- `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram`
- `docker exec mcp-telegram /opt/venv/bin/python -c "import asyncio; from mcp_telegram import server; print(asyncio.run(server.call_tool('ListDialogs', {}))[0].text)"`

The runtime command above is intentionally illustrative: the final implementation should use a
repeatable failure trigger that proves the live boundary returns actionable detail rather than the
generic wrapper.

## Phase 14 Is Ready For Planning Now

The scope is implementation-ready:

- the collapse point is isolated
- the preserved invariants are clear
- the phase can stay bounded to one server-boundary contract plus one runtime-proof step
- the recommended two-plan split covers both the contract anchor and the runtime-visible fix
