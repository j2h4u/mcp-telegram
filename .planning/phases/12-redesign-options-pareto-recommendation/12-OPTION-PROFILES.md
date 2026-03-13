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
| Medium Path | Pending population in Task 2. | Pending population in Task 2. | Pending population in Task 2. | Pending population in Task 2. | Pending population in Task 2. | Pending population in Task 2. |
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

## Public Contract Delta Inventory

| Current surface element | Current role | Minimal Path | Medium Path | Maximal Path | Rationale | Affected invariants |
| --- | --- | --- | --- | --- | --- | --- |
| `GetMyAccount` | Confirm which Telegram account is active. | `reshape` | pending | pending | Keep the one-call job, but expose cleaner success/failure metadata so account-state checks require less prose parsing. | read-only scope; stateful runtime reality |
| `GetUsageStats` | Summarize local privacy-safe telemetry. | `reshape` | pending | pending | Preserve the telemetry tool, but tighten its output contract so key metrics are easier to read without widening telemetry scope. | privacy-safe telemetry; stateful runtime reality |
| `GetUserInfo` | Resolve a natural-name user and show profile context. | `reshape` | pending | pending | Keep user lookup separate, but improve metadata and retry guidance so ambiguity recovery stays explicit with less prompt friction. | explicit ambiguity handling; recovery-critical caches |
| `ListDialogs` | Discover reachable chats and warm caches. | `keep` | pending | pending | Discovery remains a first-class tool because it still teaches real reachable names and archived scope. | stateful runtime reality; recovery-critical caches |
| `ListMessages` | Read one dialog or topic with pagination and recovery. | `reshape` | pending | pending | This is the heaviest current burden, so minimal cleanup should normalize continuation language and reduce prose-only state leakage without changing the tool boundary. | read-only scope; stateful runtime reality; recovery-critical caches |
| `ListTopics` | Discover forum topics before topic-scoped reads. | `keep` | pending | pending | Keep topic discovery as a distinct tool because topic-state fidelity is a current strength worth preserving in the low-risk path. | recovery-critical caches; explicit ambiguity handling |
| `SearchMessages` | Search one dialog with local hit context. | `reshape` | pending | pending | Preserve the dedicated search entry point, but align continuation cues and result framing more closely with reading flows. | read-only scope; stateful runtime reality |
| `discovery-first flow` | Often `ListDialogs` before the actual read or search. | `keep` | pending | pending | Minimal change accepts discovery-first choreography as real, but can reduce burden by improving descriptions and follow-up hints. | stateful runtime reality; recovery-critical caches |
| `disambiguation retry flow` | Retry with exact dialog, topic, sender, or user after candidate output. | `reshape` | pending | pending | The safe behavior is worth keeping, but the retry instructions can be more consistent across tools. | explicit ambiguity handling; recovery-critical caches |
| `topic-selection flow` | Forum reads commonly require `ListTopics` before `ListMessages(topic=...)`. | `keep` | pending | pending | Minimal change keeps topic lookup distinct because it preserves current topic-state semantics and lowers risk. | recovery-critical caches; explicit ambiguity handling |
| `pagination flow` | Reading and search use different continuation mechanics. | `reshape` | pending | pending | This is one of the clearest low-risk burden reducers: normalize continuation naming and guidance while keeping paging behavior intact. | stateful runtime reality; read-only scope |
| `text-first result parsing` | Continuation state and recovery cues are embedded in readable text. | `reshape` | pending | pending | Preserve readable text, but make high-signal cues more explicit and consistently placed. | privacy-safe telemetry; read-only scope |
| `generic server-boundary failure behavior` | Escaped failures collapse to `Tool <name> failed`. | `reshape` | pending | pending | Remove needless loss of context while preserving safe failure behavior and not leaking sensitive internals. | privacy-safe telemetry; stateful runtime reality |
| `dialog` | Natural-name chat selector used by several tools. | `keep` | pending | pending | The natural-name selector is core to the product value and does not need structural change in the low-risk path. | recovery-critical caches; explicit ambiguity handling |
| `topic` | Natural-name thread selector for forum reads. | `keep` | pending | pending | Keep the explicit topic selector because it carries important thread-state and deleted-topic semantics. | recovery-critical caches; explicit ambiguity handling |
| `sender` | Optional read filter within `ListMessages`. | `reshape` | pending | pending | Clarify filter semantics and retry guidance without moving the filter to a new workflow model. | explicit ambiguity handling |
| `cursor` | Backward or replay-style continuation token for reads. | `reshape` | pending | pending | Normalize naming and explanation so cursor reuse is less tool-specific. | stateful runtime reality |
| `offset` | Search continuation token. | `rename` | pending | pending | The minimal path can rename or alias this toward the normalized continuation model without merging search into reading. | stateful runtime reality |
| `from_beginning` | Oldest-first read mode for replay-style reading. | `reshape` | pending | pending | Keep the capability, but teach it more clearly as a read mode rather than an extra pagination quirk. | read-only scope; stateful runtime reality |
| `exclude_archived` | Scope control for archived dialogs. | `keep` | pending | pending | Archived-scope control is useful and already evidence-backed in tests, so minimal change should preserve it. | stateful runtime reality |
| `ignore_pinned` | Discovery ordering/scope control for pinned dialogs. | `keep` | pending | pending | Keep as-is because it is a low-cost knob with limited burden compared with larger workflow issues. | stateful runtime reality |
| `unread` | Filter for unread-only message reads. | `keep` | pending | pending | Preserve unread filtering because tests show topic-scoped unread behavior is subtle and already valuable. | read-only scope; recovery-critical caches |
