---
phase: 9
slug: forum-topics-support
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-03-12
---

# Phase 9 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 9.x with pytest-asyncio 1.3.0+ |
| **Config file** | `pyproject.toml` (`asyncio_mode = "auto"`) |
| **Quick run command** | `uv run pytest tests/test_tools.py -k "topic" -v` |
| **Full suite command** | `uv run pytest` |
| **Estimated runtime** | ~15 seconds |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest tests/test_tools.py -k "topic" -v`
- **After every plan wave:** Run `uv run pytest`
- **Before `$gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 20 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 9-01-01 | 01 | 1 | TOPIC-02 | unit | `uv run pytest tests/test_cache.py::test_topic_metadata_cache_round_trip -v` | ❌ W0 | ⬜ pending |
| 9-01-02 | 01 | 1 | TOPIC-02 | unit | `uv run pytest tests/test_cache.py::test_topic_metadata_cache_ttl -v` | ❌ W0 | ⬜ pending |
| 9-01-03 | 01 | 1 | TOPIC-02 | unit | `uv run pytest tests/test_cache.py::test_topic_metadata_cache_deleted_marker -v` | ❌ W0 | ⬜ pending |
| 9-01-04 | 01 | 1 | TOPIC-02 | integration | `uv run pytest tests/test_tools.py::test_fetch_forum_topics_paginates -v` | ❌ W0 | ⬜ pending |
| 9-01-05 | 01 | 1 | TOPIC-02 | integration | `uv run pytest tests/test_tools.py::test_refresh_topic_by_id_detects_deleted -v` | ❌ W0 | ⬜ pending |
| 9-02-01 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_resolves_within_dialog -v` | ❌ W0 | ⬜ pending |
| 9-02-02 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_not_found -v` | ❌ W0 | ⬜ pending |
| 9-02-03 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_ambiguous_within_dialog -v` | ❌ W0 | ⬜ pending |
| 9-02-04 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_cursor_round_trip -v` | ❌ W0 | ⬜ pending |
| 9-02-05 | 02 | 2 | TOPIC-03 | unit | `uv run pytest tests/test_tools.py::test_list_messages_topic_header -v` | ❌ W0 | ⬜ pending |
| 9-02-06 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_from_beginning -v` | ❌ W0 | ⬜ pending |
| 9-02-07 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_sender_behavior -v` | ❌ W0 | ⬜ pending |
| 9-02-08 | 02 | 2 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_unread_behavior -v` | ❌ W0 | ⬜ pending |
| 9-03-01 | 03 | 3 | TOPIC-02 | integration | `uv run pytest tests/test_tools.py::test_list_messages_deleted_topic_behavior -v` | ❌ W0 | ⬜ pending |
| 9-03-02 | 03 | 3 | TOPIC-02 | integration | `uv run pytest tests/test_tools.py::test_list_messages_general_topic_normalization -v` | ❌ W0 | ⬜ pending |
| 9-03-03 | 03 | 3 | TOPIC-01 | integration | `uv run pytest tests/test_tools.py::test_list_messages_topic_boundary_no_leakage -v` | ❌ W0 | ⬜ pending |
| 9-03-04 | 03 | 3 | TOPIC-02 | integration | `uv run pytest tests/test_tools.py::test_list_messages_private_or_inaccessible_topic_behavior -v` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_cache.py` — add topic metadata cache TTL and tombstone stubs
- [ ] `tests/test_tools.py` — add topic resolution, header, pagination, and deleted-topic stubs
- [ ] `tests/conftest.py` — add helper factory for forum-topic reply headers if current message fixture is too shallow

*Existing pytest and async fixture infrastructure already covers the phase; only topic-specific test scaffolding is missing.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Real forum topic pagination with 100+ topics | TOPIC-02 | Mocked TL objects do not prove Telegram's live `offset_topic` paging behavior | In a real forum supergroup, enumerate topics past the first page and confirm no duplicates or gaps across page boundaries |
| General topic normalization | TOPIC-02 | Official docs say General is `id=1`, while roadmap text says `0`; live data must settle the implementation contract | Run topic resolution against a forum's General thread and record the actual ID/root-message behavior used by Telegram/Telethon |
| Non-General topic retrieval via `reply_to=` | TOPIC-01 | Telethon exposes thread retrieval, but live forum semantics must be confirmed against real data | Resolve a named non-General topic, fetch first page and next page with `ListMessages(topic=...)`, verify only thread messages appear |
| Deleted/private topic fallback behavior | TOPIC-02 | Access-control and tombstone behavior depend on live server state | Attempt to read a deleted or inaccessible topic and verify the tool emits the intended warning/fallback path |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 20s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
