"""FTS stemming engine for mcp-telegram.

Provides Russian morphological stemming via snowballstemmer so that FTS5
searches match words regardless of case form, number, or tense.

  stem_text("написал сообщение") == stem_text("написали сообщениями")

The messages_fts virtual table stores pre-stemmed text for each synced
message.  At query time, stem_query() applies the same transformation so
the MATCH expression finds all morphological variants.

Design:
- Module-level stemmer is created once (thread-safe for reads).
- _WORD_RE extracts Cyrillic, Latin, and digit tokens; punctuation is
  silently dropped (matches FTS5 unicode61 tokenizer behaviour).
- backfill_fts_index() runs on every daemon startup and indexes only
  messages missing from the FTS table (idempotent, no duplicates).
"""

import re
import sqlite3
from typing import cast

import snowballstemmer  # type: ignore[import-untyped]

from .search_contracts import SEARCHABLE_QUERY_TOKEN_PATTERN
from .sync_transactions import write_transaction

# Module-level stemmer — Russian language model.
# snowballstemmer is stateless for stemWords(), safe for concurrent reads.
_russian_stemmer = snowballstemmer.stemmer("russian")

# Matches Cyrillic (including ё/Ё), Latin, and ASCII-digit word characters.
# Punctuation, whitespace, and emoji are intentionally excluded.
_WORD_RE = re.compile(f"{SEARCHABLE_QUERY_TOKEN_PATTERN}+")

# ---------------------------------------------------------------------------
# DDL and SQL constants
# ---------------------------------------------------------------------------

MESSAGES_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
    "USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, stemmed_text, "
    "tokenize='unicode61')"
)

INSERT_FTS_SQL = "INSERT INTO messages_fts(rowid, dialog_id, message_id, stemmed_text) SELECT id, dialog_id, message_id, ?3 FROM message_fts_keys WHERE dialog_id=?1 AND message_id=?2"

DELETE_FTS_SQL = (
    "DELETE FROM messages_fts WHERE rowid=(SELECT id FROM message_fts_keys WHERE dialog_id=? AND message_id=?)"
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def stem_text(text: str | None) -> str:
    """Return space-separated stemmed tokens extracted from *text*.

    Returns an empty string for None or empty input so callers can store
    the result directly into messages_fts.stemmed_text.
    """
    if not text:
        return ""
    words = _WORD_RE.findall(text)
    if not words:
        return ""
    return " ".join(_russian_stemmer.stemWords(words))


def stem_query(query: str) -> str:
    """Return space-separated quoted stemmed tokens suitable for an FTS5 MATCH clause.

    Applies the same word extraction and stemming as stem_text() so that a
    query expressed in any morphological form matches stored variants.

    Each token is wrapped in double quotes to prevent FTS5 from interpreting
    bare operator keywords (NOT, OR, AND) as boolean operators.
    """
    words = _WORD_RE.findall(query)
    if not words:
        return ""
    stemmed = _russian_stemmer.stemWords(words)
    # Defense-in-depth: re-extract only word chars from each stemmed token so that
    # any unexpected stemmer output cannot inject FTS5 operators or special chars.
    # _WORD_RE already guarantees clean input tokens, but stemmer output is not
    # contractually restricted to word-only characters.
    safe_tokens = ["".join(_WORD_RE.findall(token)) for token in stemmed]
    quoted = [f'"{t}"' for t in safe_tokens if t]
    return " ".join(quoted)


def _row_first_int(row: tuple[object | None, ...] | None) -> int:
    if row is None:
        return 0
    value = row[0]
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return 0


def backfill_fts_index(conn: sqlite3.Connection) -> int:
    """Repair live key coverage and remove duplicate/deleted rows in bounded batches."""
    last_rowid = 0
    while True:
        rows = cast(
            list[tuple[int]],
            conn.execute(
                "SELECT f.rowid FROM messages_fts f LEFT JOIN message_fts_keys k ON k.id=f.rowid "
                "LEFT JOIN messages m ON m.dialog_id=k.dialog_id AND m.message_id=k.message_id "
                "WHERE f.rowid>? AND (m.message_id IS NULL OR m.is_deleted=1 "
                "OR f.dialog_id!=k.dialog_id OR f.message_id!=k.message_id) ORDER BY f.rowid LIMIT 500",
                (last_rowid,),
            ).fetchall(),
        )
        if not rows:
            break
        with write_transaction(conn):
            conn.executemany("DELETE FROM messages_fts WHERE rowid=?", rows)
        last_rowid = rows[-1][0]
    inserted = 0
    last_key_id = 0
    while True:
        rows_to_insert = cast(
            list[tuple[int, int, int, str | None]],
            conn.execute(
                "SELECT k.id,m.dialog_id,m.message_id,m.text FROM message_fts_keys k "
                "JOIN messages m ON k.dialog_id=m.dialog_id AND k.message_id=m.message_id "
                "LEFT JOIN messages_fts f ON f.rowid=k.id WHERE k.id>? AND m.is_deleted=0 AND f.rowid IS NULL ORDER BY k.id LIMIT 500",
                (last_key_id,),
            ).fetchall(),
        )
        if not rows_to_insert:
            return inserted
        with write_transaction(conn):
            conn.executemany(INSERT_FTS_SQL, ((d, m, stem_text(t)) for _, d, m, t in rows_to_insert))
        inserted += len(rows_to_insert)
        last_key_id = rows_to_insert[-1][0]
