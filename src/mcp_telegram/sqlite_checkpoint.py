"""Small SQLite WAL checkpoint primitives shared by daemon-owned databases."""

import sqlite3


def checkpoint_sqlite_connection(conn: sqlite3.Connection) -> None:
    """Discard pending work and truncate the connection's SQLite WAL.

    ``rollback`` is intentionally unconditional: it is a no-op when the
    connection has no active transaction, and makes the checkpoint safe to
    run against a connection that was interrupted while writing.
    """
    conn.rollback()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
