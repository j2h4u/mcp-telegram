# Phase 1: Support Modules - Research

**Researched:** 2026-03-11
**Domain:** Python fuzzy matching, SQLite entity cache, message formatting, cursor pagination
**Confidence:** HIGH (architecture decided in PROJECT.md; library APIs verified)

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| RES-01 | LLM refers to a dialog by name string; server resolves to entity ID via WRatio fuzzy match (≥90 auto, 60–89 candidates, <60 not found) | rapidfuzz `process.extract()` with `score_cutoff`, `fuzz.WRatio` scorer, `utils.default_process` normalizer |
| RES-02 | LLM refers to a message sender by name string; same resolution algorithm and thresholds as dialog resolution | Same resolver module, same function signature — sender resolution is a second call site not a second module |
| FMT-01 | Messages in `HH:mm FirstName: text [reactions]` with date headers, session breaks (>60 min), reply annotations, inline media descriptions | Pure formatting function; no API calls; timezone via `TELEGRAM_TZ`; Python `datetime` + `zoneinfo` |
| CACH-01 | Entity metadata (users, groups, channels) persisted in SQLite; TTL 30d users, 7d groups/channels | `sqlite3` stdlib; schema `entities(id, type, name, username, updated_at)`; WAL mode for concurrent access |
| CACH-02 | Cache populated lazily from API responses (upsert on every entity-bearing response) | `INSERT OR REPLACE` or `INSERT … ON CONFLICT DO UPDATE`; called from Telethon response handler |
</phase_requirements>

---

## Summary

Phase 1 builds four pure-Python support modules that are tested in isolation before any tool wiring happens. The modules are: **resolver** (name→entity-id fuzzy lookup), **formatter** (message list → human-readable text), **cache** (SQLite entity store), and **pagination** (cursor encode/decode). None of them make Telethon API calls at test time — they depend on pre-loaded data or accept injected connections.

The architecture is fully decided in `PROJECT.md` and `STATE.md`. The only open question is the exact rapidfuzz function call shape for the ambiguity case (two candidates both ≥90). The recommended pattern is: call `process.extract(score_cutoff=60, limit=None)` and inspect the returned list — if more than one entry scores ≥90, return candidates rather than auto-resolving.

rapidfuzz must be added to `pyproject.toml` dependencies. pytest and pytest-asyncio must be added to the dev dependency group. No other new dependencies are needed.

**Primary recommendation:** Create four modules under `src/mcp_telegram/` — `resolver.py`, `formatter.py`, `cache.py`, `pagination.py` — each with a thin, typed public API, and a corresponding `tests/` directory with unit tests runnable without a live Telegram session.

---

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| rapidfuzz | ≥3.9.0 (latest 3.14.3) | WRatio fuzzy string matching | Single canonical library for fuzz matching; C extension; FuzzyWuzzy drop-in |
| sqlite3 | stdlib (SQLite 3.46.1 on this server) | Entity metadata persistence | No external dep; sufficient for single-writer use case |
| zoneinfo | stdlib (Python 3.9+) | Timezone-aware datetime formatting | Replaces `pytz`; built-in; `TELEGRAM_TZ` env var → `ZoneInfo(tz_name)` |
| base64 + json | stdlib | Opaque cursor token encode/decode | No external dep; encodes `{id, dialog_id}` dict |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest | ≥8.0 | Test runner | All unit tests |
| pytest-asyncio | ≥0.23 | Async test support | If any module uses async (cache likely doesn't; resolver/formatter definitely don't) |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| rapidfuzz | fuzzywuzzy | fuzzywuzzy is a wrapper around python-Levenshtein with slower pure-Python fallback; rapidfuzz is faster and actively maintained |
| sqlite3 stdlib | aiosqlite | aiosqlite adds async but cache ops are fast enough for sync; adds a dep |
| zoneinfo | pytz / dateutil | Both are deprecated in favour of stdlib zoneinfo for new code on Python 3.9+ |

**Installation (uv):**
```bash
uv add rapidfuzz
uv add --dev pytest pytest-asyncio
```

---

## Architecture Patterns

### Recommended Project Structure

```
src/mcp_telegram/
├── resolver.py      # Name→entity-id fuzzy lookup (sync, pure-Python)
├── formatter.py     # Message list → human-readable text (sync, pure-Python)
├── cache.py         # SQLite entity store (sync I/O, injected db path)
├── pagination.py    # Cursor encode/decode (sync, pure-Python)
└── ...              # existing server.py, telegram.py, tools.py

tests/
├── conftest.py      # Shared fixtures (in-memory SQLite, sample entities)
├── test_resolver.py # RES-01, RES-02
├── test_formatter.py # FMT-01
├── test_cache.py    # CACH-01, CACH-02
└── test_pagination.py # cursor round-trip, cross-dialog error
```

### Pattern 1: Resolver Return Type — Tagged Union via dataclass

**What:** Resolver returns one of three outcomes: resolved (single entity id), candidates (list of names for disambiguation), not-found. Use dataclasses rather than bare dicts so callers can pattern-match on type.

**When to use:** Any function that produces structurally different outcomes depending on match confidence.

**Example:**
```python
# Source: PROJECT.md algorithm spec + rapidfuzz docs
from dataclasses import dataclass
from rapidfuzz import fuzz, process, utils

AUTO_THRESHOLD = 90
CANDIDATE_THRESHOLD = 60

@dataclass(frozen=True)
class Resolved:
    entity_id: int
    display_name: str

@dataclass(frozen=True)
class Candidates:
    query: str
    matches: list[tuple[str, int, int]]  # (name, score, entity_id)

@dataclass(frozen=True)
class NotFound:
    query: str

ResolveResult = Resolved | Candidates | NotFound

def resolve(query: str, choices: dict[int, str]) -> ResolveResult:
    """Fuzzy-match query against {entity_id: name} mapping."""
    if query.isdigit():
        entity_id = int(query)
        if entity_id in choices:
            return Resolved(entity_id, choices[entity_id])
        return NotFound(query)

    # choices for rapidfuzz: {name: entity_id} or list with keyed dict
    name_to_id = {name: eid for eid, name in choices.items()}
    hits = process.extract(
        query,
        name_to_id.keys(),
        scorer=fuzz.WRatio,
        processor=utils.default_process,
        score_cutoff=CANDIDATE_THRESHOLD,
        limit=None,
    )
    # hits: [(name, score, index), ...] sorted descending by score

    above_auto = [(name, score, name_to_id[name]) for name, score, _ in hits if score >= AUTO_THRESHOLD]
    if len(above_auto) == 1:
        name, _, entity_id = above_auto[0]
        return Resolved(entity_id, name)
    if len(above_auto) >= 2:
        return Candidates(query, above_auto)
    if hits:
        candidates = [(name, score, name_to_id[name]) for name, score, _ in hits]
        return Candidates(query, candidates)
    return NotFound(query)
```

### Pattern 2: Cache — Sync SQLite with Context Manager

**What:** `EntityCache` wraps a sqlite3 connection opened at startup. WAL journal mode prevents read/write contention. Upsert uses `INSERT OR REPLACE` (simpler than `ON CONFLICT DO UPDATE` for this schema).

**When to use:** Single-writer, multiple-reader; process-scoped connection lifetime.

**Example:**
```python
# Source: sqlite3 stdlib docs + PROJECT.md schema spec
import sqlite3
from pathlib import Path

DDL = """
CREATE TABLE IF NOT EXISTS entities (
    id         INTEGER PRIMARY KEY,
    type       TEXT NOT NULL,     -- 'user' | 'group' | 'channel'
    name       TEXT NOT NULL,
    username   TEXT,
    updated_at INTEGER NOT NULL   -- Unix timestamp (seconds)
);
"""

class EntityCache:
    def __init__(self, db_path: Path) -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(DDL)
        self._conn.commit()

    def upsert(self, entity_id: int, entity_type: str, name: str, username: str | None) -> None:
        import time
        self._conn.execute(
            "INSERT OR REPLACE INTO entities (id, type, name, username, updated_at) VALUES (?,?,?,?,?)",
            (entity_id, entity_type, name, username, int(time.time())),
        )
        self._conn.commit()

    def get(self, entity_id: int, ttl_seconds: int) -> dict | None:
        import time
        row = self._conn.execute(
            "SELECT id, type, name, username, updated_at FROM entities WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        if int(time.time()) - row[4] > ttl_seconds:
            return None  # expired; caller re-fetches
        return {"id": row[0], "type": row[1], "name": row[2], "username": row[3]}

    def close(self) -> None:
        self._conn.close()
```

### Pattern 3: Cursor Encode/Decode — base64 JSON

**What:** Encodes `{id: int, dialog_id: int}` as base64 JSON. Cross-dialog mismatch raises `ValueError`.

**When to use:** `ListMessages` pagination — cursor encodes the last-seen message id and the dialog it belongs to.

**Example:**
```python
# Source: PROJECT.md pagination spec
import base64
import json

def encode_cursor(message_id: int, dialog_id: int) -> str:
    payload = json.dumps({"id": message_id, "dialog_id": dialog_id})
    return base64.urlsafe_b64encode(payload.encode()).decode()

def decode_cursor(token: str, expected_dialog_id: int) -> int:
    data = json.loads(base64.urlsafe_b64decode(token.encode()))
    if data["dialog_id"] != expected_dialog_id:
        msg = f"Cursor belongs to dialog {data['dialog_id']}, not {expected_dialog_id}"
        raise ValueError(msg)
    return data["id"]
```

### Pattern 4: Formatter — Pure Function, No API Calls

**What:** Accepts a list of pre-fetched message objects and a pre-loaded reply map; produces a single string. All API resolution happens before calling the formatter.

**When to use:** Message formatting at the tool layer — pass the reply dict in, get text out.

**Example:**
```python
# Source: PROJECT.md message format spec
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SESSION_BREAK_MINUTES = 60

def format_messages(
    messages: list,          # Telethon Message objects, newest-first
    reply_map: dict[int, object],  # message_id -> Message for reply annotation
    tz: ZoneInfo | None = None,
) -> str:
    tz = tz or ZoneInfo("UTC")
    lines: list[str] = []
    prev_date = None
    prev_dt: datetime | None = None

    for msg in reversed(messages):   # oldest-first for display
        if msg.date is None:
            continue
        dt = msg.date.astimezone(tz)
        date_str = dt.strftime("%Y-%m-%d")

        if prev_date != date_str:
            lines.append(f"--- {date_str} ---")
            prev_date = date_str
            prev_dt = None

        if prev_dt is not None and (dt - prev_dt) > timedelta(minutes=SESSION_BREAK_MINUTES):
            gap = int((dt - prev_dt).total_seconds() // 60)
            lines.append(f"--- {gap} мин ---")

        sender = _resolve_sender_name(msg)
        text = _render_text(msg)
        reply = _render_reply(msg, reply_map, tz)
        reactions = _render_reactions(msg)

        parts = [f"{dt.strftime('%H:%M')} {sender}: {text}"]
        if reply:
            parts.append(reply)
        if reactions:
            parts.append(reactions)
        lines.append("  ".join(parts))
        prev_dt = dt

    return "\n".join(lines)
```

### Anti-Patterns to Avoid

- **Resolver makes Telethon API calls:** The resolver must operate on a pre-loaded `dict[int, str]` — fetching the dialog list is the caller's job. Keeps resolver testable without mock clients.
- **Formatter queries the API for reply context:** All reply messages are loaded before calling `format_messages()`. The formatter is a pure function.
- **Cache opens a new connection per call:** Open once at startup, reuse for the process lifetime.
- **Using `Optional[X]` or `typing.Union`:** Project is Python 3.11; use `X | None` and `X | Y` syntax per dignified-python rules.
- **Relative imports:** Dignified-python mandates absolute imports; use `from mcp_telegram.resolver import resolve`.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Fuzzy string similarity | Custom edit-distance scorer | `rapidfuzz.fuzz.WRatio` | Handles partial matches, length differences, transpositions; C extension speed |
| String preprocessing (lowercase, strip) | Custom normalizer | `rapidfuzz.utils.default_process` | Tested edge cases; strips punctuation, lowercases |
| Timezone-aware formatting | Custom UTC offset math | `zoneinfo.ZoneInfo` | DST-correct; stdlib since Python 3.9 |
| Cursor serialization | Custom binary format | `base64 + json` stdlib | No deps; human-debuggable (decode in browser); already decided |
| SQLite schema migration | Custom migration runner | `CREATE TABLE IF NOT EXISTS` + `PRAGMA user_version` | Overkill for a single-table cache; IF NOT EXISTS is sufficient |

**Key insight:** The resolver and formatter are pure computation — no custom data structures needed beyond dataclasses and plain dicts. Complexity lives in the algorithm constants (90/60 thresholds), not the data model.

---

## Common Pitfalls

### Pitfall 1: `process.extract()` returns index, not entity_id

**What goes wrong:** `process.extract(query, list_of_names)` returns `(name, score, list_index)` — the third element is the list index, not the entity id. If you pass a list rather than a dict, you lose the id mapping.

**Why it happens:** rapidfuzz is a generic string matcher unaware of domain keys.

**How to avoid:** Either pass `choices` as a dict (`{name: entity_id}`) so the third element becomes the dict key, or build a parallel index `[(name, entity_id), ...]` and look up by list position after matching.

**Warning signs:** Tests pass with small lists but return wrong entity ids when names are reordered.

### Pitfall 2: WRatio ambiguity — two dialogs both ≥90

**What goes wrong:** A contact named "Ivan" and a group "Ivan's Team" both score ≥90 against query "ivan". Auto-resolve picks the first (highest score), silently ignoring the second match.

**Why it happens:** `extractOne` returns a single result; `extract(limit=1)` also returns one.

**How to avoid:** Use `extract(limit=None, score_cutoff=CANDIDATE_THRESHOLD)` and explicitly check how many results are ≥90. If ≥2, return `Candidates` not `Resolved`.

**Warning signs:** Tests with overlapping names never return `Candidates` even when they should.

### Pitfall 3: SQLite `updated_at` using wall clock for TTL comparison

**What goes wrong:** TTL check uses `datetime.now()` with naive timezone vs. a stored UTC timestamp, causing 3-hour or DST-offset errors for users in non-UTC zones.

**Why it happens:** Mixing timezone-aware and naive datetimes.

**How to avoid:** Store `updated_at` as a Unix integer (`int(time.time())`), compare with `int(time.time())`. No timezone involved.

**Warning signs:** Cache hits expire immediately or never for users in UTC+N.

### Pitfall 4: Formatter called with messages in wrong order

**What goes wrong:** Telethon `iter_messages` returns newest-first by default. If the formatter iterates in arrival order (newest-first), session break logic runs backwards — gaps appear inverted.

**Why it happens:** The formatter must display oldest-first to detect increasing time gaps correctly.

**How to avoid:** `format_messages` explicitly reverses the input: `for msg in reversed(messages)`. The caller passes newest-first (as received from Telethon); formatter handles reversal internally.

**Warning signs:** Session break line appears before older messages in tests.

### Pitfall 5: Cursor decode accepts any valid base64 JSON without dialog check

**What goes wrong:** An LLM passes a cursor from dialog A to ListMessages for dialog B. Without the dialog id cross-check, the server happily paginates from the wrong position.

**Why it happens:** The cursor only contains a message id — without the dialog id, no cross-dialog validation is possible.

**How to avoid:** Always encode `dialog_id` in the cursor. `decode_cursor(token, expected_dialog_id)` raises `ValueError` on mismatch.

**Warning signs:** Tests never cover the cross-dialog error path.

### Pitfall 6: WAL pragma on sqlite3 with default isolation_level

**What goes wrong:** On Python 3.12+, calling `PRAGMA journal_mode=WAL` inside an implicit transaction fails because WAL mode changes must happen outside a transaction.

**Why it happens:** Python 3.12 changed sqlite3's default autocommit behaviour.

**How to avoid:** Enable WAL before any other operation; since this project targets Python 3.11 (pyproject.toml `>=3.11`), use `isolation_level=None` on the connection to be safe for future Python upgrades:
```python
conn = sqlite3.connect(db_path, isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
# Then switch back to transactional mode for normal ops
conn.isolation_level = ""
```
Simpler alternative: just set WAL as the first statement and don't nest it in a commit block.

---

## Code Examples

Verified patterns from official sources:

### rapidfuzz extract with cutoff and limit=None

```python
# Source: https://rapidfuzz.github.io/RapidFuzz/Usage/process.html
from rapidfuzz import fuzz, process, utils

hits = process.extract(
    "ivan",
    ["Ivan Petrov", "Ivan's Team", "Anna Ivanova"],
    scorer=fuzz.WRatio,
    processor=utils.default_process,
    score_cutoff=60,
    limit=None,
)
# Returns: [("Ivan Petrov", 91.4, 0), ("Ivan's Team", 83.3, 1), ("Anna Ivanova", 72.1, 2)]
# (scores are illustrative; exact values depend on input)
```

### SQLite upsert pattern

```python
# Source: sqlite3 stdlib docs
conn.execute(
    "INSERT OR REPLACE INTO entities (id, type, name, username, updated_at) VALUES (?,?,?,?,?)",
    (entity_id, entity_type, name, username, int(time.time())),
)
conn.commit()
```

### Cursor round-trip

```python
# Source: PROJECT.md spec + stdlib docs
import base64, json

token = base64.urlsafe_b64encode(json.dumps({"id": 12345, "dialog_id": 999}).encode()).decode()
data = json.loads(base64.urlsafe_b64decode(token.encode()))
assert data["dialog_id"] == 999
assert data["id"] == 12345
```

### Telethon Dialog type detection

```python
# Source: https://docs.telethon.dev/en/stable/modules/custom.html
async for dialog in client.iter_dialogs():
    if dialog.is_user:
        entity_type = "user"
    elif dialog.is_group:
        entity_type = "group"
    elif dialog.is_channel:
        entity_type = "channel"
    # dialog.name — display name
    # dialog.id  — marked entity id (unique)
    # dialog.date — last message timestamp (datetime, UTC-aware)
```

### zoneinfo timezone formatting

```python
# Source: Python 3.11 stdlib docs
from zoneinfo import ZoneInfo
import os

tz = ZoneInfo(os.getenv("TELEGRAM_TZ", "UTC"))
dt_local = msg.date.astimezone(tz)
time_str = dt_local.strftime("%H:%M")
```

---

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `pytz` for timezone | `zoneinfo` (stdlib) | Python 3.9 | No extra dep; DST-correct |
| `fuzzywuzzy` | `rapidfuzz` | ~2021 | 10-100× faster; actively maintained |
| `typing.Optional[X]` | `X \| None` | Python 3.10 | Less verbosity; dignified-python requirement |
| `before_id` int param for pagination | opaque cursor token | This project | Hides internal IDs from LLM |

**Deprecated/outdated:**
- `dialog_id: int` in tool args: being replaced with `dialog: str` in Phase 2; Phase 1 resolver is the bridge.
- `before_id: int` in `ListMessages`: replaced by cursor token in Phase 2; pagination module handles encode/decode.

---

## Open Questions

1. **rapidfuzz `default_process` strips all punctuation — does it hurt Cyrillic names?**
   - What we know: `default_process` lowercases and strips non-alphanumeric characters. Cyrillic letters are alphanumeric (Unicode category L*).
   - What's unclear: Whether punctuation stripping degrades match quality for names like "Иванов-Петров" (hyphen stripped → "ИвановПетров").
   - Recommendation: Add a test with a hyphenated Cyrillic name. If scores degrade, pass `processor=lambda s: s.lower()` instead of `default_process`.

2. **`transliterate` deferred — what happens if a query is a Latin transliteration of a Cyrillic name?**
   - What we know: `transliterate` is out of scope for v1 (STATE.md, PROJECT.md).
   - What's unclear: Whether WRatio handles "Ivan" ↔ "Иван" at all (it won't — they share zero characters).
   - Recommendation: Document the limitation in the resolver module docstring. Out of scope for Phase 1.

3. **Media type detection in formatter — which Telethon message attributes to inspect?**
   - What we know: Media replaces text as `[фото]`, `[документ: name.pdf, 240KB]`, `[голосовое: 0:34]`.
   - What's unclear: Exact Telethon `message.media` type hierarchy (Photo, Document, Voice, etc.).
   - Recommendation: In the formatter implementation task, inspect `telethon.tl.types` — `isinstance(msg.media, types.MessageMediaPhoto)` etc. Low risk; enumerate during implementation.

---

## Validation Architecture

nyquist_validation is enabled (config.json).

### Test Framework

| Property | Value |
|----------|-------|
| Framework | pytest ≥8.0 + pytest-asyncio ≥0.23 |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` — Wave 0 gap |
| Quick run command | `uv run pytest tests/ -x -q` |
| Full suite command | `uv run pytest tests/ -v` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| RES-01 | dialog name string → Resolved / Candidates / NotFound with correct thresholds | unit | `uv run pytest tests/test_resolver.py -x` | Wave 0 |
| RES-01 | numeric string → bypasses fuzzy, returns Resolved or NotFound | unit | `uv run pytest tests/test_resolver.py::test_numeric_query -x` | Wave 0 |
| RES-01 | two candidates both ≥90 → returns Candidates not Resolved | unit | `uv run pytest tests/test_resolver.py::test_ambiguity -x` | Wave 0 |
| RES-02 | sender name resolves with same function as dialog, same thresholds | unit | `uv run pytest tests/test_resolver.py::test_sender_resolution -x` | Wave 0 |
| FMT-01 | `HH:mm FirstName: text` single message | unit | `uv run pytest tests/test_formatter.py::test_basic_format -x` | Wave 0 |
| FMT-01 | date header appears on day change | unit | `uv run pytest tests/test_formatter.py::test_date_header -x` | Wave 0 |
| FMT-01 | session break line at >60 min gap | unit | `uv run pytest tests/test_formatter.py::test_session_break -x` | Wave 0 |
| CACH-01 | upserted entity survives `EntityCache` close and re-open | unit | `uv run pytest tests/test_cache.py::test_persistence -x` | Wave 0 |
| CACH-01 | entity past TTL returns None (expired) | unit | `uv run pytest tests/test_cache.py::test_ttl_expiry -x` | Wave 0 |
| CACH-02 | upsert on existing entity updates `updated_at` | unit | `uv run pytest tests/test_cache.py::test_upsert_update -x` | Wave 0 |
| CACH-01 | cross-process: entity readable after SQLite file close/reopen | unit | `uv run pytest tests/test_cache.py::test_cross_process -x` | Wave 0 |
| (cursor) | encode + decode round-trip returns original id | unit | `uv run pytest tests/test_pagination.py::test_round_trip -x` | Wave 0 |
| (cursor) | decode with wrong dialog_id raises ValueError | unit | `uv run pytest tests/test_pagination.py::test_cross_dialog_error -x` | Wave 0 |

### Sampling Rate

- **Per task commit:** `uv run pytest tests/ -x -q`
- **Per wave merge:** `uv run pytest tests/ -v`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps

- [ ] `tests/__init__.py` — make tests a package (or omit if pytest finds tests by path)
- [ ] `tests/conftest.py` — shared fixtures: tmp SQLite path, sample entity dict, sample message list
- [ ] `tests/test_resolver.py` — covers RES-01, RES-02
- [ ] `tests/test_formatter.py` — covers FMT-01
- [ ] `tests/test_cache.py` — covers CACH-01, CACH-02
- [ ] `tests/test_pagination.py` — covers cursor success and cross-dialog error
- [ ] `pyproject.toml` `[tool.pytest.ini_options]` section — `asyncio_mode = "auto"` if async tests needed
- [ ] Framework install: `uv add --dev pytest pytest-asyncio`
- [ ] Library install: `uv add rapidfuzz`

---

## Sources

### Primary (HIGH confidence)

- PROJECT.md — algorithm spec (WRatio thresholds, resolver algorithm, cache schema, message format, cursor spec)
- STATE.md — locked decisions (names as strings, transliterate deferred, two-layer cache)
- REQUIREMENTS.md — requirement text and success criteria
- https://rapidfuzz.github.io/RapidFuzz/Usage/process.html — `extract()` function signature, score_cutoff semantics, return type
- https://docs.telethon.dev/en/stable/modules/custom.html — Dialog object attributes (name, id, date, is_user, is_group, is_channel)
- Python 3.11 stdlib — `sqlite3`, `base64`, `json`, `zoneinfo` — all standard, no version uncertainty

### Secondary (MEDIUM confidence)

- PyPI RapidFuzz page — latest version 3.14.3 (released 2025-11-01); confirmed via search + docs URL
- sqlite3 WAL mode guidance (Simon Willison TIL, Charles Leifer blog) — cross-verified with official SQLite docs

### Tertiary (LOW confidence)

- Exact Telethon media type hierarchy for formatter (`msg.media` subtypes) — not fetched from docs; to be confirmed during implementation

---

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — rapidfuzz docs fetched; stdlib libs; project constraints clear
- Architecture: HIGH — decisions locked in PROJECT.md; patterns are standard Python
- Pitfalls: HIGH — derived from algorithm spec decisions and known sqlite3/rapidfuzz gotchas
- Telethon media types: LOW — not verified from docs; flag for implementation task

**Research date:** 2026-03-11
**Valid until:** 2026-04-11 (rapidfuzz API stable; sqlite3 stdlib; no fast-moving deps)
