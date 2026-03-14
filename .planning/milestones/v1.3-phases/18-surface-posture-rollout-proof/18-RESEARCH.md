# Phase 18: Surface Posture & Rollout Proof - Research

**Researched:** 2026-03-14

## Summary

Phase 18 should finish the Medium-path migration by doing two things together:

- make the tool-surface posture explicit and consistent across planning artifacts, code, and tests;
- prove that the contract now served by the repository is the same contract exposed by the restarted live runtime, without weakening privacy-safe telemetry.

The key planning fact is that the repo already treats `ListMessages` and `SearchMessages` as the stable primary workflows. Phase 17 deliberately stopped before helper-surface classification, and Phase 13 already supplied the starting role inventory. Phase 18 should therefore **not** reopen boundary recovery, capability seam extraction, navigation unification, or direct read/search workflow shaping. It should lock posture and rollout proof on top of that finished base.

The strongest starting posture from Phase 13 is:

- `primary`: `ListMessages`, `SearchMessages`, `GetUserInfo`
- `secondary/helper`: `ListDialogs`, `ListTopics`, `GetMyAccount`, `GetUsageStats`

That posture is not yet the fully enforced source of truth in current repo artifacts. Phase 18 exists to make it explicit, testable, and runtime-proven.

## What the Planner Must Lock In

- The authoritative tool classification vocabulary.
  Use one stable vocabulary such as `primary` and `secondary/helper`. Do not reintroduce Phase 13's broader `merge` and `future-removal` labels unless the plan explicitly needs them for migration notes.

- The exact tool classification set for the current shipped Medium surface.
  The planner should start from the Phase 13 inventory, then decide whether `GetUserInfo` remains explicitly `primary` in Phase 18 artifacts or is documented as a separate inspect/operator case. That decision must be consistent everywhere.

- The source-of-truth location for posture.
  Phase 18 needs one canonical artifact that maintainers can point to first, with code/tests/docs echoing it instead of inventing parallel classifications.

- Whether posture is documentation-only or schema-visible.
  The current MCP schema already teaches contract details through descriptions and reflected input schemas. If posture needs to be visible to maintainers via docstrings or reflected descriptions, plan that as contract work and verify it through `tests/test_server.py`, `uv run cli.py list-tools`, and restarted-runtime checks.

- The exact rollout proof chain for contract-affecting work.
  The repo's established discipline is: brownfield tests -> local reflection -> rebuilt/restarted runtime verification. Phase 18 should adopt that as the explicit acceptance architecture, not as optional execution detail.

- The privacy boundary for telemetry and logs.
  Phase 18 can change posture and validation wording, but it cannot widen telemetry fields, log message content, log navigation/query payloads, or start recording identifying selectors.

- Scope guardrails.
  Phase 18 should not redesign tool behavior again. It should classify and prove the already reshaped contract from Phases 16-17.

## Required Artifacts

- A Phase 18 research artifact: this file.
- At least one planning artifact that records the final posture and rollout-proof approach for execution.
- One canonical posture artifact maintainers can cite after implementation.
  Likely candidates: the Phase 18 plan docs and a phase-local posture note or summary, not a repo-wide rewrite.
- Code-level posture evidence.
  This can be docstrings, comments near tool definitions, or other bounded in-repo markers that align with the planning posture.
- Contract/reflection tests.
  `tests/test_server.py` should remain the main reflected-schema anchor.
- Brownfield behavior and telemetry tests.
  `tests/test_tools.py`, `tests/test_analytics.py`, and `tests/privacy_audit.sh`.
- Local reflection evidence.
  `uv run cli.py list-tools`
- Live runtime evidence.
  Rebuild/restart the `mcp-telegram` container and verify inside the running container that the intended reflected surface is present.

## Existing Brownfield Evidence and Relevant Code/Test/Runtime Paths

### Planning evidence

- `.planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-FRAME.md`
  Best existing role inventory for `primary` vs `secondary` surfaces.
- `.planning/phases/13-implementation-sequencing-decision-memo/13-SEQUENCING-BRIEF.md`
  Establishes that helper-surface posture comes after Phases 14-17 and that rollout proof must include reflection plus restarted runtime.
- `.planning/phases/13-implementation-sequencing-decision-memo/13-IMPLEMENTATION-MEMO.md`
  Carries the same posture and rollout rules into milestone handoff form.
- `.planning/phases/17-direct-read-search-workflows/17-RESEARCH.md`
  Explicitly keeps final helper/posture decisions out of Phase 17.
- `.planning/phases/17-direct-read-search-workflows/17-03-PLAN.md`
  Direct handoff into Phase 18 helper-posture and rollout-proof work.
- `.planning/phases/17-direct-read-search-workflows/17-03-SUMMARY.md`
  Shows the current final direct-workflow contract and local-plus-runtime proof pattern.
- `.planning/phases/17-direct-read-search-workflows/17-04-SUMMARY.md`
  Confirms runtime-only gaps must be closed in the rebuilt container, not inferred from repo tests.
- `.planning/phases/16-unified-navigation-contract/16-VALIDATION.md`
  Good template for a validation strategy that combines schema checks, telemetry checks, and runtime checks.
- `.planning/phases/14-boundary-recovery/14-VALIDATION.md`
  Important privacy/logging reminder: richer rollout proof must still avoid message content and identifying payloads.

### Code and reflection paths

- `src/mcp_telegram/tools.py`
  Defines the public tool surface via `ToolArgs` subclasses and `tool_description()`.
- `src/mcp_telegram/server.py`
  Reflects tool classes into the MCP surface at process start via `enumerate_available_tools()` and `mapping`.
- `cli.py`
  `list-tools` is the local reflection probe used throughout Phase 13-17 planning and validation.

### Brownfield test paths

- `tests/test_server.py`
  Current reflected-schema and MCP-boundary anchor.
  It already pins `ListMessages` and `SearchMessages` schema expectations and server-boundary validation behavior.
- `tests/test_tools.py`
  Main brownfield contract anchor for read/search behavior plus telemetry recording behavior.
  It already proves schema descriptions, direct workflow behavior, and per-tool telemetry behavior.
- `tests/test_analytics.py`
  Strongest repo proof that telemetry schema stays privacy-safe and bounded.
- `tests/privacy_audit.sh`
  Static privacy gate that rejects PII fields in telemetry dataclasses, telemetry schema, and `TelemetryEvent(...)` callsites.

### Runtime paths

- `/opt/docker/mcp-telegram/docker-compose.yml`
  Rebuild/restart entrypoint for the long-lived runtime.
- Runtime container: `mcp-telegram`
- In-container verification pattern already used in prior phases:
  `docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram`
- Then verify inside container with Python reflection against `mcp_telegram.tools` or via the running MCP process.

## Validation Architecture

Phase 18 can support a clean four-layer validation architecture.

### Layer 1: posture source-of-truth checks

- Planning artifact says which tools are `primary` and which are `secondary/helper`.
- Code comments/docstrings/descriptions use the same vocabulary.
- Tests assert the same intended public teaching surface.

### Layer 2: brownfield contract checks

- `tests/test_tools.py`
  Confirms the affected tools still behave according to the post-Phase-17 workflow contract.
- `tests/test_server.py`
  Confirms the reflected local MCP surface matches the intended contract wording and schema.

### Layer 3: privacy-safe telemetry checks

- `tests/test_analytics.py`
  Confirms telemetry schema and runtime semantics stay bounded.
- `tests/privacy_audit.sh`
  Confirms no identifying or content-bearing fields enter telemetry definitions or instantiations.

### Layer 4: rollout parity checks

- Local reflection:
  `uv run cli.py list-tools`
- Restarted runtime:
  rebuild/restart the long-lived container, then inspect the surface from inside the live runtime.
- Acceptance rule:
  local reflection and restarted runtime must expose the same intended contract for affected tools.

This architecture maps directly to the phase requirements:

- `SURF-01`: posture source-of-truth plus brownfield contract checks
- `ROLL-01`: brownfield tests plus reflected local schemas plus restarted-runtime parity
- `ROLL-02`: telemetry tests plus privacy audit

## Planning Risks

- Posture drift across artifacts.
  If plans, tool descriptions, and tests use different posture language, Phase 18 will look complete but fail the maintainer-facing proof requirement.

- Reopening Phase 17 behavior work.
  If implementation starts reshaping `ListMessages` or `SearchMessages` again instead of classifying/proving them, the phase will sprawl and blur requirement ownership.

- Treating posture as docs-only.
  If the posture changes are only written in planning docs and not reflected in code/tests, maintainers will not be able to point to consistent evidence.

- Skipping reflected-schema assertions.
  Phase 16-17 already proved that behavior tests alone are insufficient for public contract work.

- Stale container proof.
  The live runtime is reflection-snapshotted at process start. Green repo tests do not prove the running container serves the new contract.

- Privacy regression during proof work.
  Extra verification logging or telemetry tweaks can accidentally widen logged payloads even if the tool behavior itself is unchanged.

- Over-classifying minor surfaces.
  Phase 18 only needs enough posture detail to distinguish primary vs secondary/helper surfaces for the current contract. It does not need a speculative full future-removal matrix unless execution genuinely benefits from it.

## Recommended Phase Shape

Phase 18 is best planned as three bounded plans.

### Plan 01: Freeze and expose the posture source of truth

Purpose:

- lock the final `primary` vs `secondary/helper` classification for the current tool set;
- add the minimum code/planning markers needed so maintainers can point to the same posture in more than one place;
- add or update tests that assert the intended surfaced teaching contract.

Primary artifacts:

- `src/mcp_telegram/tools.py`
- `tests/test_server.py`
- `tests/test_tools.py`
- Phase 18 planning docs

### Plan 02: Prove repo-local contract parity

Purpose:

- ensure brownfield behavior, reflected local schemas, and posture artifacts agree on the affected tool contract;
- make `uv run cli.py list-tools` and reflected-schema assertions part of the explicit acceptance proof.

Primary artifacts:

- `tests/test_server.py`
- `tests/test_tools.py`
- `cli.py` usage in validation docs
- Phase 18 validation doc

### Plan 03: Rebuild, restart, and close rollout/privacy proof

Purpose:

- rebuild/restart the long-lived runtime;
- verify in-container parity for the affected tool schemas/behavior;
- rerun privacy-safe telemetry gates so rollout proof includes `ROLL-02`, not just schema parity.

Primary artifacts:

- `tests/test_analytics.py`
- `tests/privacy_audit.sh`
- runtime verification commands against `/opt/docker/mcp-telegram/docker-compose.yml`
- Phase 18 summary/verification docs

## Phase 18 Is Ready For Planning Now

The repo already has enough evidence to plan this phase well:

- the target posture baseline exists in Phase 13;
- the direct primary workflows are already implemented and runtime-proven in Phase 17;
- the reflection/testing/runtime proof pattern is already established by Phases 14, 16, and 17;
- the privacy-safe telemetry gate is already explicit in tests and audit scripts.

The planner's main job is not to discover new architecture. It is to turn existing posture guidance and existing rollout discipline into one consistent, enforceable Phase 18 execution plan.

## RESEARCH COMPLETE
