"""Durable ordering reserved before asynchronous Telegram acquisition."""

import sqlite3

from .sync_transactions import write_savepoint


def allocate_observation_order(conn: sqlite3.Connection) -> int:
    """Reserve a shared sequence in a short synchronous write unit."""
    with write_savepoint(conn):
        row = conn.execute(
            "INSERT INTO daemon_state(key, value) VALUES ('canonical_observation_sequence', '1') "
            "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1 RETURNING value"
        ).fetchone()
        return int(row[0])
