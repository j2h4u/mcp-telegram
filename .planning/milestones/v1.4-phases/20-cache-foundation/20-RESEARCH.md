# Phase 20: Cache Foundation - Research

**Researched:** 2026-03-20
**Domain:** SQLite schema extension, Python Protocol/proxy classes
**Confidence:** HIGH

## Summary

Phase 20 adds the SQLite message cache table and a `CachedMessage` proxy class to the
existing `entity_cache.db` database. The foundation work deliberately excludes the
read/write logic (Phase 21) and edit detection (Phase 22) — it only defines schema and
the proxy type that later phases consume.

The existing `cache.py` already has a mature pattern for extending the schema: DDL
constants at module level, an idempotent `_database_bootstrap_required` guard, and an
`_apply_column_upgrades` helper for forward-compatible ALTER TABLE migrations. Phase 20
follows that exact pattern — no new mechanisms needed.

`CachedMessage` must satisfy the `MessageLike` Protocol so `formatter.py` can consume it
without modification. The formatter accesses `.id`, `.date`, `.message`, `.sender.first_name`,
`.reply_to.reply_to_msg_id`, `.reactions`, and `.media` via `getattr` with fallbacks — a
plain dataclass with nested stub objects is sufficient.

**Primary recommendation:** Extend `_bootstrap_cache_schema` / `_database_bootstrap_required`
with the new tables; implement `CachedMessage` as a frozen dataclass with two nested stubs.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| CACHE-01 | `message_cache` SQLite table: `(dialog_id, message_id)` PK WITHOUT ROWID, fields: `sent_at`, `text`, `sender_id`, `sender_first_name`, `media_description`, `reply_to_msg_id`, `forum_topic_id`, `edit_date`, `fetched_at` | Schema design section below; WITHOUT ROWID pattern verified against SQLite docs |
| CACHE-02 | `CachedMessage` proxy class with nested attribute objects (`.sender.first_name`, `.reply_to.reply_to_msg_id`) satisfying `MessageLike` Protocol — transparent to formatter | Protocol analysis section below; formatter access patterns catalogued |
| CACHE-07 | Same SQLite DB file as `entity_cache.db` — extend existing bootstrap, no separate connection | `get_entity_cache()` in `tools/_base.py` line 234–239 is the single instantiation point; bootstrap extension pattern is the correct path |
</phase_requirements>

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| `sqlite3` | stdlib | Persistent storage | Already in use; no new deps |
| `dataclasses` | stdlib | `CachedMessage` proxy | Matches existing `models.py` style |

No new dependencies. Everything is stdlib.

---

## Architecture Patterns

### Existing DB bootstrap flow (HIGH confidence — from cache.py source)

```
EntityCache.__init__(db_path)
  └── _ensure_cache_schema(db_path)           # serialized bootstrap via fcntl lock
        ├── probe: _database_bootstrap_required(conn) → bool
        │     checks: journal_mode=WAL, all tables, all indexes, all columns
        └── if required: _bootstrap_cache_schema(conn)
              executes all DDL in one transaction, commits

ReactionMetadataCache(conn) / TopicMetadataCache(conn)
  └── _ensure_connection_schema(conn)         # for shared-connection path (in-memory too)
```

The `_database_bootstrap_required` function is the single source of truth for "does the DB
need work?" — it must be extended to check for the new tables. `_bootstrap_cache_schema`
must apply the new DDL. Both functions are extended, not replaced.

### Recommended extension points in cache.py

**New DDL constants** (at module level, same style as existing):

```python
_MESSAGE_CACHE_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS message_cache (
    dialog_id          INTEGER NOT NULL,
    message_id         INTEGER NOT NULL,
    sent_at            INTEGER NOT NULL,
    text               TEXT,
    sender_id          INTEGER,
    sender_first_name  TEXT,
    media_description  TEXT,
    reply_to_msg_id    INTEGER,
    forum_topic_id     INTEGER,
    edit_date          INTEGER,
    fetched_at         INTEGER NOT NULL,
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
"""

_MESSAGE_CACHE_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_message_cache_dialog_sent
ON message_cache(dialog_id, sent_at DESC)
"""
```

**`_database_bootstrap_required` extension** — add two new checks before `return False`:

```python
if not _table_exists(conn, "message_cache"):
    return True
if not _index_exists(conn, "idx_message_cache_dialog_sent"):
    return True
```

**`_bootstrap_cache_schema` extension** — add two new lines before `conn.commit()`:

```python
conn.execute(_MESSAGE_CACHE_TABLE_DDL)
conn.execute(_MESSAGE_CACHE_INDEX_DDL)
```

### Schema design for CACHE-01 (HIGH confidence)

```sql
CREATE TABLE IF NOT EXISTS message_cache (
    dialog_id          INTEGER NOT NULL,
    message_id         INTEGER NOT NULL,
    sent_at            INTEGER NOT NULL,   -- Unix timestamp (UTC)
    text               TEXT,              -- NULL for media-only messages
    sender_id          INTEGER,           -- NULL for anonymous/channel posts
    sender_first_name  TEXT,              -- NULL if unknown
    media_description  TEXT,             -- pre-rendered string e.g. "[фото]"
    reply_to_msg_id    INTEGER,           -- NULL if not a reply
    forum_topic_id     INTEGER,           -- NULL for non-forum dialogs
    edit_date          INTEGER,           -- NULL if never edited; used by EDIT-03
    fetched_at         INTEGER NOT NULL,  -- Unix timestamp of cache write
    PRIMARY KEY (dialog_id, message_id)
) WITHOUT ROWID
```

**WITHOUT ROWID rationale:** PK is `(dialog_id, message_id)` — both always known on
lookup. WITHOUT ROWID eliminates the secondary B-tree and stores rows in PK order,
making range scans by `(dialog_id, sent_at)` efficient via the covering index.

**Index:** `idx_message_cache_dialog_sent ON message_cache(dialog_id, sent_at DESC)` —
supports the primary read pattern: "most recent N messages in dialog X."

**sent_at as INTEGER:** Unix timestamp avoids SQLite datetime parsing. `datetime` objects
are stored/retrieved via `int(dt.timestamp())` and `datetime.fromtimestamp(ts, tz=timezone.utc)`.

### CachedMessage proxy class for CACHE-02 (HIGH confidence)

The `MessageLike` Protocol (models.py lines 36–44) requires:

```
.id        : int
.date      : datetime
.message   : str | None
.sender    : SenderLike | None      → needs .first_name: str | None
.reply_to  : ReplyHeaderLike | None → needs .reply_to_msg_id: int | None
.reactions : ReactionsLike | None   → needs .results: list
.media     : object
```

The formatter uses `getattr(..., attr, None)` everywhere — it never calls methods, never
accesses `.text` (uses `.message`), and never type-checks `.media` directly. A plain frozen
dataclass satisfies the Protocol completely.

**Implementation pattern:**

```python
# In cache.py (or a new message_cache.py if size warrants)

from datetime import datetime, timezone
from dataclasses import dataclass

@dataclass(frozen=True)
class _CachedSender:
    first_name: str | None

@dataclass(frozen=True)
class _CachedReplyHeader:
    reply_to_msg_id: int | None

@dataclass(frozen=True)
class CachedMessage:
    """MessageLike proxy backed by a message_cache row.

    Satisfies formatter.MessageLike: .id, .date, .message, .sender,
    .reply_to, .reactions, .media all present.
    """
    id: int
    date: datetime
    message: str | None
    sender: _CachedSender | None
    reply_to: _CachedReplyHeader | None
    media: object = None       # pre-described; formatter calls _describe_media only when None text
    reactions: object = None   # always None from cache — reactions not stored

    @classmethod
    def from_row(cls, row: tuple) -> "CachedMessage":
        """Construct from a message_cache SELECT row.

        Row order: dialog_id, message_id, sent_at, text, sender_id,
                   sender_first_name, media_description, reply_to_msg_id,
                   forum_topic_id, edit_date, fetched_at
        """
        _, message_id, sent_at, text, _, sender_first_name, media_description, reply_to_msg_id, _, _, _ = row
        return cls(
            id=message_id,
            date=datetime.fromtimestamp(sent_at, tz=timezone.utc),
            message=text or media_description,  # formatter checks .message first
            sender=_CachedSender(first_name=sender_first_name) if sender_first_name else None,
            reply_to=_CachedReplyHeader(reply_to_msg_id=reply_to_msg_id) if reply_to_msg_id else None,
        )
```

**media field note:** The formatter's `_render_text` checks `msg.message` first, then
`msg.media`. Storing `media_description` in `.message` (with fallback) is the simplest
path — no need to invent a fake media object. Alternatively, keep `.message = text` and
store `media_description` in a stub media object. Either works; first approach is simpler.

### CACHE-07: Sharing the DB file (HIGH confidence)

`get_entity_cache()` in `tools/_base.py` is decorated `@functools_cache` — it returns the
same `EntityCache` singleton for the process lifetime. `EntityCache.connection` exposes
the raw `sqlite3.Connection`. A future `MessageCache` class (Phase 21) should either:

1. Accept `conn: sqlite3.Connection` (same pattern as `ReactionMetadataCache`/`TopicMetadataCache`), OR
2. Accept `entity_cache: EntityCache` and call `.connection` internally.

Option 1 matches the existing pattern exactly. Phase 20 only adds schema — no
`MessageCache` class yet — but the bootstrap must run when `EntityCache` is instantiated,
which it will once the DDL and guards are in `cache.py`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Schema migration | Custom migration runner | Extend `_database_bootstrap_required` + `_bootstrap_cache_schema` | Already handles WAL, lock races, idempotency |
| Column additions | DROP+RECREATE | `_apply_column_upgrades` + `ALTER TABLE ADD COLUMN` | Existing safe pattern; SQLite ALTER TABLE is cheap |
| Protocol satisfaction | Abstract base class | `@dataclass(frozen=True)` + structural subtyping | Protocol is structural; dataclass fields are enough |
| datetime storage | `DATETIME` type | `INTEGER` (Unix timestamp) | No SQLite datetime ambiguity; consistent with `fetched_at` |

---

## Common Pitfalls

### Pitfall 1: WITHOUT ROWID requires non-NULL PK columns
**What goes wrong:** SQLite raises `NOT NULL constraint failed` if `dialog_id` or
`message_id` is NULL — WITH ROWID tables would use the hidden rowid as fallback.
**How to avoid:** Both columns are `NOT NULL` in the DDL. Enforce at the Python layer too.

### Pitfall 2: INSERT OR REPLACE deletes + reinserts (EDIT-02 concern)
**What goes wrong:** `INSERT OR REPLACE` on a WITHOUT ROWID table with a composite PK
performs DELETE then INSERT — any BEFORE UPDATE trigger would not fire (documented in
REQUIREMENTS.md Out of Scope). Phase 20 just defines the table; Phase 22 (edit detection)
must use application-level versioning, not triggers.
**How to avoid:** Noted in requirements; research confirms: use Python comparison before
writing `message_versions`.

### Pitfall 3: _database_bootstrap_required must be updated for in-memory connections
**What goes wrong:** `_ensure_connection_schema(conn)` is called for in-memory SQLite
connections (used in tests). If `_database_bootstrap_required` returns True for an
in-memory DB (which starts empty), `_bootstrap_cache_schema` is called directly — this is
correct. But if the new table checks are added incorrectly (e.g., checking for lock file),
in-memory tests will fail.
**How to avoid:** The table/index existence checks via `sqlite_master` work on in-memory
DBs. Follow the existing pattern precisely.

### Pitfall 4: Protocol structural check at type-check time
**What goes wrong:** mypy verifies `CachedMessage` satisfies `MessageLike` at the call
site, not at class definition. If `.media` is typed as `str | None` (the description
string) instead of `object`, mypy may flag it.
**How to avoid:** Type `.media` as `object` in `CachedMessage`; store `None` by default.
Keep `media_description` as a separate field used only in `from_row`.

### Pitfall 5: sent_at precision
**What goes wrong:** Telegram `message.date` is a `datetime` with second precision (no
microseconds). Storing as `int(dt.timestamp())` is safe. Storing via `dt.isoformat()`
would require parsing on read.
**How to avoid:** Use `int(dt.timestamp())` on write; `datetime.fromtimestamp(ts, tz=timezone.utc)` on read.

---

## Code Examples

### Adding a new table to the bootstrap (from existing cache.py pattern)

```python
# Source: src/mcp_telegram/cache.py lines 143–192

# Step 1: DDL constant at module level
_MESSAGE_CACHE_TABLE_DDL = "CREATE TABLE IF NOT EXISTS message_cache (...) WITHOUT ROWID"
_MESSAGE_CACHE_INDEX_DDL = "CREATE INDEX IF NOT EXISTS idx_message_cache_dialog_sent ON ..."

# Step 2: Guard check (add before `return False` in _database_bootstrap_required)
if not _table_exists(conn, "message_cache"):
    return True
if not _index_exists(conn, "idx_message_cache_dialog_sent"):
    return True

# Step 3: Apply DDL (add before conn.commit() in _bootstrap_cache_schema)
conn.execute(_MESSAGE_CACHE_TABLE_DDL)
conn.execute(_MESSAGE_CACHE_INDEX_DDL)
```

### Formatter's actual attribute access (from formatter.py)

```python
# formatter.py uses getattr with fallbacks throughout — never direct attribute access
sender = getattr(msg, "sender", None)           # line 169
first_name = getattr(sender, "first_name", None) # line 171
reply_to = getattr(msg, "reply_to", None)        # line 77
reply_id = getattr(reply_to, "reply_to_msg_id", None)  # line 79
text = getattr(msg, "message", "") or ""         # line 180
media = getattr(msg, "media", None)              # line 179
```

This means `CachedMessage` works even if `sender` or `reply_to` are `None` — the
formatter already handles that case.

---

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.0.2 + pytest-asyncio 1.3.0 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `cd /home/j2h4u/repos/j2h4u/mcp-telegram && uv run pytest tests/test_cache.py -x -q` |
| Full suite command | `cd /home/j2h4u/repos/j2h4u/mcp-telegram && uv run pytest -x -q` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|--------------|
| CACHE-01 | `message_cache` table exists with correct schema after `EntityCache` init | unit | `uv run pytest tests/test_cache.py -k "message_cache" -x` | ❌ Wave 0 |
| CACHE-01 | WITHOUT ROWID PK constraint enforced | unit | `uv run pytest tests/test_cache.py -k "message_cache_pk" -x` | ❌ Wave 0 |
| CACHE-01 | `idx_message_cache_dialog_sent` index exists | unit | `uv run pytest tests/test_cache.py -k "message_cache_index" -x` | ❌ Wave 0 |
| CACHE-02 | `CachedMessage.from_row()` produces correct field values | unit | `uv run pytest tests/test_cache.py -k "cached_message" -x` | ❌ Wave 0 |
| CACHE-02 | `CachedMessage` passes through `format_messages()` without error | unit | `uv run pytest tests/test_formatter.py -k "cached_message" -x` | ❌ Wave 0 |
| CACHE-07 | `message_cache` table created on same DB file as entities table | unit | `uv run pytest tests/test_cache.py -k "same_db" -x` | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_cache.py -x -q`
- **Per wave merge:** `uv run pytest -x -q`
- **Phase gate:** Full suite green + `uv run mypy src/` zero errors before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_cache.py` — new tests for `message_cache` schema (CACHE-01, CACHE-07); `CachedMessage` round-trip (CACHE-02). Add to existing file, do not replace.
- [ ] `tests/test_formatter.py` — one smoke test: `format_messages([CachedMessage(...)], {})` returns non-empty string (CACHE-02 transparency guarantee)

*(Existing `tests/conftest.py` fixtures: `tmp_db_path` is directly reusable; no new fixtures needed for schema tests)*

---

## Sources

### Primary (HIGH confidence)
- `src/mcp_telegram/cache.py` — full bootstrap/guard/DDL pattern, `EntityCache.connection` property
- `src/mcp_telegram/models.py` — `MessageLike`, `SenderLike`, `ReplyHeaderLike` Protocol definitions
- `src/mcp_telegram/formatter.py` — all `getattr` access patterns on `MessageLike` objects
- `src/mcp_telegram/tools/_base.py` line 233–239 — `get_entity_cache()` singleton, DB path (`xdg_state_home() / "mcp-telegram" / "entity_cache.db"`)
- `.planning/REQUIREMENTS.md` — CACHE-01, CACHE-02, CACHE-07 spec; Out of Scope table (SQLite triggers)
- `tests/test_cache.py` — test patterns for bootstrap, schema checks, TTL; reusable as reference
- `tests/conftest.py` — `tmp_db_path` fixture, `make_mock_message` factory

### Secondary (MEDIUM confidence)
- SQLite WITHOUT ROWID tables documentation — composite PK storage layout, NOT NULL requirement

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all stdlib, no new deps
- Architecture: HIGH — bootstrap extension pattern read directly from source
- Schema design: HIGH — follows existing patterns; WITHOUT ROWID is well-documented SQLite feature
- CachedMessage proxy: HIGH — Protocol attributes enumerated directly from models.py + formatter access patterns
- Pitfalls: HIGH — derived from source code analysis, not speculation

**Research date:** 2026-03-20
**Valid until:** Stable — depends only on internal codebase, not external library versions
