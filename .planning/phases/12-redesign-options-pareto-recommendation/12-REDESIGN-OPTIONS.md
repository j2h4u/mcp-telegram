# Phase 12 Redesign Options and Pareto Recommendation

Last updated: 2026-03-13

This is the primary Phase 12 deliverable. It turns the Phase 12 comparison frame and option
profiles into one decision-friendly artifact that a maintainer can hand directly to Phase 13
without re-reading every intermediate note.

## Scope and Decision Posture

- This document is a bounded redesign comparison for the public `mcp-telegram` MCP surface, not an
  implementation plan.
- The judgment posture is inherited from Phase 10 and Phase 11: named evidence, direct brownfield
  anchors, and explicit preservation of invariants unless an option can justify a change.
- The comparison goal is to reduce model burden around discovery, reading, search, topic handling,
  pagination, and recovery without redefining the product into a new system.
- The question is not which option is most ambitious. The question is which option best improves
  the model-facing contract while staying safe against the reflected runtime and public contract
  that exist today.

## Frozen Baseline From Phase 11

The frozen baseline for this decision comes from Phase 11 and stays anchored to the reflected
runtime inventory seen through `list-tools` on 2026-03-13 plus the concrete brownfield anchors in
`server.py`, `tools.py`, and the contract tests. This document does not reopen discovery.

The current public surface is seven tools:

- `GetMyAccount`
- `GetUsageStats`
- `GetUserInfo`
- `ListDialogs`
- `ListMessages`
- `ListTopics`
- `SearchMessages`

The relevant Phase 11 synthesis from
[11-COMPARATIVE-AUDIT.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md)
and
[10-BROWNFIELD-BASELINE.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md)
is stable:

- the surface is workflow-capable but continuation-heavy
- discovery and forum reading often start with helper-step choreography before the user-visible job
- adjacent navigation tasks still split between `next_cursor`, `next_offset`, and
  `from_beginning=True`
- results are readable but text-first, which pushes parsing burden onto the model
- handler-local recovery is often strong, but escaped failures can still collapse at the server
  boundary

Those baseline facts are the decision input. No option gets to claim safety or leverage by
silently discarding them.

## Comparison Dimensions

This recommendation uses the shared Phase 12 comparison dimensions so the conclusion stays tied to
the same vocabulary as the option work.

| Dimension | What matters most here |
| --- | --- |
| user-task fit | Whether the surface lets the model ask for the real job instead of assembling helper steps first |
| continuation-contract simplicity | Whether read and search navigation feel like one coherent model rather than adjacent but different token systems |
| contract delta size | How much public-schema and workflow adaptation the option asks clients and prompts to absorb |
| migration risk | How much risk the option creates for reflected schemas, long-lived runtimes, and existing agent expectations |
| implementation scope | How much Phase 13 sequencing work the option would force before any user benefit lands |
| preserved-strength retention | Whether topic fidelity, action-oriented recovery, and privacy-safe telemetry survive intact |
| recovery quality | Whether ambiguity, invalid continuation, and topic-state failure behavior stay explicit and actionable |
| output-shape burden | Whether the option reduces prose parsing without pretending the system is suddenly a fully structured API |
| state-model impact | Whether the option works with the real stateful runtime instead of assuming statelessness |
| operational/runtime risk | Whether the option increases or decreases stale-runtime and reflection-snapshot mismatch risk |

## Option Matrix

| Path | Core surface move | Main upside | Main cost | Best use of the path |
| --- | --- | --- | --- | --- |
| Minimal Path | Keep the seven-tool topology and clean the contract in place | Low-risk reduction in metadata confusion, pagination wording, and generic failure collapse | Leaves the discovery-first and topic-helper choreography mostly intact | Safe cleanup if the goal is contract hygiene without changing the workflow shape |
| Medium Path | Reframe the public surface around capability-oriented workflows while preserving the read-only, stateful baseline | Removes much of the helper-step burden on common read/search/topic jobs | Requires moderate contract changes and compatibility discipline | Best fit when the goal is meaningful burden reduction without a full surface rewrite |
| Maximal Path | Merge more roles and shrink the public surface to a few broader workflow entry points | Highest theoretical burden reduction and the strongest job-shaped contract | Highest migration, rollout, and runtime risk because names, roles, and outputs all move | Useful as the upper-bound stress test, not as the default answer |

In practical terms, Minimal is the safest contract-tuning path, Medium is the strongest
capability-oriented redesign range, and Maximal is the upper-bound rewrite candidate. The key Phase
12 question is whether the Medium Path captures most of the benefit without taking on Maximal's
risk.

## Public Contract Delta Inventory

This inventory preserves the Phase 12 rule that every major current tool, interaction pattern, and
high-signal parameter must be compared explicitly.

| Current surface element | Current role | Minimal Path | Medium Path | Maximal Path | Why the row matters |
| --- | --- | --- | --- | --- | --- |
| `GetMyAccount` | Confirm active Telegram identity | `reshape` | `reshape` | `merge` | Account context stays useful, but higher-ambition paths can fold it into a broader inspect surface |
| `GetUsageStats` | Show local aggregate telemetry | `reshape` | `reshape` | `demote` | Keep privacy-safe telemetry visible without treating it as a primary workflow entry point |
| `GetUserInfo` | Resolve and inspect a person | `reshape` | `reshape` | `merge` | Identity lookup is valuable, but its public role can become a secondary inspect capability |
| `ListDialogs` | Discover reachable conversations and warm caches | `keep` | `demote` | `merge` | Discovery still matters, but Medium and Maximal stop treating it as the common first step |
| `ListMessages` | Read dialog or topic content with continuation | `reshape` | `reshape` | `rename` | Reading remains core, but the public contract can become more workflow-shaped |
| `ListTopics` | Discover exact forum topic choices | `keep` | `demote` | `merge` | Topic fidelity must stay, but common forum reads should not always require a separate helper hop |
| `SearchMessages` | Search a dialog with local hit context | `reshape` | `reshape` | `merge` | Search should feel closer to conversation navigation, especially in higher-ambition paths |
| `discovery-first flow` | Often inventory first, then do the actual read/search job | `keep` | `reshape` | `remove` | This is a core burden driver and one of the biggest differentiators across the options |
| `disambiguation retry flow` | Retry with exact dialog, sender, topic, or user | `reshape` | `reshape` | `reshape` | All paths must preserve safe recovery instead of hiding ambiguity |
| `topic-selection flow` | Often `ListTopics` before `ListMessages(topic=...)` | `keep` | `reshape` | `merge` | Topic reads are one of the clearest places where helper burden shows up |
| `pagination flow` | Reading and search use different continuation models | `reshape` | `reshape` | `merge` | Continuation simplification is one of the highest-leverage redesign opportunities |
| `text-first result parsing` | Continuation state and cues arrive inside readable prose | `reshape` | `reshape` | `reshape` | All paths should reduce parsing burden without losing readable output |
| `generic server-boundary failure behavior` | Escaped failures degrade to `Tool <name> failed` | `reshape` | `reshape` | `remove` | Better recovery is valuable in every path, especially if the contract becomes more abstract |
| `dialog` | Natural-name chat selector | `keep` | `keep` | `rename` | Natural-name selection is core product value and should remain visible |
| `topic` | Natural-name forum-thread selector | `keep` | `keep` | `reshape` | Topic semantics carry recovery-critical state and cannot be flattened away casually |
| `sender` | Optional read filter | `reshape` | `reshape` | `reshape` | Sender filtering should become clearer without losing safe ambiguity handling |
| `cursor` | Read continuation token | `reshape` | `rename` | `merge` | A shared navigation model becomes more plausible as redesign ambition increases |
| `offset` | Search continuation token | `rename` | `rename` | `merge` | Search-specific paging vocabulary is a clear burden source today |
| `from_beginning` | Oldest-first read mode | `reshape` | `reshape` | `rename` | The capability matters, but the flag can be expressed more coherently |
| `exclude_archived` | Discovery scope control | `keep` | `reshape` | `demote` | Important but secondary compared with the core workflow burden |
| `ignore_pinned` | Discovery ordering/scope control | `keep` | `demote` | `remove` | This is one of the easiest knobs to push out of the primary contract |
| `unread` | Unread-only message filter | `keep` | `keep` | `reshape` | Valuable behavior that should survive even if the surface becomes more workflow-shaped |

Across the table, the comparison is stable: Minimal mostly tunes the existing contract, Medium
reframes the public workflow without discarding preserved strengths, and Maximal pays for cleaner
top-level ergonomics with much larger migration and runtime exposure.

## Pareto Recommendation

The recommendation at this stage is directional rather than fully argued: the Medium Path is the
leading candidate for the next milestone because it is the smallest redesign tier that materially
changes the workflow burden identified in Phase 11.

Minimal is credible as a safety baseline, but it mostly preserves the same discovery-first and
topic-helper choreography that
[11-COMPARATIVE-AUDIT.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md)
called out. Maximal offers more theoretical cleanup, but it pushes far harder against the reflected
contract, deployment freshness, and result-shape stability described in
[10-BROWNFIELD-BASELINE.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md).

That leaves Medium as the most promising balance point. It is the first option that directly
changes the model-facing workflow shape rather than just polishing it, but it still stays inside
the current read-only and stateful runtime posture.

## Recommendation Guardrails and Invariants

Any follow-on design work should keep these guardrails explicit:

- `read-only scope` remains the public boundary.
- `privacy-safe telemetry` remains mandatory and must not widen into message-content logging.
- `recovery-critical state` remains a preserved strength, including cache-backed entity and topic
  context.
- `explicit ambiguity handling` remains non-negotiable; reducing retries must not turn into silent
  auto-picks.
- The real `stateful runtime` remains part of the contract, including reflection snapshots,
  persisted caches, and long-lived process behavior.

These guardrails are why Maximal cannot be treated as free upside. The farther the public surface
moves from today's tool and workflow boundaries, the more carefully it must prove that it still
preserves safe recovery, cache-backed fidelity, and deploy-time correctness.

## Phase 13 Handoff Notes

Phase 13 should treat this artifact as a decision input, not as a finished implementation spec.

- Turn the leading option into an implementation-sequencing brief rather than reopening the option
  comparison.
- Sequence public-contract changes before deeper internal cleanup so migration risk stays visible.
- Validate continuation unification, topic-read ergonomics, and failure-surface cleanup against the
  current `server.py` and `tools.py` anchors before any coding plan assumes they are easy.
- Preserve the natural-name contract and topic-state fidelity while deciding which helper tools stay
  primary, become secondary, or move behind compatibility shims.
- Define runtime verification around reflected schemas and restart freshness so a future redesign is
  not judged only by tests but also by the live `list-tools` surface.
