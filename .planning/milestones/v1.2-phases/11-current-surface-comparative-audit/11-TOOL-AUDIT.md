# Phase 11 Tool Audit

Last verified: 2026-03-13

The reflected runtime inventory on 2026-03-13 is authoritative for this audit. It covers exactly
seven public tools: `GetMyAccount`, `GetUsageStats`, `GetUserInfo`, `ListDialogs`,
`ListMessages`, `ListTopics`, and `SearchMessages`.

This artifact reuses the Phase 10 evidence set and the non-numeric `strong` / `mixed` / `weak`
rubric rather than inventing a new method. Each major finding below pairs named Phase 10 evidence
with direct current-surface anchors in reflection, source, or tests.

Leak labels are explicit so later phases can reuse them directly: metadata ambiguity,
text-first parsing burden, helper-step burden, pagination burden, disambiguation burden, and
workflow dependency on other tools.

## Tool Audit Rows

### `GetMyAccount`

- Primary user job: Confirm which Telegram account is active before doing account-scoped work.
- Current contract shape: Reflected metadata exposes an empty input schema and a short description.
  The handler returns one text-first `TextContent` line with `id=`, `name=`, and `username=@...`,
  or an action-oriented authentication message when no Telegram session is available. There is no
  pagination token or follow-up parameter contract.
- Judgment band: `strong`
- Preserved strengths: Direct one-call task-shape fit, no input ambiguity, and an auth-gate response that
  keeps the next step legible instead of collapsing into opaque failure text.
- Gaps / burdens: The result is still a plain text line, so downstream agents must parse fields
  from prose-like output rather than consume a structured object.
- Named external evidence: MCP Tools specification; Anthropic implement-tool-use doc; Anthropic
  tool-use overview.
- Brownfield anchor: Reflected `uv run cli.py list-tools` output on 2026-03-13;
  [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1796);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1819).
- Main leak: text-first parsing burden.

### `GetUsageStats`

- Primary user job: Summarize how this MCP surface has been used recently without opening the local
  telemetry database directly.
- Current contract shape: Reflected metadata exposes an empty input schema and a short descriptive
  sentence. The handler reads `analytics.db`, summarizes the last 30 days into one short natural
  language `TextContent`, and otherwise returns action-oriented empty or missing-database text.
  There is no continuation token, drill-down mode, or structured metric object.
- Judgment band: `mixed`
- Preserved strengths: Zero-argument invocation is legible, the response stays privacy-safe and aggregate,
  and the empty-state paths tell the model whether telemetry is missing or simply absent for the
  last 30 days.
- Gaps / burdens: The metadata does not expose how dependent the tool is on hidden local state, and
  the compact prose summary collapses counts, latencies, and error classes into text that cannot be
  reused reliably as fields.
- Named external evidence: Anthropic implement-tool-use doc; Anthropic tool-use overview.
- Brownfield anchor: [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1949);
  [src/mcp_telegram/analytics.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/analytics.py);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L2208).
- Main leak: metadata ambiguity around hidden telemetry-state dependency.

### `GetUserInfo`

- Primary user job: Resolve one person by natural name and inspect their Telegram profile plus
  shared-chat context.
- Current contract shape: The tool exposes one `user: string` input. It resolves the name through
  the cache-backed fuzzy resolver, returns a resolved banner plus text lines for `id=`, `name=`,
  `username=@...`, and a `Common chats (...)` block, or emits action-oriented not-found,
  ambiguous, or fetch-failure text. Continuation usually means retrying with an exact candidate.
- Judgment band: `mixed`
- Preserved strengths: The job is user-centered rather than low-level, the handler preserves useful shared
  chat context, and ambiguity/error paths tell the model how to retry instead of hiding the cause.
- Gaps / burdens: Success depends on the entity cache already knowing the user, fuzzy resolution can
  force retries, and the profile plus common-chat list is text-first rather than a stable object the
  model can query by field.
- Named external evidence: MCP Tools specification; Anthropic implement-tool-use doc; Anthropic
  tool-use overview.
- Brownfield anchor: [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1850);
  [src/mcp_telegram/resolver.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/resolver.py#L33);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1849).
- Main leak: disambiguation burden.

### `ListDialogs`

- Primary user job: Discover reachable dialogs by natural name before reading, searching, or
  resolving people.
- Current contract shape: The reflected contract exposes `exclude_archived: bool = false` and
  `ignore_pinned: bool = false`. The handler returns newline-delimited text rows like
  `name='...' id=... type=... last_message_at=... unread=...`, with an action-oriented empty state
  when nothing is visible. There is no continuation token, so the contract assumes one full scan.
- Judgment band: `strong`
- Preserved strengths: The tool maps directly to the discovery job, its metadata explains the archived-scope
  switch clearly, and each call also warms the cache that later name-based tools depend on.
- Gaps / burdens: Discovery results are only text rows, so agents must parse names and ids from
  rendered lines, and large inventories still rely on the model to decide what to carry forward.
- Named external evidence: MCP Tools specification; Anthropic implement-tool-use doc; Anthropic
  tool-use overview.
- Brownfield anchor: Reflected `uv run cli.py list-tools` output on 2026-03-13;
  [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L962);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L36).
- Main leak: helper-step burden, because many later jobs still start here to warm discovery state.

### `ListMessages`

- Primary user job: Read one dialog or one forum topic with enough recovery help to keep moving.
- Current contract shape: The tool requires `dialog` and exposes optional `limit`, `cursor`,
  `sender`, `topic`, `unread`, and `from_beginning` parameters. It returns formatted text-first
  transcripts with date headers, session breaks, optional topic labels, and a `next_cursor` token
  when another page exists. Continuation depends on reusing `next_cursor`, remembering that
  `from_beginning=True` reverses pagination direction, and often resolving the dialog or topic first.
- Judgment band: `mixed`
- Preserved strengths: This is the richest current tool. It preserves action-oriented recovery for ambiguous
  dialogs, senders, topics, deleted topics, inaccessible topics, and invalid cursors; supports both
  backward and forward-in-time pagination; and can expose cross-topic forum history with inline
  labels.
- Gaps / burdens: The contract carries the heaviest prose load in the whole surface, mixes multiple
  reading modes behind one text-first response shape, and often requires `ListDialogs` and
  `ListTopics` before the real read can happen cleanly.
- Named external evidence: MCP Tools specification; Anthropic implement-tool-use doc; Anthropic
  tool-use overview.
- Brownfield anchor: [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1140);
  [src/mcp_telegram/formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/formatter.py#L9);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L352).
- Main leak: helper-step burden.

### `ListTopics`

- Primary user job: Discover the exact forum-topic choices for one dialog before filtering message
  reads by topic.
- Current contract shape: The tool exposes one `dialog: string` input. It resolves the dialog,
  then returns one text row per active topic with `topic_id=`, `title="..."`, `top_message_id=`,
  `status=...`, and `last_error=...` when relevant. Continuation expectation is explicit: use the
  chosen exact topic name or numeric topic id in later message-reading calls.
- Judgment band: `strong`
- Preserved strengths: The model-facing purpose is unusually clear, and the result preserves topic-state
  visibility that would be easy to lose in a simplification pass, including `general`, `active`,
  and `previously_inaccessible` states.
- Gaps / burdens: The result is still a text table rather than a structured topic catalog, and the
  tool mostly exists as a prerequisite step for `ListMessages` instead of finishing the user task on
  its own.
- Named external evidence: MCP Tools specification; Anthropic implement-tool-use doc; Anthropic
  tool-use overview.
- Brownfield anchor: [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1042);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L128).
- Main leak: workflow dependency on other tools.

### `SearchMessages`

- Primary user job: Search inside one dialog and inspect each hit in local context.
- Current contract shape: The tool exposes `dialog`, `query`, `limit`, and `offset`. It resolves
  the dialog, fetches search hits, wraps each hit in a `+-3` context window, marks the hit line
  with `[HIT]`, and appends `next_offset` when another page exists. Continuation is explicit but
  separate from `ListMessages`: reuse `next_offset` rather than a cursor token.
- Judgment band: `strong`
- Preserved strengths: The result shape is well aligned with the actual search job, the hit-window formatting
  is deliberate rather than accidental, and ambiguity or no-hit paths stay action-oriented.
- Gaps / burdens: The tool still assumes the dialog is already discoverable, the grouped search
  result is text-first, and its pagination contract diverges from `ListMessages` even though both
  are navigational read tools.
- Named external evidence: MCP Tools specification; Anthropic implement-tool-use doc; Anthropic
  tool-use overview.
- Brownfield anchor: [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L1597);
  [tests/test_tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_tools.py#L1657);
  [tests/test_formatter.py](/home/j2h4u/repos/j2h4u/mcp-telegram/tests/test_formatter.py).
- Main leak: pagination burden.
