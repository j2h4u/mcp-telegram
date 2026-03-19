# Phase 13 Implementation Frame

## Locked Implementation Posture

Phase 13 is not choosing a redesign path. Medium is already chosen, and this artifact freezes the
planning posture that the next implementation milestone must inherit.

The chosen Medium posture is a migration stage toward a later Maximal redesign rather than a final
steady-state public contract. The next implementation milestone should therefore optimize for a
clean Medium-era transition that removes a large share of current model burden while leaving a
later Maximal step cheaper to execute.

For the follow-up implementation milestone, backward compatibility is not a default planning
constraint.

Backward compatibility is not a default planning constraint for that follow-up implementation
milestone. Compatibility shims, alias tools, or dual-surface rollout support should only appear if
a later decision explicitly requires them.

## Preserved Invariants

The next implementation milestone must preserve these non-negotiable inputs:

- `read-only scope` remains the Telegram boundary.
- `privacy-safe telemetry` remains mandatory and must not widen into message-content logging.
- `explicit ambiguity handling` remains required; simplification must not become silent auto-picks.
- `stateful runtime reality` remains part of the design boundary, including reflection-time tool
  exposure, persisted session state, process-cached clients, and local SQLite-backed metadata.
- `recovery-critical caches` remain preserved strengths, especially entity and topic state that
  support forum fidelity and recovery-oriented retries.

## Planning Boundary

This implementation frame is decision-ready and implementation-oriented. It does not reopen Minimal
versus Medium versus Maximal comparison, and it does not treat current tool names as constraints
that must survive unchanged. Its purpose is to freeze the recommendation posture and the preserved
constraints that later sequencing and validation work must trust.

## Brownfield Starting Point

The future implementation milestone starts from the reflected seven-tool surface verified on
2026-03-13 by `UV_CACHE_DIR=/tmp/.uv-cache uv run cli.py list-tools` and cross-checked against
`src/mcp_telegram/tools.py`:

- `GetMyAccount`
- `GetUsageStats`
- `GetUserInfo`
- `ListDialogs`
- `ListMessages`
- `ListTopics`
- `SearchMessages`

This is the real starting point for Medium-era implementation planning. The milestone should assume
a reflection-based and stateful runtime rather than a clean-slate interface rewrite.

## Main Burden Drivers Inherited From Phase 11

The next milestone should treat these pressure points as the concrete reasons to reshape the
surface:

- `helper-step choreography`: common jobs still push the model through `ListDialogs` before the
  actual read or search, and forum reads often add `ListTopics` as another required hop.
- `mixed continuation`: adjacent navigation jobs still teach different control vocabulary through
  `next_cursor`, `next_offset`, and `from_beginning=True`.
- `text-first parsing burden`: readable outputs remain useful, but continuation state, recovery
  cues, hit markers, and topic labels still have to be parsed from prose.
- `generic server-boundary failure collapse`: escaped failures still degrade to `Tool <name> failed`,
  which discards richer handler-local recovery guidance at the boundary.

## Current-Surface Role Inventory For Medium Planning

The next implementation milestone should use the following role inventory as its starting posture.
These labels are not final product names; they are sequencing signals for what should stay primary,
what should become secondary, what is a merge candidate, and what is a future-removal candidate.

| Surface element | Current job | Medium posture | Why it matters next |
| --- | --- | --- | --- |
| `ListMessages` | Core read surface for dialog and topic reads | `primary` | This remains the main user-task surface for conversation reading, but it should absorb lower-burden navigation and clearer continuation framing. |
| `SearchMessages` | Core search surface with hit-local context | `primary` | This stays a primary user-task surface, but its navigation contract should move closer to the read path. |
| `GetUserInfo` | User inspection and shared-chat context | `primary` | This remains a direct user-task surface for inspect-style jobs, even if later Maximal work folds it into a broader inspect capability. |
| `GetMyAccount` | Confirm active account context | `secondary` | Useful operator context, but not a common first-step workflow driver under Medium. |
| `GetUsageStats` | Show privacy-safe local telemetry | `secondary` | Keep visible, but do not let telemetry shape the primary workflow surface. |
| `ListDialogs` | Dialog discovery and cache warmup | `secondary` | Discovery remains available, but Medium should demote it from the default first move on ordinary reads and searches. |
| `ListTopics` | Forum-topic discovery and state fidelity | `secondary` | Topic fidelity stays preserved, but common forum reads should not require this helper step by default. |
| `discovery-first flow` | Inventory first, then perform the real job | `future-removal` | Medium should reduce this as the default interaction pattern so the model can ask for the job directly. |
| `topic-selection flow` | `ListTopics` before `ListMessages(topic=...)` | `merge` | Topic catalog knowledge remains real, but common thread reads should move toward one more direct workflow. |
| `split continuation model` | Separate read and search paging concepts | `merge` | `next_cursor`, `next_offset`, and `from_beginning=True` should converge toward one coherent navigation model. |
| `generic boundary-failure surface` | Server wraps escaped errors generically | `future-removal` | Medium should narrow the gap between rich handler recovery and generic boundary wrapping. |

## Medium-Era Implementation Reading

For the next build milestone, the practical posture is:

- keep `ListMessages`, `SearchMessages`, and `GetUserInfo` as the clearest primary user-task
  surfaces while reshaping their contracts;
- demote `ListDialogs`, `ListTopics`, `GetMyAccount`, and `GetUsageStats` into helper or operator
  roles where appropriate;
- treat discovery-first flow, split continuation, and generic boundary failure collapse as
  explicit reduction targets;
- sequence Medium work so any capability-layer or adapter choices make a later Maximal merge of
  read/search/inspect roles cheaper rather than harder.
