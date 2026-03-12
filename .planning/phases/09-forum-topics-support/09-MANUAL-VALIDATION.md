---
phase: 09-forum-topics-support
plan: 06
status: ready
updated: 2026-03-12
---

# Phase 9 Manual Validation

Use this checklist to close Phase 9 against the rebuilt `mcp-telegram` runtime, not just the local checkout.

## Preconditions

- Export `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
- Ensure the Telegram session you will use is already valid for both `mcp-telegram` and local `cli.py` debugging.
- Prepare one forum-enabled supergroup with:
  - 100+ topics if possible
  - the default `General` topic
  - at least one active non-General topic with enough history for paging
  - at least one topic that currently fails, was deleted, or is inaccessible/private

## 1. Rebuild And Restart The Runtime

Run this from the host:

```bash
docker compose -f /opt/docker/mcp-telegram/docker-compose.yml up -d --build mcp-telegram
```

Expected result:

- The `mcp-telegram` container is recreated and started successfully.

## 2. Prove The Running Container Is Current

Verify the runtime inside the container before trusting any live results:

```bash
docker exec mcp-telegram mcp-telegram --help
docker exec mcp-telegram /opt/venv/bin/python -c "import inspect,mcp_telegram.tools as t; src=inspect.getsource(t.list_messages); print('use_topic_scoped_fetch' in src); print('cursor_source_messages' in src); print('_fetch_topic_messages' in src)"
docker exec mcp-telegram /opt/venv/bin/python -c "import inspect,mcp_telegram.tools as t; src=inspect.getsource(t._fetch_topic_messages); print('_message_matches_topic' in src); print('raw_messages' in src); print('topic_messages' in src)"
```

Expected result:

- `mcp-telegram --help` works in the running container.
- The first Python check prints `True` three times.
- The second Python check prints `True` three times.

This proves the deployed runtime includes the `09-05` topic-unread scoping path before you continue to live validation.

## 3. Confirm The Local Debug CLI Surface

Run these from the repo checkout:

```bash
uv run python cli.py debug-topic-catalog --help
uv run python cli.py debug-topic-by-id --help
```

Expected result:

- Both commands render help successfully.

## 4. Inspect Topic Catalog Pagination

Use a small page size so the debug output must cross page boundaries:

```bash
uv run python cli.py debug-topic-catalog --dialog "<FORUM_NAME>" --page-size 10
```

Capture:

- `dialog_id=...`
- at least `page=1 ...` and `page=2 ...`
- topic rows containing:
  - `topic_id=...`
  - `title="..."`
  - `top_message_id=...`
  - `is_general=...`
  - `is_deleted=...`
- the final normalized summary:
  - `normalized_catalog_count=...`
  - `active_count=...`
  - `deleted_count=...`

Expected result:

- Pagination clearly crosses the first page.
- Topic ids and anchors look stable.
- Deleted topics, if any, are visible as deleted.
- General is visible in the normalized catalog.

## 5. Inspect A Failing Topic By ID

Pick one topic that previously failed by name or one deleted/inaccessible candidate from the catalog output.

```bash
uv run python cli.py debug-topic-by-id --dialog "<FORUM_NAME>" --topic-id <TOPIC_ID>
```

Capture:

- `cached=...`
- `refreshed=...`

Expected result:

- You can distinguish one of these cases directly from the output:
  - stale anchor: `top_message_id` changes after refresh
  - deleted topic: refreshed metadata is deleted/tombstoned
  - still inaccessible/unchanged: metadata remains active but the fetch issue is not an anchor mismatch

## 6. Validate Topic Thread Fetches

### First page

```bash
uv run python cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","limit":20}'
```

Expected result:

- Output starts with `[topic: <TOPIC_NAME>]`.
- Messages belong only to the requested topic.
- If a `next_cursor` is returned, save it.

### Next cursor page

```bash
uv run python cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","limit":20,"cursor":"<NEXT_CURSOR>"}'
```

Expected result:

- Only older messages from the same topic appear.
- No adjacent-topic leakage.

### From the beginning

```bash
uv run python cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","limit":20,"from_beginning":true}'
```

Expected result:

- Oldest-first topic-scoped output.
- No unrelated messages.

### Unread mode

```bash
uv run python cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","unread":true,"limit":20}'
uv run python cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"General","unread":true,"limit":20}'
```

Expected result:

- The returned unread page stays inside the requested topic.
- The General-topic unread result does not mirror the dialog-wide unread result.
- Any `next_cursor` belongs to the last emitted topic message, not an unrelated unread item.

## 7. Validate Deleted Or Inaccessible Behavior

Run this against one deleted, private, or otherwise failing topic:

```bash
uv run python cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<FAILING_TOPIC_NAME>","limit":5}'
```

Expected result:

- Deleted topics return the tombstone text.
- Inaccessible topics return explicit RPC-driven text.
- The tool never falls back to unrelated dialog history.

If name-based probing is ambiguous, repeat with the `debug-topic-catalog` and `debug-topic-by-id` outputs to select the exact `topic_id` and refreshed anchor first.

## Evidence To Capture For Roadmap Criterion 5

- Forum name used and approximate topic count.
- Proof that topic catalog pagination crossed page 1.
- At least one topic id/title/top-message anchor from `debug-topic-catalog`.
- One `debug-topic-by-id` before/after sample.
- One successful non-General first page.
- One successful cursor page.
- One successful `from_beginning=true` sample.
- One successful `unread=true` sample.
- One deleted or inaccessible topic sample.
- Explicit note that no adjacent-topic leakage was observed.

## Sign-Off Checklist

- [ ] Container rebuilt and restarted with `docker compose`
- [ ] Running container proved current via in-container Python checks
- [ ] `debug-topic-catalog` help works
- [ ] `debug-topic-by-id` help works
- [ ] Topic catalog pagination crosses the first page
- [ ] By-id refresh distinguishes the failing topic state
- [ ] First page topic fetch is correct
- [ ] Cursor page is correct
- [ ] `from_beginning=true` is correct
- [ ] `unread=true` remains topic-scoped
- [ ] Deleted/inaccessible topic behavior is explicit
- [ ] No adjacent-topic leakage observed
