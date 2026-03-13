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
