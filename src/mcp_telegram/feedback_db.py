"""feedback_db — Feedback database lifecycle for the mcp-telegram daemon.

Manages feedback.db, a separate SQLite file for storing AI-agent feedback
entries submitted via the SubmitFeedback MCP tool.

Public API:
  get_feedback_db_path()        -> Path
  ensure_feedback_schema(path)  -> sqlite3.Connection  (caller owns lifecycle)
  VALID_SEVERITIES              frozenset[str]
  VALID_STATUSES                frozenset[str]
  _FEEDBACK_SCHEMA_VERSION      int

The daemon is the sole writer. The CLI (mcp-telegram feedback list/delete)
opens the same file read-only in a separate process — WAL mode lets them
coexist without blocking each other.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

__all__ = [
    "VALID_SEVERITIES",
    "VALID_STATUSES",
    "_FEEDBACK_SCHEMA_VERSION",
    "get_feedback_db_path",
    "ensure_feedback_schema",
]

_FEEDBACK_SCHEMA_VERSION: int = 2

VALID_SEVERITIES: frozenset[str] = frozenset({"bug", "suggestion", "question"})
VALID_STATUSES: frozenset[str] = frozenset({"open", "in_progress", "done", "dismissed"})

_FEEDBACK_DDL = """
CREATE TABLE IF NOT EXISTS feedback (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_at  INTEGER NOT NULL,
    message       TEXT NOT NULL,
    severity      TEXT,
    context       TEXT,
    model         TEXT,
    harness       TEXT
)
"""

# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def get_feedback_db_path() -> Path:
    """Return the canonical path for feedback.db under XDG state home."""
    db_dir = xdg_state_home() / "mcp-telegram"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "feedback.db"


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------


def _open_feedback_db(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite connection to feedback.db with busy_timeout=10s policy.

    No PRAGMA foreign_keys needed — feedback table has no FK references.
    """
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def ensure_feedback_schema(db_path: Path) -> sqlite3.Connection:
    """Open feedback.db, apply WAL, and run any pending migrations.

    Idempotent — calling twice on the same path is a no-op; schema_version
    table will have exactly one row with version=1.

    Returns the open connection.  Caller is responsible for closing it.
    """
    conn = _open_feedback_db(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "version INTEGER NOT NULL, "
        "applied_at INTEGER NOT NULL)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0
    if current < 1:
        conn.execute(_FEEDBACK_DDL)
        conn.execute("INSERT INTO schema_version VALUES (1, strftime('%s', 'now'))")
        conn.commit()
    if current < 2:
        conn.execute(
            "ALTER TABLE feedback ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
        )
        conn.execute(
            "ALTER TABLE feedback ADD COLUMN status_changed_at INTEGER"
        )
        conn.execute(
            "ALTER TABLE feedback ADD COLUMN status_comment TEXT"
        )
        conn.execute(
            "INSERT INTO schema_version VALUES (2, strftime('%s', 'now'))"
        )
        conn.commit()
    return conn
