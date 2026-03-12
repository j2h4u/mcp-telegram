---
phase: 09-forum-topics-support
plan: 03
status: ready
updated: 2026-03-12
---

# Phase 9 Manual Validation

Use this playbook to validate forum-topic behavior against a real Telegram forum supergroup before shipping.

## Preconditions

- Export `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` for the account you will test with.
- Ensure the account already has a usable Telegram session for `uv run mcp-telegram` / `uv run cli.py`.
- Prepare one forum-enabled supergroup with:
  - 100+ topics if possible
  - the default `General` topic still present
  - at least one active non-General topic with 20+ messages
  - at least two adjacent active topics with recent traffic
  - one disposable topic that you can delete during validation
- Optional but useful: a second Telegram account that does not have access to the target forum.

## Tool Entry Points

Terminal tool calls:

```bash
uv run cli.py list-tools
uv run cli.py call-tool --name ListDialogs --arguments '{}'
```

Inspector:

```bash
npx @modelcontextprotocol/inspector uv run mcp-telegram
```

## 1. Confirm Dialog Discovery

Find the exact forum dialog name first.

```bash
uv run cli.py call-tool --name ListDialogs --arguments '{}'
```

Expected result:

- The forum supergroup is listed.
- Use the exact dialog name from this output in all later commands.

## 2. Validate General Topic Normalization

Run the General-topic fetch explicitly.

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"General","limit":5}'
```

Expected result:

- Output starts with `[topic: General]`.
- Messages are ordinary General-thread messages.
- No topic-not-found error for `General`.
- No evidence of a hard-coded `topic=0` assumption.

Record:

- Whether Telegram/Telethon treats General as topic id `1`.
- Whether the returned General messages have any visible root-message quirks.

## 3. Validate Topic Metadata Pagination Beyond the First Page

Use the raw helper to enumerate all topics and confirm pagination past 100 topics.

```bash
uv run python - <<'PY'
import asyncio

from mcp_telegram.telegram import create_client
from mcp_telegram.tools import _fetch_all_forum_topics

FORUM_NAME = "<FORUM_NAME>"


async def main() -> None:
    client = create_client()
    await client.connect()
    try:
        entity = await client.get_entity(FORUM_NAME)
        topics = await _fetch_all_forum_topics(client, entity=entity)
        print(f"total_topics={len(topics)}")
        print("first_five=", topics[:5])
        print("last_five=", topics[-5:])
    finally:
        await client.disconnect()


asyncio.run(main())
PY
```

Expected result:

- `total_topics` matches the forum’s visible topic count closely enough to explain deleted/private topics.
- Topic count exceeds 100 without duplicates or obvious gaps.
- `General` is present in the normalized output even if Telegram omits it from listing pages.

## 4. Validate Non-General Topic Paging

Pick one active non-General topic with enough messages to require pagination.

First page:

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","limit":20}'
```

Next page:

- Copy the `next_cursor` value from the first result.

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","limit":20,"cursor":"<NEXT_CURSOR>"}'
```

From the beginning:

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","limit":20,"from_beginning":true}'
```

Expected result:

- Every page starts with `[topic: <TOPIC_NAME>]`.
- First page contains only messages from the target topic.
- Cursor page contains only messages from the target topic.
- `from_beginning=true` returns oldest-first topic messages only.
- No duplication between page 1 and page 2.
- No adjacent-topic leakage at any page boundary.

## 5. Validate Sender and Unread Combinations

If the topic has a known sender and unread messages, run:

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","sender":"<SENDER_NAME>","limit":20}'
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<TOPIC_NAME>","unread":true,"limit":20}'
```

Expected result:

- Sender-filtered results stay inside the target topic.
- Unread-filtered results stay inside the target topic.
- Neither mode falls back to unrelated forum history.

## 6. Validate Deleted Topic Behavior

Create a temporary topic, post at least one message, delete the topic in Telegram, then run:

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<DELETED_TOPIC_NAME>","limit":5}'
```

Expected result:

- The tool returns an explicit deleted-topic message.
- It does not silently switch to unfiltered forum history.

## 7. Validate Inaccessible / Private Behavior

Use a second account that cannot access the target forum, or point the current session at a forum/topic that now returns a Telegram RPC access error.

```bash
uv run cli.py call-tool --name ListMessages --arguments '{"dialog":"<FORUM_NAME>","topic":"<RESTRICTED_TOPIC_NAME>","limit":5}'
```

Expected result:

- The tool returns an explicit inaccessible-topic message with the RPC reason.
- It does not return unrelated messages as if the topic filter worked.

## Sign-Off Checklist

- [ ] `General` resolves and reads successfully
- [ ] Raw topic enumeration crosses 100 topics without duplicate/gap issues
- [ ] Non-General topic page 1 is clean
- [ ] Cursor page is clean
- [ ] `from_beginning=true` is clean
- [ ] Sender filter stays in-topic
- [ ] Unread filter stays in-topic
- [ ] Deleted topic returns explicit tombstone behavior
- [ ] Inaccessible topic returns explicit RPC behavior

## Notes to Capture

- Forum name used
- Approximate topic count
- Topic names used for paging checks
- Deleted topic name used
- Any live Telegram behavior that differs from the mocked assumptions
