---
phase: 09-forum-topics-support
verified: 2026-03-12T11:40:00Z
status: gaps_found
score: 3/5 roadmap success criteria verified, 2 gaps found
re_verification: true
---

# Phase 9: Forum Topics Support Verification Report

**Phase Goal:** Enable `ListMessages` to filter by forum topic with comprehensive edge-case handling (General topic normalization, deleted/private topics, pagination).

**Verified:** 2026-03-12T11:40:00Z

**Status:** GAPS FOUND - automated coverage is strong, but live validation found one behavioral gap and one product gap.

## Goal Achievement

### Roadmap Success Criteria

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | `ListMessages` accepts `topic: str \| None` and resolves topic names only inside the selected dialog | ✓ VERIFIED | Implemented in [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L534) and [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L645); live validation found dialog discovery and topic resolution working on one forum-enabled dialog |
| 2 | Topic metadata is cached with short TTL and handles General normalization, deleted topics, inaccessible topics, and pagination | ⚠ GAP | General works live; one deleted topic produced `TOPIC_ID_INVALID`; three existing topics (`Topic A`, `Topic B`, `Topic C`) resolved by name but then failed with the same `TOPIC_ID_INVALID`, so the runtime cannot distinguish inaccessible/private/closed/deleted cases clearly enough |
| 3 | Messages stay inside topic boundaries with topic header output and correct pagination | ⚠ GAP | Header, first page, next cursor, `from_beginning`, sender-filtering, and leakage checks passed live; `topic + unread` is suspect because the returned cursor matched the unfiltered dialog cursor instead of the topic-scoped cursor |
| 4 | Dialog-first resolution prevents ambiguity when different dialogs have identically named topics | ✓ VERIFIED | The implementation resolves the dialog first, then loads the dialog-local topic catalog in [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L571) and [src/mcp_telegram/tools.py](/home/j2h4u/repos/j2h4u/mcp-telegram/src/mcp_telegram/tools.py#L651); no cross-dialog ambiguity was observed in live testing |
| 5 | Real forum group testing confirms behavior with 100+ topics, some deleted, some private, and pagination with no off-by-one errors | ✗ NOT VERIFIED | Live validation was executed, but only against a 5-topic forum. Some real-world scenarios were exercised successfully, but the roadmap’s 100+ topic requirement was not met, and live gaps were found before phase closure |

**Score:** 3/5 roadmap success criteria verified, 2/5 have gaps or remain unverified

## Live Validation Outcome

### Environment

| Field | Value |
|---|---|
| Forum used | `Forum Alpha` (`id=<redacted>`) |
| Topic count observed | 5 |
| Topics identified | `General` (`id=1`), `Topic A` (`id=<redacted>`), `Topic B` (`id=<redacted>`), `Topic C` (`id=<redacted>`), `Topic D` (`id=<redacted>`) |
| Deleted-topic scenario | Testable; `Deleted Topic` returned `TOPIC_ID_INVALID` |
| Inaccessible/private scenario | Testable; multiple resolved topics returned `TOPIC_ID_INVALID` |

### Live Checks

| Check | Result | Notes |
|---|---|---|
| Dialog discovery | PASS | Forum group found via `ListDialogs` |
| General topic | PASS | Returned `[topic: General]` with correct content |
| Topic page 1 | PASS | Returned topic-scoped messages with cursor |
| Topic next cursor | PASS | Returned older messages correctly |
| Topic `from_beginning` | PASS | Returned oldest-first messages |
| Topic + sender | PASS | Returned only the selected sender’s messages |
| Topic + unread | SUSPECT | Cursor matched unfiltered dialog cursor; topic filter may be bypassed in unread mode |
| Deleted topic behavior | PASS with caveat | Returned explicit `TOPIC_ID_INVALID`, but not a differentiated deleted-topic response |
| Inaccessible/private topic behavior | FAIL | Existing resolved topics produced the same `TOPIC_ID_INVALID`, so cause is not distinguishable |
| Adjacent-topic leakage | PASS | No leakage observed between `General` and `Topic A` |

### Key Evidence

1. `General` passed live: header and content were correct.
2. `Topic A` paging passed live with no cross-topic leakage.
3. `topic="Topic B"` resolved via fuzzy/topic catalog discovery but `ListMessages` returned `Topic "Topic B" is inaccessible: TOPIC_ID_INVALID`.
4. `topic="Topic C"` and `topic="Topic D"` showed the same failure pattern.
5. `ListMessages(unread=true)` and `ListMessages(topic="General", unread=true)` produced the same cursor, which strongly suggests the unread path is not topic-scoped.

## Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| TOPIC-01 | ✓ PARTIALLY VERIFIED LIVE | Topic parameter, dialog-scoped resolution, first-page paging, next cursor, `from_beginning`, and sender flow all worked on a real forum |
| TOPIC-02 | ⚠ GAP | Cache/pagination/general normalization exist and are test-covered, but live topic-state handling is not differentiated enough and inaccessible/private/deleted cases collapse into `TOPIC_ID_INVALID` |
| TOPIC-03 | ✓ VERIFIED | Topic header appears live for successful topic fetches |

## Gap Summary

### Gap 1: Existing topics can resolve by name and still fail with undifferentiated `TOPIC_ID_INVALID`

**Severity:** Major

Observed on:
- `Topic B`
- `Topic C`
- `Topic D`

Impact:
- The runtime can discover or resolve a topic name but cannot tell the user whether the topic is deleted, private, closed, or otherwise inaccessible.
- This makes live behavior inconsistent with the intended explicit edge-case handling.

Recommended closure targets:
- Preserve and report more diagnostic context when a resolved topic fails.
- Add direct topic lookup by ID or a topic-listing tool so live debugging is not blocked by name-only flows.
- Differentiate deleted/inaccessible/invalid cases in user-visible output where Telegram semantics allow it.

### Gap 2: `topic + unread` may ignore the topic filter

**Severity:** Minor

Observed symptom:
- `ListMessages(unread=true)` and `ListMessages(topic="General", unread=true)` returned the same cursor while `General` without `unread=true` returned a different topic-scoped cursor.

Impact:
- Topic-filtered unread mode may page from dialog-wide unread state rather than topic-scoped state.

Recommended closure targets:
- Add live-representative regression coverage for `topic + unread`.
- Fix cursor/min_id handling so unread mode remains topic-scoped.

## Recommended Next Step

```text
$gsd-plan-phase 9 --gaps
```

## Summary

Phase 9 is not ready to close. The core topic feature works in important live paths, but live validation found one concrete behavior bug (`topic + unread`) and one major product/runtime gap (resolved topics collapsing into indistinguishable `TOPIC_ID_INVALID` failures). Gap-closure planning is the correct next move.

---

_Verified: 2026-03-12T11:40:00Z_  
_Verifier: Codex, updated with external live-validation results_
