# Phase 22: Edit Detection - Research

**Researched:** 2026-03-20
**Domain:** SQLite application-level versioning + formatter annotation
**Confidence:** HIGH

## Summary

Phase 22 is a focused two-part change: (1) write a version record to `message_versions` before overwriting a cache row when text has changed, and (2) emit an `[edited HH:mm]` suffix in the formatter for messages where `edit_date IS NOT NULL`.

Both the schema and the data path are already substantially in place. `message_versions` table exists (created in Phase 20, schema-only). `MessageCache.store_messages()` already extracts `edit_date` from every message. `CachedMessage` already carries `edit_date: int | None`. The formatter does not yet use `edit_date` at all. What is missing is purely: the comparison-before-replace logic and the formatter annotation.

The key constraint documented in REQUIREMENTS.md is that SQLite `INSERT OR REPLACE` is internally a DELETE + INSERT, so `BEFORE UPDATE` triggers never fire. Application-level versioning in Python is the mandated approach: read the current cached row, compare text, write to `message_versions` if changed, then proceed with the replace.

**Primary recommendation:** Add a `record_version_if_changed()` helper to `MessageCache` called inside `store_messages()` before the executemany INSERT OR REPLACE, then add an `[edited HH:mm]` suffix in `format_messages()` for any message where `edit_date` is truthy.

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|-----------------|
| EDIT-01 | `message_versions` table (dialog_id, message_id, version, old_text, edit_date) for tracking text changes | Schema already created in Phase 20 bootstrap. No DDL changes needed. Need write path in `store_messages()`. |
| EDIT-02 | Application-level versioning: before INSERT OR REPLACE, compare text with cached version, write to `message_versions` if changed | Core logic of this phase. Single SELECT before each batch write; then INSERT into `message_versions`. Trigger approach explicitly forbidden. |
| EDIT-03 | Formatter shows `[edited HH:mm]` marker on messages where `edit_date IS NOT NULL`. No false positives. | `CachedMessage.edit_date` already set from cache row. `format_messages()` needs to check `getattr(msg, 'edit_date', None)` and append marker. Telethon `Message.edit_date` is a `datetime` object; `CachedMessage.edit_date` is `int | None` (Unix timestamp). Formatter must handle both types. |
</phase_requirements>

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| sqlite3 | stdlib | Versioned row writes | Already in use throughout cache.py |
| zoneinfo | stdlib | Timezone-aware time formatting | Already used in formatter.py |

No new dependencies. This phase is pure Python + SQLite.

## Architecture Patterns

### Recommended Project Structure

No new files needed. Changes confined to:
- `src/mcp_telegram/cache.py` — `MessageCache.store_messages()` gains versioning logic
- `src/mcp_telegram/formatter.py` — `format_messages()` gains edited marker

### Pattern 1: Application-Level Versioning in `store_messages()`

**What:** Before the `executemany INSERT OR REPLACE`, check each incoming message against the currently cached text. If text differs, write a version record.

**When to use:** Always inside `store_messages()`. This is the only write path to `message_cache`.

**Approach:**

```python
# Source: REQUIREMENTS.md EDIT-02, cache.py existing patterns

def _record_versions_if_changed(
    conn: sqlite3.Connection,
    dialog_id: int,
    incoming: list[tuple[int, str | None]],  # (message_id, new_text)
) -> None:
    """Write message_versions rows for messages whose text has changed."""
    if not incoming:
        return
    ids = [row[0] for row in incoming]
    placeholders = ",".join("?" * len(ids))
    existing = dict(conn.execute(
        f"SELECT message_id, text, edit_date FROM message_cache "
        f"WHERE dialog_id = ? AND message_id IN ({placeholders})",
        [dialog_id, *ids],
    ).fetchall())

    version_rows: list[tuple[object, ...]] = []
    for msg_id, new_text in incoming:
        cached = existing.get(msg_id)
        if cached is None:
            continue  # New message, no prior version to record
        old_text, old_edit_date = cached[0], cached[1]
        if old_text == new_text:
            continue  # Text unchanged, skip
        # Determine next version number
        max_ver = conn.execute(
            "SELECT MAX(version) FROM message_versions WHERE dialog_id=? AND message_id=?",
            (dialog_id, msg_id),
        ).fetchone()[0] or 0
        version_rows.append((dialog_id, msg_id, max_ver + 1, old_text, old_edit_date))

    if version_rows:
        conn.executemany(
            "INSERT OR REPLACE INTO message_versions "
            "(dialog_id, message_id, version, old_text, edit_date) VALUES (?, ?, ?, ?, ?)",
            version_rows,
        )
```

**Note on batching:** The SELECT IN query scales fine for a page of 20-50 messages. No loop-per-message needed.

### Pattern 2: `[edited HH:mm]` in `format_messages()`

**What:** After resolving `text`, check `edit_date` on the message and append the marker.

**When to use:** Inside the per-message loop in `format_messages()`.

**Approach:**

```python
# Source: formatter.py existing pattern, REQUIREMENTS.md EDIT-03

edit_date_raw = getattr(msg, "edit_date", None)
if edit_date_raw is not None:
    if isinstance(edit_date_raw, datetime):
        ed_dt = edit_date_raw.astimezone(effective_tz)
    else:
        # int Unix timestamp (from CachedMessage)
        ed_dt = datetime.fromtimestamp(int(edit_date_raw), tz=timezone.utc).astimezone(effective_tz)
    text = f"{text} [edited {ed_dt.strftime('%H:%M')}]"
```

**Where in format_messages():** After `_render_text(msg)`, before reactions are appended.

**False positive prevention:** `edit_date` is `None` on non-edited messages. Telethon sets it only when a message was actually edited. No separate `is_edited` flag is needed or wanted per REQUIREMENTS.md.

### Anti-Patterns to Avoid

- **SQLite BEFORE UPDATE trigger for versioning:** INSERT OR REPLACE is DELETE + INSERT; the trigger never fires. Explicitly out of scope per REQUIREMENTS.md Out of Scope section.
- **Per-message SELECT in a loop:** Use SELECT IN for the batch; avoids N individual queries per `store_messages()` call.
- **Adding `is_edited` column to `message_cache`:** Out of scope. `edit_date IS NOT NULL` is sufficient.
- **Committing version rows separately from the main INSERT OR REPLACE:** Both the version writes and the cache update should be in a single transaction (one `conn.commit()` call at the end of `store_messages()`).

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Change detection | Custom diff / hash comparison | Direct `==` text comparison | Messages have stable text; string equality is sufficient and fast |
| Version numbering | Auto-increment trigger | MAX(version)+1 in Python | WITHOUT ROWID tables don't have rowid-based autoincrement; Python MAX is idiomatic |
| Timezone conversion | Manual UTC math | `datetime.fromtimestamp(..., tz=timezone.utc).astimezone(tz)` | Already used in formatter.py |

## Common Pitfalls

### Pitfall 1: Forgetting that `edit_date` can be a `datetime` or `int`

**What goes wrong:** Telethon `Message.edit_date` is a `datetime` object. `CachedMessage.edit_date` is an `int | None` (Unix timestamp, stored in cache.py). The formatter receives both types via `MessageLike`.

**Why it happens:** `CachedMessage.from_row()` converts `edit_date` to `int`. Live Telethon messages keep it as `datetime`.

**How to avoid:** Always branch on `isinstance(edit_date_raw, datetime)` in the formatter before converting.

**Warning signs:** `AttributeError: 'int' object has no attribute 'astimezone'` in formatter tests.

### Pitfall 2: Transaction boundary — version writes and cache writes must be one transaction

**What goes wrong:** If version rows are committed before the INSERT OR REPLACE and then the replace fails, you have orphan version records pointing to a cache row that was not updated.

**Why it happens:** Calling `conn.commit()` after the version INSERT but before the cache executemany.

**How to avoid:** Call `conn.commit()` once, after both `executemany` calls (version rows first, then cache rows). The current `store_messages()` ends with a single `conn.commit()` — preserve that pattern.

### Pitfall 3: Calling `MAX(version)` per message in a loop

**What goes wrong:** N round-trips for N messages, all within a single `store_messages()` call.

**Why it happens:** Naive loop implementation.

**How to avoid:** Use a single `SELECT message_id, MAX(version) ... GROUP BY message_id` for all changed messages, then resolve versions in Python.

**Optimized query:**
```sql
SELECT message_id, MAX(version)
FROM message_versions
WHERE dialog_id = ? AND message_id IN (?, ?, ...)
GROUP BY message_id
```

### Pitfall 4: `[edited HH:mm]` appended after reactions string

**What goes wrong:** Output reads `"text [👍×2] [edited 14:30]"` which looks odd; the edit marker should come before reactions.

**Why it happens:** Appending the edited marker after `reactions_str` is joined.

**How to avoid:** Append the edited marker to `text` before the reactions append block in `format_messages()`.

### Pitfall 5: No version recorded for a new message that arrives with `edit_date` set

**What goes wrong:** A message could arrive from Telegram API already having an `edit_date` (edited before we ever cached it). We don't need to record a version for it — there's no prior text in our cache.

**Why it happens:** Checking `edit_date IS NOT NULL` as the version trigger instead of "does the text differ from what we have cached."

**How to avoid:** Version writes are triggered by `old_text != new_text`, not by presence of `edit_date`. This is already the correct approach per EDIT-02.

## Code Examples

### Current `store_messages()` structure (cache.py lines 372-438)

The existing method builds a `rows` list then calls `executemany` + `commit`. The versioning logic fits between the `rows` build loop and the `executemany` call. The method signature does not need to change.

### Formatter message line assembly (formatter.py lines 69-100)

Current order: resolve sender_name → render text → reactions → reply_prefix → topic_prefix → line_prefix → append line.

The edited marker should be appended to `text` (line 70 area) before the reactions join at lines 73-74.

### `message_versions` schema (already in place)

```sql
CREATE TABLE IF NOT EXISTS message_versions (
    dialog_id   INTEGER NOT NULL,
    message_id  INTEGER NOT NULL,
    version     INTEGER NOT NULL,
    old_text    TEXT,
    edit_date   INTEGER,
    PRIMARY KEY (dialog_id, message_id, version)
) WITHOUT ROWID
```

`edit_date` in `message_versions` stores the **old** `edit_date` value from the cache row being overwritten — i.e., when the previous version was set. This is distinct from the `edit_date` on the incoming new message.

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| Trigger-based versioning | Application-level versioning | Phase 22 design | SQLite `INSERT OR REPLACE` = DELETE + INSERT; triggers don't fire |
| `is_edited` boolean column | `edit_date IS NOT NULL` check | Phase 22 design decision | Fewer columns, same expressiveness |
| Schema-only `message_versions` | Populated `message_versions` | This phase | Edit history becomes queryable for EHIST-01 (future) |

## Open Questions

1. **`edit_date` in `message_versions` — which value?**
   - What we know: The column exists and is `INTEGER`. REQUIREMENTS.md says "old_text, edit_date" without specifying which message's `edit_date`.
   - What's reasonable: Store the `edit_date` from the **existing cache row** being displaced (i.e., when was the previous version set). This allows reconstructing "at what timestamp was version N set."
   - Recommendation: Use `existing_row.edit_date` (the value being overwritten), not the incoming message's `edit_date`. Consistent with naming: `old_text` stores the old text, `edit_date` stores the old timestamp.

2. **Batching version number lookup**
   - What we know: `version` is part of the PK. `MAX(version)+1` per message_id is needed.
   - What's unclear: Whether a single GROUP BY query is worth the complexity vs. MAX per changed message.
   - Recommendation: If changed messages per call is typically 0-2 (edits are rare), individual MAX queries are fine and simpler. Note in plan to consider GROUP BY if profiling shows it matters.

## Validation Architecture

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest 9.x + pytest-asyncio |
| Config file | `pyproject.toml` `[tool.pytest.ini_options]` |
| Quick run command | `uv run pytest tests/test_cache.py tests/test_formatter.py -x --tb=short -q` |
| Full suite command | `uv run pytest -x --tb=short -q` |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| EDIT-01 | `message_versions` table accepts version rows | unit | `uv run pytest tests/test_cache.py -k "versions" -x` | ✅ (schema tests exist; write tests needed) |
| EDIT-02 | Re-storing a message with changed text writes a `message_versions` row | unit | `uv run pytest tests/test_cache.py -k "edit_detection or version" -x` | ❌ Wave 0 |
| EDIT-02 | Re-storing a message with unchanged text writes NO version row | unit | same | ❌ Wave 0 |
| EDIT-02 | First-time store of a message writes NO version row | unit | same | ❌ Wave 0 |
| EDIT-03 | `format_messages()` appends `[edited HH:mm]` when `edit_date` is set | unit | `uv run pytest tests/test_formatter.py -k "edited" -x` | ❌ Wave 0 |
| EDIT-03 | `format_messages()` does NOT append edited marker when `edit_date` is None | unit | same | ❌ Wave 0 |
| EDIT-03 | Marker works with `CachedMessage` (int `edit_date`) and Telethon Message (datetime `edit_date`) | unit | same | ❌ Wave 0 |

### Sampling Rate
- **Per task commit:** `uv run pytest tests/test_cache.py tests/test_formatter.py -x --tb=short -q`
- **Per wave merge:** `uv run pytest -x --tb=short -q`
- **Phase gate:** Full suite green before `/gsd:verify-work`

### Wave 0 Gaps
- [ ] `tests/test_cache.py` — add `test_store_messages_records_version_on_text_change`, `test_store_messages_no_version_on_unchanged_text`, `test_store_messages_no_version_on_first_store` (append to existing file)
- [ ] `tests/test_formatter.py` — add `test_edited_marker_shown_when_edit_date_set` (int and datetime), `test_edited_marker_absent_when_edit_date_none`

*(No new test files needed — append to existing test modules)*

## Sources

### Primary (HIGH confidence)
- `src/mcp_telegram/cache.py` — full read; schema DDL, `store_messages()`, `CachedMessage.from_row()`
- `src/mcp_telegram/formatter.py` — full read; `format_messages()` structure and message line assembly
- `.planning/REQUIREMENTS.md` — EDIT-01, EDIT-02, EDIT-03 spec; Out of Scope section (trigger prohibition, is_edited prohibition)
- `tests/test_cache.py` — full read; existing test patterns, fixture shapes, `_make_msg()` helper
- `tests/test_formatter.py` — read; `MockMessage` shape, `_make_msg()` helper

### Secondary (MEDIUM confidence)
- `.planning/STATE.md` — Phase 20 decision: "message_versions schema-only in Plan 01 — Phase 22 populates; schema-first keeps bootstrap idempotent"
- SQLite documentation (known behavior): `INSERT OR REPLACE` = DELETE + INSERT, `BEFORE UPDATE` trigger never fires

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — no new deps, existing codebase fully read
- Architecture: HIGH — schema exists, write path and formatter location precisely identified
- Pitfalls: HIGH — derived from direct code inspection and documented decisions

**Research date:** 2026-03-20
**Valid until:** 60 days (stable domain, no external dependencies)
