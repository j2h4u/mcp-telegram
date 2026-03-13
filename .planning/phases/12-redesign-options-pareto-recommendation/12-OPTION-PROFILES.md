# Phase 12 Option Profiles

Last updated: 2026-03-13

This artifact turns the frozen Phase 11 redesign pressure into three concrete public-contract
alternatives. It stays grounded in the reflected seven-tool baseline from
[10-BROWNFIELD-BASELINE.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/10-evidence-base-audit-frame/10-BROWNFIELD-BASELINE.md),
the comparative burden inventory in
[11-COMPARATIVE-AUDIT.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md),
and the locked comparison vocabulary in
[12-COMPARISON-FRAME.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/12-redesign-options-pareto-recommendation/12-COMPARISON-FRAME.md).

## Option Matrix

| Path | Public-contract shape | Burden reduction focus | Expected impact | Migration risk | Implementation scope | Preserved invariants |
| --- | --- | --- | --- | --- | --- | --- |
| Minimal Path | Preserve the seven-tool topology and current role split between account, discovery, reading, topic, search, and telemetry tools. | Metadata cleanup, continuation normalization, and error-surface cleanup with only small schema edits. | Moderate improvement on repeated read/search flows because the model spends less effort parsing tool docs and continuation tokens. | Low, because existing tool names and most call shapes remain intact. | Small-to-medium follow-on work in `tools.py`, `server.py`, formatter text, and contract tests. | Read-only scope, privacy-safe telemetry, stateful runtime reality, recovery-critical caches, and explicit ambiguity handling remain default-preserve. |
| Medium Path | Reshape the model-facing contract around capability-oriented workflows, with helper tools selectively consolidated or demoted rather than kept as first-class starting points. | Reduce helper-step burden through selective consolidation, shared continuation language, and more direct read/search entry points. | High improvement on common tasks because the model can ask for the job more directly instead of assembling the workflow from helper calls. | Medium, because primary tool roles change even though the read-only and stateful runtime baseline is preserved. | Medium-to-large follow-on work across tool schemas, compatibility shims, result framing, and tests. | Read-only scope, privacy-safe telemetry, stateful runtime reality, recovery-critical caches, and explicit ambiguity handling stay preserved even as the public workflow surface is reframed. |
| Maximal Path | Rewrite the public surface around a smaller set of merged workflow tools with larger role changes and more structured result contracts. | Aggressively cut helper-step burden by tool-merging, result-shape changes, and deeper workflow abstraction. | Very high on idealized model workflows because discovery, reading, search, and thread navigation become much more direct. | High, because tool names, role boundaries, and response shapes all move materially. | Large follow-on work touching schemas, handlers, compatibility policy, result formatting, and rollout strategy. | Read-only scope, privacy-safe telemetry, stateful runtime reality, recovery-critical caches, and explicit ambiguity handling should still be preserved by default, but this path stresses them the most. |

## Minimal Path

The Minimal Path is the lowest-risk baseline, but it is not a disguised no-op. It keeps the
current seven-tool topology intact:
`GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`, `ListMessages`, `ListTopics`, and
`SearchMessages`. The change is that the public contract gets cleaned where Phase 11 found burden
without forcing a topology rewrite. This path keeps read, search, and topic capabilities as
separate tools, but makes them cheaper to use through metadata cleanup, continuation normalization,
small contract edits, and error-surface cleanup.

### Why this is the true low-risk baseline

- It preserves the current tool map and the current stateful/read-only runtime assumptions proven in
  [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py),
  [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py), and
  the Phase 10 baseline.
- It directly targets the main Phase 11 leaks without pretending those leaks require a full
  capability rewrite:
  mixed continuation contracts (`next_cursor`, `next_offset`, `from_beginning=True`), verbose
  prose-first tool guidance, and generic server-boundary failure collapse to `Tool <name> failed`.
- It narrows helper cost around the existing workflows instead of redefining the workflows. The
  model still uses dialog discovery, message reads, forum-topic lookups, and search as separate
  jobs; it just receives a cleaner contract for them.

### Expected impact

The main impact is friction removal on the already-common workflows identified in
[11-COMPARATIVE-AUDIT.md](/home/j2h4u/repos/j2h4u/mcp-telegram/.planning/phases/11-current-surface-comparative-audit/11-COMPARATIVE-AUDIT.md).
Models should spend less effort remembering which continuation token belongs to which tool,
deciding whether a docstring or parameter name is teaching the right next step, and recovering from
escaped failures that currently lose handler-level context at the server boundary.

This path does not eliminate helper-step choreography for forum reading or discovery-first flows.
Instead, it makes those helper steps more legible and less error-prone.

### Migration risk

Migration risk is low because callers still see the same named seven-tool topology and the same
overall split between discovery, reading, topic, and search entry points. The likely breakage
surface is mostly prompt-level or schema-level interpretation:

- parameter naming cleanup or clearer field descriptions
- continuation normalization between `cursor` and `offset` conventions
- richer surfaced failure text instead of generic boundary wrapping
- lightweight structure additions to readable text responses

No option here requires dropping the existing reflection-based discovery model or changing the
read-only/stateful operating baseline.

### Implementation scope

Implementation scope is intentionally bounded. The expected code footprint is mostly contract-facing
cleanup in:

- [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py) for docstrings, schema wording, result framing, and normalized continuation cues
- [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py) for error-surface cleanup around generic boundary wrapping
- [src/mcp_telegram/formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py) for lighter parsing burden without discarding readable text
- [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py) and related test anchors for contract verification

This is a contract-tuning pass, not a new architecture.

### Preserved invariants

The Minimal Path explicitly preserves the invariants Phase 10 and Phase 11 marked as default-safe:

- read-only Telegram scope
- privacy-safe telemetry and aggregate-only analytics
- stateful runtime reality, including cached client/session and SQLite-backed caches
- recovery-critical topic and entity metadata, including deleted and previously inaccessible topic state
- explicit ambiguity handling instead of silent auto-resolution

## Medium Path

The Medium Path is the likely Pareto-candidate range because it keeps the read-only and stateful
runtime baseline, but materially reshapes the model-facing surface around capability-oriented
workflows instead of around the current helper tool boundaries. This path assumes the public
contract should optimize for jobs like "read a conversation", "search a conversation", and
"inspect account or user context" first, while still preserving the runtime facts that make the
current implementation reliable.

This is meaningfully different from the Minimal Path. The goal here is not only metadata cleanup or
continuation normalization. The goal is to reduce helper-step burden through selective
consolidation or re-framing of the public contract:

- discovery remains available, but it is no longer the assumed starting point for many reads
- topic selection remains real state, but common forum reads should not require a separate public
  tool hop every time
- search and read continuation should feel like variations of one navigation model instead of two
  adjacent but different contracts
- handler-level recovery remains explicit, but the surface should teach the main workflow before the
  model needs to learn the helper choreography

### Expected impact

Expected impact is high on the workflows that Phase 11 flagged as continuation-heavy. A medium path
should let the model express the user-visible job first and only surface helper mechanics when the
job truly needs them. That especially improves:

- forum-thread reading, where `ListTopics` becomes a secondary support surface rather than a common
  mandatory prerequisite
- message reading and replay flows, where continuation and oldest-first reading become one coherent
  navigation model
- search continuation, where the public contract stops forcing the model to remember a separate
  paging vocabulary for a closely related task

### Migration risk

Migration risk moves from low to medium because this path starts changing the primary workflow
shape. Tool compatibility can still be preserved, but agent expectations would shift:

- helper tools such as `ListDialogs` and `ListTopics` may become demoted compatibility surfaces
  instead of the primary contract
- `ListMessages` and `SearchMessages` likely absorb more of the discovery and continuation burden
- result framing may add lightweight structure or stronger sectioning so recovery and continuation
  state are easier to consume

This is still not a full rewrite. It does not require abandoning reflection-based discovery,
read-only scope, or cache-backed recovery behavior.

### Implementation scope

Implementation scope is medium-to-large because the public contract becomes more workflow-shaped,
not just better worded. The likely code and test impact includes:

- reshaping `ToolArgs` docstrings and schemas in
  [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py)
- teaching compatibility behavior or secondary-tool posture in
  [src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py)
  and tool descriptions
- aligning continuation logic currently split across
  [src/mcp_telegram/pagination.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py)
  and tool-specific response text
- updating the high-signal contract tests in
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py)

### Preserved invariants

The Medium Path still preserves the same non-negotiable baseline:

- read-only Telegram access
- privacy-safe telemetry
- stateful runtime and cache-backed resolution
- recovery-critical topic and entity metadata
- explicit ambiguity handling, even if the retry points are surfaced later in the workflow

## Maximal Path

The Maximal Path allows the largest public-contract rewrite that still tries to preserve the
project's default invariants. This path treats the current helper-heavy surface as evidence that the
model should see a much smaller number of workflow entry points, with larger tool-merging, role
changes, and result-shape changes than either the minimal or medium option.

In practice, that means the public surface could collapse toward a few primary capabilities:

- a merged conversation-navigation tool that can discover, select, and read dialogs or forum
  threads without exposing the current discovery-first choreography
- a merged search-and-navigate tool or unified conversation tool that treats search as one mode of
  navigation rather than a separate top-level contract
- a more structured inspect/context surface for account, peer, and telemetry information instead of
  today's mostly prose-shaped responses

### Expected impact

Expected impact is very high if the redesign lands cleanly. The model would need far fewer helper
steps, and many current contract leaks would disappear from the primary surface:

- discovery-first flow becomes optional or invisible for common jobs
- topic-selection flow is absorbed into the main conversation-navigation contract
- result-shape changes can expose continuation, topic state, and recovery cues in a more directly
  machine-usable format
- read/search/tool boundaries become more intuitive for new agents because the surface aligns to
  jobs instead of implementation-era tool splits

### Migration risk

Migration risk is high because this path changes names, boundaries, and likely expectations all at
once. The main risks are:

- reflected schema drift for long-lived runtimes and clients expecting the current seven-tool map
- compatibility complexity if old and new tool names must coexist during rollout
- degraded recovery quality if tool-merging hides ambiguity, topic-state semantics, or invalid-token
  recovery behind too much abstraction
- operational risk from changing result-shape contracts while the server still uses process-start
  reflection snapshots

### Implementation scope

Implementation scope is large. A maximal path is not only more editing in
[src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py); it
also implies a heavier migration strategy across the reflection boundary in
[src/mcp_telegram/server.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/server.py), the
continuation helpers in
[src/mcp_telegram/pagination.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/pagination.py),
the text contract in
[src/mcp_telegram/formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py),
and the contract tests in
[tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py).

This path would likely need compatibility shims, phased exposure, or temporary dual-contract
support. That implementation burden is why it should be viewed as the upper bound of redesign
ambition, not the default answer.

### Preserved invariants

The Maximal Path still defaults to preserving:

- read-only Telegram access
- privacy-safe telemetry
- stateful runtime and local cache reality
- recovery-critical topic/entity metadata
- explicit ambiguity handling

### Invariants stressed most

This path stresses several invariants even if it does not intentionally break them:

- `explicit ambiguity handling` is easiest to accidentally weaken when merged tools try too hard to
  infer intent
- `recovery-critical caches` matter more, not less, when the public surface hides helper steps and
  must recover internally
- `stateful runtime reality` becomes harder to reason about when one tool can span discovery,
  selection, reading, and continuation
- `privacy-safe telemetry` must stay guarded if richer structured results tempt deeper runtime
  instrumentation during rollout

## Public Contract Delta Inventory

| Current surface element | Current role | Minimal Path | Medium Path | Maximal Path | Rationale | Affected invariants |
| --- | --- | --- | --- | --- | --- | --- |
| `GetMyAccount` | Confirm which Telegram account is active. | `reshape` | `reshape` | `merge` | Keep the job, but the maximal path can fold it into a broader inspect/context surface that returns structured operator state. | read-only scope; stateful runtime reality |
| `GetUsageStats` | Summarize local privacy-safe telemetry. | `reshape` | `reshape` | `demote` | Preserve the capability, but a maximal redesign likely treats telemetry as secondary operator context rather than a primary top-level tool. | privacy-safe telemetry; stateful runtime reality |
| `GetUserInfo` | Resolve a natural-name user and show profile context. | `reshape` | `reshape` | `merge` | Keep user lookup value, but a maximal path can fold it into a broader inspect/context surface with shared entity resolution. | explicit ambiguity handling; recovery-critical caches |
| `ListDialogs` | Discover reachable chats and warm caches. | `keep` | `demote` | `merge` | Discovery remains important, but higher-ambition paths stop treating it as the default first move for common read/search jobs. | stateful runtime reality; recovery-critical caches |
| `ListMessages` | Read one dialog or topic with pagination and recovery. | `reshape` | `reshape` | `rename` | Reading remains core, but the more aggressive paths can reframe it as a smaller set of workflow-oriented conversation tools. | read-only scope; stateful runtime reality; recovery-critical caches |
| `ListTopics` | Discover forum topics before topic-scoped reads. | `keep` | `demote` | `merge` | Topic fidelity stays preserved, but higher-ambition paths stop requiring a separate topic catalog tool for common thread reads. | recovery-critical caches; explicit ambiguity handling |
| `SearchMessages` | Search one dialog with local hit context. | `reshape` | `reshape` | `merge` | Preserve the search capability, but the maximal path can treat search as one mode of conversation navigation instead of a separate top-level tool. | read-only scope; stateful runtime reality |
| `discovery-first flow` | Often `ListDialogs` before the actual read or search. | `keep` | `reshape` | `remove` | The higher-ambition paths aim to make discovery-first choreography optional rather than the default workflow. | stateful runtime reality; recovery-critical caches |
| `disambiguation retry flow` | Retry with exact dialog, topic, sender, or user after candidate output. | `reshape` | `reshape` | `reshape` | The safe behavior is worth keeping across all paths, but aggressive paths must surface ambiguity without falling into silent auto-picks. | explicit ambiguity handling; recovery-critical caches |
| `topic-selection flow` | Forum reads commonly require `ListTopics` before `ListMessages(topic=...)`. | `keep` | `reshape` | `merge` | Common topic reads should become more direct while still preserving deleted/inaccessible-topic fidelity. | recovery-critical caches; explicit ambiguity handling |
| `pagination flow` | Reading and search use different continuation mechanics. | `reshape` | `reshape` | `merge` | This is one of the clearest burden reducers: the more ambitious the path, the more it should collapse navigation into one continuation model. | stateful runtime reality; read-only scope |
| `text-first result parsing` | Continuation state and recovery cues are embedded in readable text. | `reshape` | `reshape` | `reshape` | Preserve readability, but aggressive paths can shift farther toward structured result-shape changes while still keeping human-legible context. | privacy-safe telemetry; read-only scope |
| `generic server-boundary failure behavior` | Escaped failures collapse to `Tool <name> failed`. | `reshape` | `reshape` | `remove` | All paths should remove needless context loss, and the maximal path has the most reason to eliminate generic boundary failure behavior entirely. | privacy-safe telemetry; stateful runtime reality |
| `dialog` | Natural-name chat selector used by several tools. | `keep` | `keep` | `rename` | The natural-name selector is core to the product value, but maximal redesign may rename it to match a broader conversation-centric contract. | recovery-critical caches; explicit ambiguity handling |
| `topic` | Natural-name thread selector for forum reads. | `keep` | `keep` | `reshape` | Keep the explicit topic concept because it carries important thread-state and deleted-topic semantics, even if selection becomes more embedded in merged read flows. | recovery-critical caches; explicit ambiguity handling |
| `sender` | Optional read filter within `ListMessages`. | `reshape` | `reshape` | `reshape` | Clarify filter semantics and retry guidance without losing sender-scoped reads. | explicit ambiguity handling |
| `cursor` | Backward or replay-style continuation token for reads. | `reshape` | `rename` | `merge` | Normalize navigation so ambitious paths can collapse read/search paging into one shared continuation token. | stateful runtime reality |
| `offset` | Search continuation token. | `rename` | `rename` | `merge` | The more ambitious the path, the less reason remains for a separate search-only continuation vocabulary. | stateful runtime reality |
| `from_beginning` | Oldest-first read mode for replay-style reading. | `reshape` | `reshape` | `rename` | Keep the capability, but aggressive paths should express it as a general read-order mode rather than a special-case flag. | read-only scope; stateful runtime reality |
| `exclude_archived` | Scope control for archived dialogs. | `keep` | `reshape` | `demote` | Preserve archived-scope control, but higher-ambition paths can push it behind a broader conversation-selection contract. | stateful runtime reality |
| `ignore_pinned` | Discovery ordering/scope control for pinned dialogs. | `keep` | `demote` | `remove` | This knob matters less than the main workflow burden and is the easiest to drop from a radically simplified primary surface. | stateful runtime reality |
| `unread` | Filter for unread-only message reads. | `keep` | `keep` | `reshape` | Preserve unread filtering because tests show topic-scoped unread behavior is subtle and already valuable, but a maximal path may express it as a broader read mode. | read-only scope; recovery-critical caches |

## Cross-Option Summary

| Comparison axis | Minimal Path | Medium Path | Maximal Path |
| --- | --- | --- | --- |
| burden reduction | Lowest absolute gain, but it removes obvious friction around metadata, continuation naming, and failure wording. | Strong gain because the public contract starts from capability-oriented workflows and reduces helper-step burden directly. | Highest potential gain because discovery, topic selection, and navigation can disappear into merged workflows. |
| contract change size | Small. Existing tool names and most roles survive. | Medium. Primary workflow roles shift, but the current surface can plausibly survive as compatibility or secondary tooling. | Large. Tool-merging, role changes, and result-shape changes redefine the public contract. |
| operational risk | Low. Reflected schemas and long-lived runtimes mostly see contract cleanup. | Medium. Runtime and client expectations need migration handling, but the current operating model still shows through. | High. Reflection snapshots, rollout compatibility, and result-shape drift become first-class operational concerns. |

Minimal is the safest baseline, but it leaves the discovery-first and topic-helper burden mostly in
place. Maximal offers the largest burden reduction, but it also takes on the largest contract and
operational risk. Medium is the clearest middle shape: it removes a meaningful share of helper-step
burden without forcing a full surface rewrite, which is why it reads like the most plausible
Pareto-candidate range going into the recommendation plan.
