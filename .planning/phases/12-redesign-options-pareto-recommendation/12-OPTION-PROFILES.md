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
| Maximal Path | Pending population in Task 3. | Pending population in Task 3. | Pending population in Task 3. | Pending population in Task 3. | Pending population in Task 3. | Pending population in Task 3. |

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

## Public Contract Delta Inventory

| Current surface element | Current role | Minimal Path | Medium Path | Maximal Path | Rationale | Affected invariants |
| --- | --- | --- | --- | --- | --- | --- |
| `GetMyAccount` | Confirm which Telegram account is active. | `reshape` | `reshape` | pending | Keep the one-call job, but expose cleaner success/failure metadata so account-state checks require less prose parsing. | read-only scope; stateful runtime reality |
| `GetUsageStats` | Summarize local privacy-safe telemetry. | `reshape` | `reshape` | pending | Preserve the telemetry tool, but tighten its output contract so key metrics are easier to read without widening telemetry scope. | privacy-safe telemetry; stateful runtime reality |
| `GetUserInfo` | Resolve a natural-name user and show profile context. | `reshape` | `reshape` | pending | Keep user lookup separate, but improve metadata and retry guidance so ambiguity recovery stays explicit with less prompt friction. | explicit ambiguity handling; recovery-critical caches |
| `ListDialogs` | Discover reachable chats and warm caches. | `keep` | `demote` | pending | Discovery remains important, but the medium path stops treating it as the default first move for common read/search jobs. | stateful runtime reality; recovery-critical caches |
| `ListMessages` | Read one dialog or topic with pagination and recovery. | `reshape` | `reshape` | pending | Reading remains core, but the medium path refocuses it on the user-visible job and absorbs more workflow guidance directly. | read-only scope; stateful runtime reality; recovery-critical caches |
| `ListTopics` | Discover forum topics before topic-scoped reads. | `keep` | `demote` | pending | Topic fidelity stays preserved, but topic lookup becomes a secondary support surface instead of a routine prerequisite. | recovery-critical caches; explicit ambiguity handling |
| `SearchMessages` | Search one dialog with local hit context. | `reshape` | `reshape` | pending | Preserve the search capability, but align its continuation and entry shape more closely with read workflows. | read-only scope; stateful runtime reality |
| `discovery-first flow` | Often `ListDialogs` before the actual read or search. | `keep` | `reshape` | pending | The medium path reduces helper-step burden by letting main workflows attempt the job first and fall back to discovery when needed. | stateful runtime reality; recovery-critical caches |
| `disambiguation retry flow` | Retry with exact dialog, topic, sender, or user after candidate output. | `reshape` | `reshape` | pending | The safe behavior is worth keeping, but the retry instructions can be more consistent across tools and later in the workflow. | explicit ambiguity handling; recovery-critical caches |
| `topic-selection flow` | Forum reads commonly require `ListTopics` before `ListMessages(topic=...)`. | `keep` | `reshape` | pending | Common topic reads should become more direct while still preserving deleted/inaccessible-topic fidelity. | recovery-critical caches; explicit ambiguity handling |
| `pagination flow` | Reading and search use different continuation mechanics. | `reshape` | `reshape` | pending | This is one of the clearest burden reducers: align navigation language so read and search feel capability-oriented instead of tool-specific. | stateful runtime reality; read-only scope |
| `text-first result parsing` | Continuation state and recovery cues are embedded in readable text. | `reshape` | `reshape` | pending | Preserve readable text, but make high-signal cues more explicit and consistently placed, potentially with light structure. | privacy-safe telemetry; read-only scope |
| `generic server-boundary failure behavior` | Escaped failures collapse to `Tool <name> failed`. | `reshape` | `reshape` | pending | Remove needless loss of context while preserving safe failure behavior and not leaking sensitive internals. | privacy-safe telemetry; stateful runtime reality |
| `dialog` | Natural-name chat selector used by several tools. | `keep` | `keep` | pending | The natural-name selector is core to the product value and should survive even if workflow entry points change. | recovery-critical caches; explicit ambiguity handling |
| `topic` | Natural-name thread selector for forum reads. | `keep` | `keep` | pending | Keep the explicit topic concept because it carries important thread-state and deleted-topic semantics, even if selection becomes more embedded in read flows. | recovery-critical caches; explicit ambiguity handling |
| `sender` | Optional read filter within `ListMessages`. | `reshape` | `reshape` | pending | Clarify filter semantics and retry guidance without losing sender-scoped reads. | explicit ambiguity handling |
| `cursor` | Backward or replay-style continuation token for reads. | `reshape` | `rename` | pending | Normalize navigation so the primary continuation cue is shared across workflow-shaped read and search jobs. | stateful runtime reality |
| `offset` | Search continuation token. | `rename` | `rename` | pending | Move search away from a separate continuation vocabulary without deleting paged search capability. | stateful runtime reality |
| `from_beginning` | Oldest-first read mode for replay-style reading. | `reshape` | `reshape` | pending | Keep the capability, but teach it as part of a coherent read mode instead of an extra pagination quirk. | read-only scope; stateful runtime reality |
| `exclude_archived` | Scope control for archived dialogs. | `keep` | `reshape` | pending | Preserve archived-scope control, but possibly fold it into a broader conversation-selection story instead of exposing it only as discovery jargon. | stateful runtime reality |
| `ignore_pinned` | Discovery ordering/scope control for pinned dialogs. | `keep` | `demote` | pending | Keep available, but treat it as secondary because it matters less than the main workflow burden. | stateful runtime reality |
| `unread` | Filter for unread-only message reads. | `keep` | `keep` | pending | Preserve unread filtering because tests show topic-scoped unread behavior is subtle and already valuable. | read-only scope; recovery-critical caches |
