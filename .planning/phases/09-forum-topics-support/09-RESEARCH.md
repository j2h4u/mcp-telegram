# Phase 9: Forum Topics Support - Research

**Researched:** 2026-03-12
**Domain:** Telegram forum topics, Telethon thread retrieval, dialog-scoped topic resolution
**Confidence:** HIGH

## Summary

Phase 9 can be implemented on top of existing `ListMessages` flow without introducing a new global resolver. The dialog should still resolve first from `EntityCache`, then topic lookup should happen inside that dialog only. Telegram exposes forum topics through `channels.getForumTopics` / `channels.getForumTopicsByID`, while Telethon already exposes raw request types for both and supports thread retrieval through `iter_messages(..., reply_to=...)`.

The highest-leverage design is:
1. Add a dialog-scoped topic lookup layer with short TTL in the existing SQLite cache database.
2. Reuse the existing `resolve()` fuzzy matcher against `{topic_id: topic_title}` for the selected dialog.
3. Fetch topic messages server-side when possible with `iter_messages(reply_to=topic_root_id)` and keep a tested fallback path for client-side filtering if forum-thread semantics differ in real groups.
4. Prefix `ListMessages` output with the active topic name without changing the core formatter contract more than necessary.

## Important Correction

The roadmap wording says "topic 0 (General)", but Telegram's official forum API docs state that every forum has a non-deletable **General** topic with **`id=1`**. Plans for this phase should not hard-code `0`; they should validate real runtime behavior and normalize General-topic handling explicitly.

Source: `https://core.telegram.org/api/forum`

## User Constraints

No `CONTEXT.md` exists for Phase 9. Planning should optimize for the documented requirements and current codebase patterns.

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| TOPIC-01 | `ListMessages` gains `topic: str | None`; fuzzy-resolves topic name within the dialog and filters to that topic only | Reuse existing `resolve()` against per-dialog topic map; no global topic resolver needed |
| TOPIC-02 | Short-TTL topic metadata cache via `GetForumTopicsRequest`; handle General topic, deleted topics, pagination | Telethon exposes `GetForumTopicsRequest`; raw API supports pagination via `offset_date`, `offset_id`, `offset_topic`; deleted topics appear as `ForumTopicDeleted` |
| TOPIC-03 | Topic name shown in `ListMessages` output header when topic filter is active | Cheapest path is tool-level header prefix ahead of formatted messages |

## Primary Sources

- Telegram Forum API: `https://core.telegram.org/api/forum`
- Telethon `TelegramClient.iter_messages` signature and local package source (`Telethon 1.38.1`)
- Telethon raw types in local environment:
  - `channels.GetForumTopicsRequest(channel, offset_date, offset_id, offset_topic, limit, q=None)`
  - `channels.GetForumTopicsByIDRequest(channel, topics=[...])`
  - `messages.ForumTopics(count, topics, messages, chats, users, pts, order_by_create_date=None)`
  - `ForumTopic(id, date, title, icon_color, top_message, ...)`
  - `ForumTopicDeleted(id)`
  - `MessageReplyHeader(..., forum_topic=None, reply_to_msg_id=None, reply_to_top_id=None, ...)`

## Current Codebase Fit

### Relevant Existing Paths

- `src/mcp_telegram/tools.py`
  - `ListMessages` already handles dialog resolution, sender filtering, unread filtering, reverse pagination, reply annotations, reactions, and cursor generation in one function.
- `src/mcp_telegram/cache.py`
  - Holds SQLite-backed entity metadata and reaction metadata; good place to add topic metadata cache table(s).
- `src/mcp_telegram/resolver.py`
  - Already provides numeric, exact, fuzzy, transliteration-aware resolution; can be reused for topic lookup once choices are scoped to one dialog.
- `src/mcp_telegram/formatter.py`
  - Formats message bodies, date separators, reply annotations, and reactions; does not currently have a concept of conversation header metadata.
- `tests/test_tools.py`
  - Existing handler-focused async tests fit the likely topic test surface.
- `tests/test_cache.py`
  - Cache TTL behavior is already tested and is the right place for topic cache tests.

### Architectural Implication

Do not extend the global resolver or entity cache name map to include topics from all dialogs. Topic names are only meaningful within a single forum dialog, and resolving them globally would create unnecessary ambiguity and stale data problems.

## API Facts That Matter

### Topic Enumeration

Telegram forum topics are listed with `channels.getForumTopics`. The request paginates using:

- `offset_date`
- `offset_id`
- `offset_topic`
- `limit`
- optional `q` for search

Result shape:

- `count`
- `topics`
- `messages`
- `chats`
- `users`
- `pts`

The planner should assume pagination is required because Phase 9 explicitly calls out forums with 100+ topics.

### Topic Identity

Official Telegram docs say:

- General topic has `id=1`
- Non-General topics use the ID of the `messageActionTopicCreate` service message that created the topic
- Topic/thread behavior should be treated similarly to Telegram message threads

This aligns with Telethon exposing both:

- `ForumTopic.id`
- `ForumTopic.top_message`

For non-General topics, the safest planning assumption is that topic filtering logic should be keyed by the thread root/top message, not only by title.

### Deleted Topics

Telethon exposes `ForumTopicDeleted(id)`. That means topic enumeration can return tombstones instead of full metadata, so the cache layer must either:

- skip deleted entries during name resolution, or
- preserve them as deleted markers to produce a clearer error path

For this phase, preserving deleted markers is preferable because it lets `ListMessages(topic="...")` distinguish "never existed" from "was deleted or inaccessible recently". The best refresh path is an explicit by-ID lookup for the candidate topic, not inferring deletion from empty message pages.

### Message-to-Topic Linkage

Telethon's `MessageReplyHeader` exposes:

- `forum_topic`
- `reply_to_msg_id`
- `reply_to_top_id`

The official forum docs say forum topics are message threads. The most likely thread anchor is the topic root/top message ID. Planning should validate the exact runtime rule with real topic messages, but the candidate linkage is:

- non-General topic root message: `msg.id == topic.id` or `msg.id == topic.top_message`
- replies inside topic: `msg.reply_to.reply_to_top_id == topic.id` or `topic.top_message`
- General topic: messages without forum-thread linkage, or replies whose top ID is absent and remain in the default thread

This specific mapping needs verification in tests against realistic mocks and in one manual live-forum check.

### Server-Side Retrieval Option

Local Telethon source shows `iter_messages(..., reply_to=<message_id>)` routes to `messages.GetRepliesRequest`. This is the best candidate for efficient topic-scoped retrieval because it avoids scanning the entire supergroup history.

Planning should treat this as the preferred path for non-General topics:

```python
client.iter_messages(entity, reply_to=topic_root_id, limit=..., reverse=...)
```

But because forum topics are a specific subtype of thread behavior, the implementation plan should require a fallback strategy if real-world behavior differs:

- Fallback: iterate normally, filter client-side using reply header topic linkage

## Recommended Cache Design

Add a dedicated topic metadata cache in `cache.py`, backed by the existing SQLite database file. Avoid mixing topics into the `entities` table.

Recommended schema fields:

- `dialog_id INTEGER NOT NULL`
- `topic_id INTEGER NOT NULL`
- `title TEXT NOT NULL`
- `top_message_id INTEGER`
- `is_general INTEGER NOT NULL`
- `is_deleted INTEGER NOT NULL`
- `updated_at INTEGER NOT NULL`

Recommended primary key:

- `(dialog_id, topic_id)`

Recommended index:

- `(dialog_id, updated_at)`

Recommended TTL:

- 300 to 600 seconds

Why this shape:

- dialog-scoped uniqueness is correct
- deleted topics can be marked without losing history
- `top_message_id` gives retrieval/filtering anchor
- General topic can be represented explicitly even if server responses omit or special-case it

## Resolver Strategy

Reuse `resolve()` exactly as-is.

Implementation flow:
1. Resolve dialog via existing entity cache.
2. Load cached topics for that dialog; refresh from Telegram if stale/missing.
3. Build `choices: dict[int, str]` from active topics in that dialog only.
4. Call `resolve(args.topic, choices, cache=None)` or with lightweight metadata access if useful.
5. Use resolved `topic_id` plus cached `top_message_id` to fetch/filter messages.

This satisfies the "resolver handles `(dialog_name, topic_name)` tuple correctly" success criterion without modifying `resolver.py` to understand tuples globally.

## Output/Header Strategy

Do not move topic-header responsibility into `formatter.py` unless implementation proves it is reused elsewhere. Current formatter contract is "format message list body". A lower-risk plan is:

- keep formatter output unchanged
- prepend a topic header in `list_messages`, similar to how `resolve_prefix` is prepended today

Example shape:

```text
[resolved: "dev forum" -> Dev Forum]
[topic: Releases]
--- 2026-03-12 ---
10:00 Ivan: shipped
```

That keeps topic state close to topic resolution and avoids broad formatter churn.

## Likely Plan Split

The phase should be planned as at least 3 executable plans:

### Plan A: Topic metadata cache + retrieval

Scope:

- add cache table/class in `cache.py`
- add helper(s) in `tools.py` or nearby module for paginated topic fetch
- normalize General topic representation
- record deleted-topic markers

Why separate:

- isolated persistence/API integration work
- gives a stable base for topic resolution and message filtering

### Plan B: `ListMessages` topic argument + filtering + header

Scope:

- add `topic: str | None` to `ListMessages`
- resolve topic within resolved dialog
- route non-General topics through `reply_to=` retrieval if viable
- add fallback client-side filtering when needed
- prepend topic header to output
- preserve compatibility with `cursor`, `sender`, `unread`, and `from_beginning`

Why separate:

- the handler already has several interacting flags; this is the highest behavioral risk

### Plan C: Test and manual validation hardening

Scope:

- unit/integration-style mocks for topic enumeration, pagination, ambiguity, deleted topics, General topic behavior
- cache TTL tests
- explicit manual verification checklist for a real forum with many topics

Why separate:

- success criteria require real forum validation and pagination edge checks

## Interaction Risks To Explicitly Plan For

### `topic` + `unread`

Unread currently sets `min_id` based on `GetPeerDialogsRequest`. Topic retrieval may also need `reply_to=` or client-side filtering. The plan must define whether `unread` remains supported with `topic`, and if supported, who owns the cursor/min_id behavior.

Preferred planning stance:

- preserve support
- treat `reply_to=` and `min_id` as compatible where possible
- add targeted tests for `topic + unread`

### `topic` + `from_beginning`

Reverse iteration is already implemented. Topic filtering must preserve:

- oldest-first retrieval
- correct `next_cursor` generation
- no skipped/duplicated messages across topic pages

This needs explicit tests because thread-based retrieval may have slightly different ordering semantics than whole-chat retrieval.

### `topic` + `sender`

`iter_messages` can switch to `messages.SearchRequest` when `from_user` is provided, but `reply_to=` uses reply-thread retrieval. The planner should assume this combination may need client-side sender filtering after topic retrieval rather than combining all constraints server-side.

### Empty or inaccessible topic

Behavior must be explicit for:

- topic name not found
- ambiguous topic name inside one dialog
- deleted topic
- forum disabled or dialog not a forum supergroup
- permissions / private topic access issues

Telethon error classes worth planning around:

- `ChannelForumMissingError`
- `TopicDeletedError`
- broader `RPCError` subclasses for access failures

## Testing Strategy

### Automated

Add or expand tests in:

- `tests/test_tools.py`
- `tests/test_cache.py`

Required automated coverage:

- topic name resolves within selected dialog only
- ambiguous topic names in same dialog return candidates
- same topic title in different dialogs does not cause cross-dialog ambiguity
- General topic normalization behaves as designed
- deleted-topic cache entries do not resolve as active topics
- topic pagination fetches beyond first 50/100 topics correctly
- `ListMessages(topic=...)` returns only topic messages
- header includes topic name
- `topic + cursor`
- `topic + from_beginning`
- `topic + sender`
- `topic + unread` (or explicit incompatibility message if design chooses that)

### Manual

One manual verification is mandatory because mocks will not prove real Telegram forum semantics:

1. Use a real forum supergroup with 100+ topics.
2. Include at least one hidden/General case, one deleted topic, and one inaccessible/private edge if available.
3. Verify topic list pagination.
4. Verify topic-filtered `ListMessages` first page, next page, and reverse pagination.
5. Verify the header shows the resolved topic name and no messages leak from adjacent topics.

## Validation Architecture

### Framework

- Python 3.11+
- `pytest`
- async handler tests using existing async fixtures/mocks

### Quick Feedback Commands

- `uv run pytest tests/test_cache.py -k "topic" -v`
- `uv run pytest tests/test_tools.py -k "topic" -v`

### Full Validation Commands

- `uv run pytest`

### Required Wave 0 Work

- Add topic cache unit-test scaffolding in `tests/test_cache.py`
- Add topic-oriented `ListMessages` scaffolding in `tests/test_tools.py`
- Add a small helper fixture/factory for forum-topic reply headers if existing message fixtures are too shallow

### Manual-Only Validation

- Live Telegram forum group validation for actual thread semantics and deleted/private-topic behavior

## Recommended Plan Quality Gates

- Every plan must map to `TOPIC-01`, `TOPIC-02`, or `TOPIC-03`
- At least one plan must own cache/pagination mechanics
- At least one plan must own handler behavior and header output
- At least one plan must own real-forum validation and edge-case tests
- No plan should assume General topic is `0`
- No plan should introduce global topic resolution across all dialogs

## Open Questions For Planner To Resolve

1. Should non-General topic retrieval rely primarily on `reply_to=topic_root_id`, or should the plan start with a proof test and keep client-side filtering as the main path?
2. Should inaccessible/deleted topic requests fall back to unfiltered messages as the roadmap says, or return a clearer warning while still surfacing the fallback?
3. Does the implementation need a separate helper module for topic fetching, or is one small helper inside `tools.py` consistent with the current codebase size?

The plan checker should reject plans that ignore any of these questions.
