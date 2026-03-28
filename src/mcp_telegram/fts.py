"""FTS stemming engine for mcp-telegram (SYNC-07).

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
- backfill_fts_index() is a one-shot utility called after schema v3
  migration to populate the table from existing messages rows.
"""
from __future__ import annotations

import re
import sqlite3

import snowballstemmer  # type: ignore[import-untyped]

# Module-level stemmer — Russian language model.
# snowballstemmer is stateless for stemWords(), safe for concurrent reads.
_stemmer = snowballstemmer.stemmer("russian")

# Matches Cyrillic (including ё/Ё), Latin, and ASCII-digit word characters.
# Punctuation, whitespace, and emoji are intentionally excluded.
_WORD_RE = re.compile(r"[а-яёА-ЯЁa-zA-Z0-9]+")

# ---------------------------------------------------------------------------
# DDL and SQL constants
# ---------------------------------------------------------------------------

MESSAGES_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts "
    "USING fts5(dialog_id UNINDEXED, message_id UNINDEXED, stemmed_text, "
    "tokenize='unicode61')"
)

INSERT_FTS_SQL = (
    "INSERT OR REPLACE INTO messages_fts(dialog_id, message_id, stemmed_text) "
    "VALUES (?, ?, ?)"
)

DELETE_FTS_SQL = (
    "DELETE FROM messages_fts WHERE dialog_id=? AND message_id=?"
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
    return " ".join(_stemmer.stemWords(words))


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
    stemmed = _stemmer.stemWords(words)
    # Quote each token to prevent FTS5 operator interpretation (NOT, OR, AND)
    quoted = [f'"{token}"' for token in stemmed]
    return " ".join(quoted)


def backfill_fts_index(conn: sqlite3.Connection) -> int:
    """Populate messages_fts from all non-deleted rows in messages.

    Runs in a single transaction.  Safe to call on a fresh database (no
    messages rows) — returns 0 in that case.

    Returns the number of rows inserted.
    """
    rows = conn.execute(
        "SELECT dialog_id, message_id, text FROM messages WHERE is_deleted = 0"
    ).fetchall()

    with conn:
        conn.executemany(
            INSERT_FTS_SQL,
            ((dialog_id, message_id, stem_text(text)) for dialog_id, message_id, text in rows),
        )

    return len(rows)
