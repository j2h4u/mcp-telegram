---
status: resolved
trigger: "Investigate one UAT gap and find root cause only.\n\nTruth: Concurrent MCP sessions can execute read-oriented tools without SQLite lock failures during shared cache initialization.\nExpected: Run two independent MCP client sessions against the live `mcp-telegram` container and call read-oriented tools concurrently. Shared SQLite-backed cache setup should stay available under concurrent access instead of failing during connection or cache initialization.\nActual: Parallel runtime verification hit `sqlite3.OperationalError: database is locked` from `EntityCache.__init__()` while another MCP session was active.\nErrors: `sqlite3.OperationalError: database is locked`\nReproduction: Test 6 in UAT using two independent MCP stdio client sessions against the live container; one `ListMessages` call failed while another session was active.\nTimeline: Discovered during UAT for phase 17 on 2026-03-14.\nGoal: find_root_cause_only"
created: 2026-03-14T00:00:00Z
updated: 2026-03-14T00:20:00Z
---

## Current Focus

hypothesis: Confirmed. Each MCP stdio process reruns mutating SQLite startup work in `EntityCache.__init__()`, so a new session can collide with writes from an already-active session on the shared `entity_cache.db`.
test: Diagnosis complete.
expecting: N/A
next_action: Hand off fix to make cache startup read-safe and move one-time schema/optimize work out of hot-path constructors.

## Symptoms

expected: Run two independent MCP client sessions against the live `mcp-telegram` container and call read-oriented tools concurrently without SQLite lock failures during shared cache initialization.
actual: Parallel runtime verification hit `sqlite3.OperationalError: database is locked` from `EntityCache.__init__()` while another MCP session was active.
errors: `sqlite3.OperationalError: database is locked`
reproduction: Test 6 in UAT using two independent MCP stdio client sessions against the live container; one `ListMessages` call failed while another session was active.
started: Discovered during UAT for phase 17 on 2026-03-14.

## Eliminated

## Evidence

- timestamp: 2026-03-14T00:00:00Z
  checked: `.planning/phases/17-direct-read-search-workflows/17-UAT.md`
  found: UAT test 6 is the only failed check, and it reports `sqlite3.OperationalError: database is locked` specifically from `EntityCache.__init__()` during parallel runtime verification.
  implication: The failure occurs at cache startup, not in higher-level read logic.

- timestamp: 2026-03-14T00:00:00Z
  checked: `src/mcp_telegram/cache.py` and `src/mcp_telegram/tools.py`
  found: `get_entity_cache()` is process-cached only; each MCP process opens its own SQLite connection, runs WAL setup, creates tables/indexes, commits, and runs `PRAGMA optimize` during `EntityCache.__init__()`.
  implication: Two independent MCP stdio sessions can race through the same initialization sequence against one shared `entity_cache.db`.

- timestamp: 2026-03-14T00:10:00Z
  checked: focused multiprocessing repro with only parallel `EntityCache(Path(db_path))` against a fresh temp DB
  found: Eight clean constructors completed successfully.
  implication: The failure is not a generic "two clean startups always race"; it requires concurrent locking activity on the shared SQLite file.

- timestamp: 2026-03-14T00:14:00Z
  checked: focused repro with one process holding an `IMMEDIATE` write transaction on `topic_metadata` while another connection reran the `EntityCache.__init__()` startup statements
  found: On an already-bootstrapped DB, `PRAGMA optimize` raised `sqlite3.OperationalError: database is locked` while the earlier `CREATE TABLE IF NOT EXISTS` and `CREATE INDEX IF NOT EXISTS` statements succeeded.
  implication: In steady state, the most likely exact lock point is `EntityCache.__init__()`'s unconditional `PRAGMA optimize`, which is a write-capable maintenance step running in the hot path of every new MCP process.

- timestamp: 2026-03-14T00:16:00Z
  checked: focused repro with one process holding an `IMMEDIATE` write transaction on the shared DB before `entities` existed
  found: `PRAGMA journal_mode=WAL` and `CREATE TABLE IF NOT EXISTS entities` both surfaced `database is locked`.
  implication: During cold-start or schema-creation windows, earlier startup DDL/PRAGMA steps are also unsafe lock points.

- timestamp: 2026-03-14T00:18:00Z
  checked: `src/mcp_telegram/capabilities.py`
  found: Active `ListMessages`/topic workflows lazily instantiate `TopicMetadataCache(cache._conn)` and `ReactionMetadataCache(cache._conn)`, which perform their own table/index initialization on the same shared SQLite file.
  implication: An already-active read-oriented session is still capable of holding write/schema locks against `entity_cache.db`, creating the exact cross-process contention window seen in UAT.

## Eliminated

- hypothesis: Any two concurrent `EntityCache` constructors fail by themselves on a fresh DB.
  evidence: An eight-process temp-DB constructor repro completed without errors.
  timestamp: 2026-03-14T00:10:00Z

## Resolution

root_cause: New MCP stdio sessions do not open the shared entity cache in a read-safe way. `EntityCache.__init__()` unconditionally performs mutating startup work (`PRAGMA journal_mode=WAL`, schema/index DDL, and especially `PRAGMA optimize`) on the shared `entity_cache.db` every time a process starts. Meanwhile active `ListMessages` paths can hold write/schema locks through `TopicMetadataCache` and `ReactionMetadataCache` initialization on the same file. That makes cache startup itself lock-prone across processes. The most likely steady-state exact lock point is `PRAGMA optimize`; cold-start/schema windows can also lock earlier at `PRAGMA journal_mode=WAL` or entity-table/index DDL.
fix: Removed PRAGMA optimize from EntityCache.__init__(), added busy_timeout=30000, serialized bootstrap with fcntl.flock file lock, added fast-path _database_bootstrap_required() check to skip mutating work when schema is ready. ReactionMetadataCache and TopicMetadataCache now delegate to _ensure_connection_schema() instead of running their own DDL.
verification: Code review confirms all mutating startup work is behind file lock, PRAGMA optimize removed from cache.py entirely, busy_timeout prevents transient lock errors.
files_changed: [src/mcp_telegram/cache.py]
