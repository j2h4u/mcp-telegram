# Phase 13: Implementation Sequencing & Decision Memo - Context

**Gathered:** 2026-03-13
**Status:** Ready for planning

<domain>
## Phase Boundary

Turn the completed v1.2 research into a decision-ready implementation brief for the follow-up
milestone. This phase should sequence the chosen redesign path, define validation checkpoints,
surface open questions, and make the next build milestone plannable without rerunning the audit or
option comparison. This phase does not reopen the redesign choice and does not preserve old
contracts just because they already exist.

</domain>

<decisions>
## Implementation Decisions

### Recommendation posture
- Treat the Phase 12 `Medium Path` recommendation as locked for the next milestone.
- Treat that `Medium Path` as a migration stage toward a later `Maximal Path`, not as the final
  steady-state public contract.
- Keep the rejected-alternative reasoning visible in the memo so later planning does not drift back
  toward Minimal-by-default cleanup or Maximal-by-default rewrite pressure.

### Compatibility posture
- Backward compatibility is not a planning constraint for the follow-up implementation milestone.
- Do not add compatibility shims, alias tools, or dual-contract support unless a later explicit
  decision says they are required.
- Favor the cleanest contract and sequencing plan over preserving the reflected seven-tool public
  shape.

### Architecture preparation for later Maximal
- Sequence the implementation so `Medium` builds internal foundations that make a later `Maximal`
  redesign cheaper.
- Explicitly prefer:
  - a capability-oriented internal layer over tool-name-shaped internals
  - one unified continuation/navigation model over separate read/search paging concepts
  - a separation between public contract adapters and execution paths
  - normalized result framing that reduces parsing burden and can evolve toward more structured
    outputs later
- The memo should call out where current helper tools become primary, secondary, merged candidates,
  or removable later.

### Preserved invariants
- Keep read-only Telegram scope as a hard invariant.
- Keep privacy-safe telemetry and continue to avoid message-content logging.
- Keep explicit ambiguity handling; simpler workflows must not turn into silent auto-picks.
- Keep stateful-runtime reality, cache-backed recovery, and topic/entity fidelity as design
  constraints during sequencing.

### Planning posture
- Phase 13 should optimize for clean sequencing and validation, not for minimizing short-term code
  churn.
- Public-contract changes should be sequenced before deeper internal cleanup only when they reduce
  migration ambiguity; otherwise planners may front-load enabling architecture if that makes the
  later Maximal step cheaper.
- The memo should explicitly distinguish:
  - what must land for a strong `Medium` milestone
  - what should be prepared now to avoid rework for `Maximal`
  - what can safely wait for the future `Maximal` phase

### Claude's Discretion
- Exact milestone slicing, task granularity, and validation artifact format
- Whether the recommended sequence is grouped by capability, migration risk, or validation order
- Exact terminology for any proposed capability-layer naming, as long as it preserves the locked
  decisions above

</decisions>

<specifics>
## Specific Ideas

- The user explicitly allows a future `Maximal` redesign and wants today's planning to keep that
  path cheap.
- The user explicitly does not require backward compatibility, so the memo should not bias toward
  shims or legacy preservation.
- The most valuable Phase 13 outcome is a plan that avoids doing a second large internal refactor
  when the project later moves from `Medium` to `Maximal`.

</specifics>

<code_context>
## Existing Code Insights

### Reusable Assets
- [12-REDESIGN-OPTIONS.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/12-redesign-options-pareto-recommendation/12-REDESIGN-OPTIONS.md):
  locked recommendation, rejected alternatives, and Phase 13 handoff posture.
- [12-OPTION-PROFILES.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/12-redesign-options-pareto-recommendation/12-OPTION-PROFILES.md):
  explicit contract deltas for Minimal, Medium, and Maximal.
- [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py):
  current tool boundaries, schemas, and result-shape conventions that sequencing must transform.
- [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py):
  reflection-based exposure boundary and runtime coupling to public tool schemas.
- [src/mcp_telegram/pagination.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py):
  current split continuation mechanics that should converge under the chosen path.

### Established Patterns
- Planning artifacts in this repo are the authoritative handoff layer for GSD agents.
- The live runtime is reflection-based and stateful, so sequencing must consider deploy/restart
  validation rather than only static code changes.
- Topic handling, ambiguity recovery, and privacy-safe telemetry are preserved strengths, not
  incidental implementation details.

### Integration Points
- Phase 13 planning should consume the recommendation and delta inventory from Phase 12 directly.
- The next implementation milestone should be able to plan from this memo without reopening Phase
  10-12 research.
- Validation guidance should include both tests and live reflected-surface checks.

</code_context>

<deferred>
## Deferred Ideas

- Full `Maximal Path` execution is deferred to a later phase or milestone; Phase 13 should prepare
  for it but not redefine the current chosen path.
- Any decision to reintroduce backward compatibility is deferred until explicitly requested.

</deferred>

---

*Phase: 13-implementation-sequencing-decision-memo*
*Context gathered: 2026-03-13*
