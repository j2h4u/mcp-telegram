"""Durable ordering reserved before asynchronous Telegram acquisition."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .sync_transactions import write_savepoint

if TYPE_CHECKING:
    from .sync_db import SyncDatabaseConnection


def allocate_observation_order(conn: SyncDatabaseConnection) -> int:
    """Reserve a shared sequence in a short synchronous write unit."""
    with write_savepoint(conn):
        row = cast(
            tuple[str],
            conn.execute(
                "INSERT INTO daemon_state(key, value) VALUES ('canonical_observation_sequence', '1') "
                "ON CONFLICT(key) DO UPDATE SET value=CAST(value AS INTEGER)+1 RETURNING value"
            ).fetchone(),
        )
        return int(row[0])
