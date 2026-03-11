---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: Core API
status: complete
stopped_at: v1.0 shipped 2026-03-11
last_updated: "2026-03-11T14:33:53.724Z"
last_activity: 2026-03-11
progress:
  total_phases: 5
  completed_phases: 5
  total_plans: 14
  completed_plans: 14
  percent: 100
---

# Project State

## Project Reference

See: .planning/PROJECT.md (updated 2026-03-11)

**Core value:** LLM can work with Telegram using natural names — zero cold-start friction, no ID lookup boilerplate before every real task
**Current focus:** Planning next milestone — run `/gsd:new-milestone`

## Current Position

Phase: 05 of 3 (cache error hardening)
Plan: Not started
Status: In progress
Last activity: 2026-03-11

Progress: [████░░░░░░] 42%

## Performance Metrics

**Velocity:**
- Total plans completed: 0
- Average duration: —
- Total execution time: —

**By Phase:**

| Phase | Plans | Total | Avg/Plan |
|-------|-------|-------|----------|
| - | - | - | - |

*Updated after each plan completion*
| Phase 01-support-modules P01 | 2 | 2 tasks | 9 files |
| Phase 01-support-modules P02 | 2min | 3 tasks | 2 files |
| Phase 01-support-modules P03 | 2min | 2 tasks | 2 files |
| Phase 01-support-modules P04 | 5 | 3 tasks | 4 files |
| Phase 02-tool-updates P01 | 5 | 2 tasks | 2 files |
| Phase 02-tool-updates P02 | 2 | 2 tasks | 2 files |
| Phase 02-tool-updates P03 | 10 | 2 tasks | 2 files |
| Phase 02-tool-updates P04 | 2 | 2 tasks | 3 files |
| Phase 03-new-tools P02 | 5min | 1 task | 1 file |
| Phase 04-search-context-window P01 | 5min | 1 tasks | 1 files |
| Phase 04-search-context-window P02 | 5min | 1 tasks | 1 files |
| Phase 05-cache-error-hardening P01 | 5min | 2 tasks | 2 files |
| Phase 05-cache-error-hardening P02 | 5min | 2 tasks | 2 files |

## Accumulated Context

### Decisions

Decisions are logged in PROJECT.md Key Decisions table.
Recent decisions affecting current work:

- Names as strings (not str|int union): LLM always sends strings; Pydantic union has MCP client compatibility risk
- transliterate deferred: validate need against real contacts first; rapidfuzz alone may suffice
- Two cache layers (L1 in-memory, L2 SQLite): messages always fresh; entity metadata safe to cache
- Remove GetDialog + GetMessage: no BC obligations; tools require IDs unavailable in new format
- [Phase 01-support-modules]: Pin Python 3.13: pydantic-core PyO3 0.22.6 cannot build against Python 3.14 (system default)
- [Phase 01-support-modules]: asyncio_mode=auto in pytest: forward-compatible for future async tests, no noise on current sync stubs
- [Phase 01-support-modules]: Pass name_to_id.keys() not dict to process.extract: rapidfuzz dict dispatch expects string values, not int entity_ids
- [Phase 01-support-modules]: Ambiguity check via above_auto list len>=2 after extract: handles 2-way and N-way ties without separate logic
- [Phase 01-support-modules]: Lazy Telethon import in _describe_media(): formatter has zero hard Telethon dependency at module level
- [Phase 01-support-modules]: MockMessage/MockSender defined in test_formatter.py (not conftest): formatter-specific mocks not shared
- [Phase 01-support-modules]: all_names() returns all rows without TTL filtering — caller (Phase 2 resolver) applies its own TTL logic
- [Phase 01-support-modules]: Test monkeypatches mcp_telegram.cache.time (module attribute) not time.time directly — required for Python monkeypatching to work with module-level imports
- [Phase 02-tool-updates]: async_iter defined at module level in conftest so it can be passed directly to mock_client.iter_dialogs/iter_messages
- [Phase 02-tool-updates]: CLNP tests are sync (no async fixtures) — they verify module-level class absence, not runtime behavior
- [Phase 02-tool-updates]: get_entity_cache() creates state directory with mkdir before opening SQLite — required for first-run correctness
- [Phase 02-tool-updates]: ListDialogs drops unread field — filter moves to ListMessages where it belongs semantically
- [Phase 02-tool-updates]: _async_iter defined at module level in test_tools.py (underscore prefix) to distinguish from conftest async_iter
- [Phase 02-tool-updates]: Use mock_client.return_value (not __call__ override) for AsyncMock call stubbing in unread filter test
- [Phase 02-tool-updates]: Resolve dialog and sender names before entering async client context to fail fast
- [Phase 02-tool-updates]: next_cursor appended as plain text suffix to formatted output (single TextContent item)
- [Phase 02-tool-updates]: SearchMessages uses dialog: str (not dialog_id: int) consistent with ListMessages pattern
- [Phase 02-tool-updates]: offset-based pagination for SearchMessages: Telegram Search uses add_offset; cursor pagination incompatible
- [Phase 02-tool-updates]: make_mock_message fixture sets msg.message=text: formatter reads .message (Telethon attr), not .text
- [Phase 03-new-tools]: Per-test assignment of mock_client.get_me and get_entity (not in conftest) to avoid coupling GetMe/GetUserInfo tests
- [Phase 03-new-tools]: mock_client.return_value used for GetCommonChatsRequest stub — consistent with Phase 02 unread-filter pattern
- [Phase 04-search-context-window]: Hit marker assertion uses [HIT]/>>>/=== HIT === (not date separator ---) to avoid false-green against current formatter output
- [Phase 04-search-context-window]: test_search_messages_context updated with get_messages=AsyncMock(return_value=[]) before search_messages call so it does not crash when Wave 1 adds context fetch
- [Phase 04-search-context-window]: Use client.__call__(...) instead of client(...) in search_messages reaction loop to match test assertion mock_client.__call__.assert_called()
- [Phase 05-cache-error-hardening]: Stub stale-entity test uses instance-level monkeypatch on all_names_with_ttl to avoid class pollution
- [Phase 05-cache-error-hardening]: Upsert spy uses MagicMock(wraps=mock_cache.upsert) to capture calls while delegating to real SQLite
- [Phase 05-cache-error-hardening]: USER_TTL=2_592_000 and GROUP_TTL=604_800 exported as module-level constants — tools.py uses all_names_with_ttl instead of all_names for TTL-filtered resolution
- [Phase 05-cache-error-hardening]: Cursor error handler uses bare Exception in list_messages to uniformly catch binascii.Error, json.JSONDecodeError and ValueError from decode_cursor

### Pending Todos

None yet.

### Blockers/Concerns

- Research flag (Phase 1): `transliterate` with Ukrainian/Belarusian names — test coverage needed before resolver is complete
- Research flag (Phase 2): Pydantic v2 `str | int` union schema — verify MCP client handles `anyOf` before tool signature changes land

### Quick Tasks Completed

| # | Description | Date | Commit | Directory |
|---|-------------|------|--------|-----------|
| 1 | Resolver redesign | 2026-03-11 | 9221641 | [1-resolver-redesign](./quick/1-resolver-redesign/) |

## Session Continuity

Last session: 2026-03-11T14:59:30.133Z
Stopped at: Completed quick task 1: Resolver redesign
Resume file: None
