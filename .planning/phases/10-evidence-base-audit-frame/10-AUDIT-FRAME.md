# Phase 10 Audit Frame

This artifact defines the reusable audit rubric for the current `mcp-telegram` MCP surface. Phase
11 should use it with the retained evidence log and brownfield baseline instead of re-deriving the
evaluation method from scratch.

## Evaluation Posture

- Use named evidence for every judgment.
- Pair normative external guidance from the evidence log with concrete brownfield authority from the
  current code, tests, and reflected runtime surface.
- Treat discovery freshness as limited by snapshotted tool enumeration at process start. A tool
  discovery judgment must distinguish between what the client sees today and what would only appear
  after a restart.
- Treat generic server-boundary wrapping as part of recovery analysis. If a path can collapse into
  `Tool <name> failed`, call out the lost context explicitly rather than assuming the handler's
  intended recovery text reached the model.

## Judgment Bands

- `strong`: The current surface matches the tool or workflow shape well, gives the model enough
  guidance to continue without guesswork, and the cited evidence shows that this behavior is
  deliberate rather than accidental.
- `mixed`: The current surface partly supports the intended use, but the model still has to infer,
  retry, or reconcile inconsistent patterns to complete the task.
- `weak`: The current surface leaves material burden on the model, obscures the next step, or
  depends on behavior that is not reliably visible from the public contract.

## Rubric

| Dimension | What the evaluator should look for | Brownfield evidence to cite | `strong` in this project | `mixed` in this project | `weak` in this project |
| --- | --- | --- | --- | --- | --- |
| task-shape fit | Whether a tool or workflow matches the user job directly, instead of forcing helper-step choreography before the real work can start. | `tools.py` tool contracts, reflected tool list, workflow tests in `tests/test_tools.py`, preserved invariants from `10-BROWNFIELD-BASELINE.md`. | The tool or workflow lines up with the user task in one obvious step or a clearly taught sequence, with minimal detours beyond the shipped read-only scope. | The model can finish the job, but only by composing extra discovery or pagination steps that feel adjacent to the real task rather than integral to it. | The model must reconstruct the task from low-level mechanics, hidden prerequisites, or fragile sequencing that the current surface does not teach well. |
| metadata/schema clarity | Whether tool names, descriptions, and input schemas make invocation choices legible before a call is made. | `server.py` reflection path, `ToolArgs` docstrings, Pydantic schema output, `_sanitize_tool_schema()`, reflected `tools/list` output, discovery freshness limits from snapshotted tool enumeration. | The model can distinguish when to use a tool and how to fill its inputs from the exposed metadata alone, and the snapshot behavior does not create misleading discovery expectations. | The metadata points in the right direction, but important caveats, mode switches, or stale-discovery edge cases still require trial-and-error or memory of prior calls. | The metadata leaves major ambiguity about purpose or inputs, or the discovery freshness limit makes the available surface easy to misread in ordinary agent flow. |
| continuation burden | How much work the model must do after the first call to finish the user-visible task, including pagination, retries, and cross-tool choreography. | `tools.py` pagination contracts, `next_cursor`, `next_offset`, `from_beginning`, forum flow tests, resolver behavior, brownfield workflow notes. | The continuation path is explicit and lightweight: next steps, pagination direction, and required follow-up tools are taught in the surface itself. | The surface eventually supports completion, but continuation depends on learning inconsistent pagination or tool choreography that is only partly explained. | The model carries most of the burden for sequencing, state tracking, or retry logic because the surface does not make the continuation contract clear. |
| ambiguity recovery | Whether the model gets actionable recovery guidance when names, topics, or runtime conditions do not resolve cleanly. | `resolver.py`, recovery branches in `tools.py`, `tests/test_resolver.py`, recovery cases in `tests/test_tools.py`, generic `Tool <name> failed` server-boundary behavior. | Ambiguous, missing, or inaccessible states usually return action-oriented guidance that tells the model what to retry next, and generic wrapper failures are rare or clearly bounded. | Some recovery paths are well-guided, but other paths degrade into generic server wrapping or require the model to infer how to recover from partial context. | Failures commonly collapse to unclear text, generic `Tool <name> failed` wrapping, or missing next-step guidance, leaving the model without a reliable recovery path. |
| structured-output expectations | Whether the public contract sets realistic expectations for what will be structured versus text-first, and whether that output shape is workable for downstream reasoning. | `server.py` result contract, `formatter.py`, `tests/test_formatter.py`, text-first responses in `tools.py`, evidence-log comparison point from Anthropic structured-output guidance. | The current text-first contract is consistent and explicit enough that the model can reliably extract the needed state or next action without pretending there is a richer schema than exists. | The outputs are usable, but important fields or continuation cues are embedded in prose or formatting conventions that create moderate parsing burden. | The output shape hides essential state in inconsistent text or implies structure that the current contract does not actually provide, making downstream reasoning fragile. |

## Audit Invariants

- The rubric is non-numeric. Use `strong`, `mixed`, and `weak` with evidence notes rather than
  scores.
- The audit should describe redesign pressure without assuming the current read-only, privacy-safe,
  stateful baseline is disposable.

## Phase 11 Audit Instructions

Phase 11 must audit both `mcp-telegram` units of analysis:

- each current public tool
- the main user workflows for discovery, reading, search, topic handling, and recovery/error flows

Use the current reflected seven-tool surface as the tool-level checklist:
`GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`, and
`SearchMessages`.

For every major finding:

- pair named evidence from `10-EVIDENCE-LOG.md` with the concrete `mcp-telegram` behavior being
  judged
- cite the brownfield baseline or direct code/test anchors that show where the behavior appears
- describe the model burden or benefit in the actual workflow, not only in handler-local terms

Do not write findings that rely only on generic best-practice prose. A valid finding ties named
evidence to a specific tool contract, workflow step, recovery path, pagination pattern, or output
convention present in the shipped surface.
