# Phase 9 Live Validation Checklist For Claude Desktop

## Role

You are validating the live behavior of forum topic support in `mcp-telegram`.

Your job is not to change code. Your job is to use the existing MCP tools against real Telegram forum groups that already contain topics, then report back with concrete results.

## Goal

Confirm whether Phase 9 is actually correct on real Telegram data, not just in automated tests.

The implementation is expected to support:

- `ListMessages(topic=...)`
- dialog-scoped topic resolution
- General topic handling
- pagination across topic messages
- no message leakage from adjacent topics
- explicit behavior for deleted or inaccessible topics

## Important Rules

1. Do not edit code.
2. Do not guess. If something cannot be tested, say `NOT TESTABLE`.
3. Prefer real tool calls over reasoning from code.
4. If a check fails, capture the exact input and the exact output.
5. If a scenario is ambiguous, run one more confirming call before reporting.

## Tools To Use

Use these MCP tools if available:

- `ListDialogs`
- `ListMessages`

If you also have a way to inspect raw Telegram topic lists in your environment, use it. If not, continue with the checks below and mark raw-enumeration items as `NOT TESTABLE`.

## Required Output Format

When you finish, reply in this exact structure:

```md
# Phase 9 Live Validation Report

## Verdict
- APPROVED
- or ISSUES FOUND
- or BLOCKED

## Environment
- Forum used:
- Approximate topic count:
- Whether deleted topic scenario was testable:
- Whether inaccessible/private topic scenario was testable:

## Checks
| Check | Status | Notes |
|------|--------|-------|
| Dialog discovery | PASS/FAIL/NOT TESTABLE | ... |
| General topic | PASS/FAIL/NOT TESTABLE | ... |
| Topic page 1 | PASS/FAIL/NOT TESTABLE | ... |
| Topic next cursor | PASS/FAIL/NOT TESTABLE | ... |
| Topic from_beginning | PASS/FAIL/NOT TESTABLE | ... |
| Topic + sender | PASS/FAIL/NOT TESTABLE | ... |
| Topic + unread | PASS/FAIL/NOT TESTABLE | ... |
| Deleted topic behavior | PASS/FAIL/NOT TESTABLE | ... |
| Inaccessible/private topic behavior | PASS/FAIL/NOT TESTABLE | ... |
| Adjacent-topic leakage | PASS/FAIL/NOT TESTABLE | ... |

## Evidence
- Call 1:
  - Input:
  - Output summary:
- Call 2:
  - Input:
  - Output summary:

## Issues
- If none, write `None`.
- If any, include:
  - exact tool call
  - actual result
  - expected result
  - severity

## Recommendation
- APPROVE PHASE 9
- or OPEN GAP-CLOSURE WORK
```

## Validation Steps

### 1. Confirm Dialog Discovery

Run `ListDialogs`.

Pass criteria:

- at least one forum-enabled supergroup with topics is visible
- you can identify the exact dialog name you will use for all later checks

If no forum dialog is available, stop and report `BLOCKED`.

### 2. Validate General Topic

Pick one forum dialog and call `ListMessages` with:

- `dialog=<forum name>`
- `topic="General"`
- small limit such as `5`

Pass criteria:

- result starts with a topic header for General
- result clearly contains General-topic messages
- result does not say topic not found
- behavior does not look like fallback to unrelated dialog history

Fail if:

- `General` is not resolvable
- messages are clearly not from the General topic
- the tool silently falls back to unfiltered history

### 3. Validate Non-General Topic Page 1

Pick one active non-General topic with enough recent traffic.

Call `ListMessages` with:

- `dialog=<forum name>`
- `topic=<topic name>`
- `limit=20`

Pass criteria:

- output starts with `[topic: <topic name>]` or equivalent topic header
- messages look like they belong to the selected topic only
- no obvious adjacent-topic leakage
- if a `next_cursor` is returned, save it for the next step

### 4. Validate Next Cursor

If step 3 returned `next_cursor`, call `ListMessages` again with that cursor.

Pass criteria:

- second page still stays inside the same topic
- no obvious duplication from page 1
- no obvious skipped boundary behavior
- no adjacent-topic leakage

If no cursor is available because the topic is too small, mark this `NOT TESTABLE` and say so explicitly.

### 5. Validate `from_beginning=true`

Call `ListMessages` with:

- same dialog
- same topic
- `from_beginning=true`
- reasonable limit such as `20`

Pass criteria:

- results remain inside the same topic
- ordering appears oldest-first
- no adjacent-topic leakage

### 6. Validate `topic + sender`

Only do this if you can identify a sender who definitely posted in that topic.

Call `ListMessages` with:

- same dialog
- same topic
- `sender=<sender name>`

Pass criteria:

- returned messages still belong to the selected topic
- sender filter seems respected
- tool does not fall back to unrelated history

If no reliable sender is available, mark `NOT TESTABLE`.

### 7. Validate `topic + unread`

Only do this if there are unread messages in the selected topic.

Call `ListMessages` with:

- same dialog
- same topic
- `unread=true`

Pass criteria:

- unread results stay inside the selected topic
- tool does not fall back to unrelated history

If there are no unread messages to test, mark `NOT TESTABLE`.

### 8. Validate Deleted Topic Behavior

If you can safely create and then delete a disposable topic in a test forum, do that. Then call `ListMessages` for the deleted topic name.

Pass criteria:

- tool returns an explicit deleted-topic style response
- tool does not silently return unrelated messages

If you cannot safely create/delete topics, mark `NOT TESTABLE`.

### 9. Validate Inaccessible Or Private Topic Behavior

If you have access to a second account or a forum/topic that is currently inaccessible, test that scenario.

Pass criteria:

- tool returns an explicit inaccessible/private error
- tool does not pretend the topic filter worked

If you cannot reproduce this safely, mark `NOT TESTABLE`.

### 10. Check For Adjacent-Topic Leakage

This is critical.

For every non-General topic call above, actively inspect whether any returned message obviously belongs to another topic nearby.

Use this rule:

- if even one message clearly appears to belong to another topic, mark `FAIL`

## Minimum Evidence To Return

At minimum, include:

1. one successful `General` check
2. one successful non-General page-1 check
3. one cursor check or explicit `NOT TESTABLE`
4. one `from_beginning` check
5. explicit statement on deleted-topic testability
6. explicit statement on inaccessible/private-topic testability

## Approval Logic

Recommend `APPROVE PHASE 9` only if:

- all core live checks that were testable passed
- no adjacent-topic leakage was observed
- no misleading fallback behavior was observed

Recommend `OPEN GAP-CLOSURE WORK` if:

- any tested core behavior failed
- results suggest silent fallback
- results suggest messages leak across topic boundaries
- General topic behavior is inconsistent or broken
