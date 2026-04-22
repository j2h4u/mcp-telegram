"""Shared read-cursor primitive for synced_dialogs (Phase 39.3-01).

Single owner of the monotonic-write SQL pattern for both inbox and outbox
read cursors. Before this module existed, the inbox-side `UPDATE synced_dialogs
SET read_inbox_max_id = MAX(COALESCE(read_inbox_max_id, 0), ?) WHERE dialog_id=?`
fragment was duplicated at ``event_handlers.py`` (live MessageRead handler)
and ``daemon.py`` (bootstrap loop). Phase 39.3 adds a symmetric ``read_outbox_max_id``
column (schema v12) — rather than triple the duplication, all callers now
route through :func:`apply_read_cursor`.

Contract:

* ``kind`` is the ``Literal["inbox", "outbox"]`` discriminator; anything else
  raises ``KeyError`` (fail loud — caller bug).
* Values flow via SQL parameters (``?``); only the column name is interpolated
  into the SQL, and it comes from a closed whitelist (``_CURSOR_COLUMNS``) —
  no SQL-injection surface.
* The caller owns the transaction boundary. The helper does NOT open a
  ``with conn:`` block; if you want the write committed, commit yourself.
* Monotonic semantics: ``MAX(COALESCE(<col>, 0), ?)`` — a smaller ``max_id``
  is silently absorbed. The stored cursor never regresses.
* ``UPDATE`` on a missing ``dialog_id`` is a no-op (affects 0 rows, no raise) —
  same as the Phase 38 inbox behaviour it replaces.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Final, Literal

ReadCursorKind = Literal["inbox", "outbox"]

_CURSOR_COLUMNS: Final[Mapping[ReadCursorKind, str]] = {
    "inbox": "read_inbox_max_id",
    "outbox": "read_outbox_max_id",
}


def apply_read_cursor(
    conn: sqlite3.Connection,
    dialog_id: int,
    kind: ReadCursorKind,
    max_id: int,
) -> int:
    """Monotonic write of a read cursor on ``synced_dialogs``.

    Args:
        conn: Open SQLite connection (caller-owned transaction).
        dialog_id: Primary-key row to update. UPDATE on a missing row is a no-op.
        kind: ``"inbox"`` (peer→me) or ``"outbox"`` (me→peer). ``KeyError``
            on any other value — caller bug, fail loud.
        max_id: New candidate cursor value. Smaller values are absorbed by
            ``MAX(COALESCE(<col>, 0), ?)`` — the cursor never regresses.

    Returns:
        ``cursor.rowcount`` from the UPDATE — ``0`` when ``dialog_id`` is not
        present in ``synced_dialogs``, ``1`` otherwise. Callers use this to
        preserve observability of "handler fired on unknown dialog" anomalies
        without re-owning the SQL string.

    Raises:
        KeyError: if ``kind`` is not one of ``_CURSOR_COLUMNS``.
    """
    column = _CURSOR_COLUMNS[kind]  # KeyError on unknown kind — not silent.
    # Safe f-string: ``column`` is always one of two hard-coded strings
    # (see _CURSOR_COLUMNS). No user-controlled text flows into the SQL.
    sql = f"UPDATE synced_dialogs SET {column} = MAX(COALESCE({column}, 0), ?) WHERE dialog_id = ?"
    cur = conn.execute(sql, (max_id, dialog_id))
    return cur.rowcount
