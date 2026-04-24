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

import snowballstemmer  # type: ignore[import-untyped]

# Module-level stemmer — Russian language model.
# snowballstemmer is stateless for stemWords(), safe for concurrent reads.
_russian_stemmer = snowballstemmer.stemmer("russian")

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

INSERT_FTS_SQL = "INSERT INTO messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)"

DELETE_FTS_SQL = "DELETE FROM messages_fts WHERE dialog_id=? AND message_id=?"


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


def backfill_fts_index(conn: sqlite3.Connection) -> int:
    """Index messages that are missing from messages_fts.

    Only inserts rows where the (dialog_id, message_id) pair has no
    corresponding FTS entry — safe to call on every daemon startup without
    creating duplicates.  Returns 0 when the index is fully caught up.

    Uses a fast count comparison first to skip the expensive LEFT JOIN
    when the index is already complete (common case on restart).

    Returns the number of rows inserted.
    """
    msg_count = conn.execute("SELECT COUNT(*) FROM messages WHERE is_deleted = 0").fetchone()[0]
    fts_count = conn.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]

    if msg_count == 0 or fts_count >= msg_count:
        return 0

    # Avoid LEFT JOIN against FTS5 — the FTS virtual table has no B-tree index
    # for the planner, making a JOIN O(n*m). Instead: fetch both key sets into
    # Python, compute the difference, then load text only for missing rows.
    fts_keys: set[tuple[int, int]] = set(
        conn.execute("SELECT dialog_id, message_id FROM messages_fts").fetchall()
    )
    all_rows = conn.execute(
        "SELECT dialog_id, message_id, text FROM messages WHERE is_deleted = 0"
    ).fetchall()
    rows = [(d, m, t) for d, m, t in all_rows if (d, m) not in fts_keys]

    if not rows:
        return 0

    with conn:
        conn.executemany(
            INSERT_FTS_SQL,
            ((dialog_id, message_id, stem_text(text)) for dialog_id, message_id, text in rows),
        )

    return len(rows)
