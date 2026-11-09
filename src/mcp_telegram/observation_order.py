"""Durable ordering reserved before asynchronous Telegram acquisition."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from .delivered_update import current_delivered_update
from .own_only_contracts import is_channel_peer_id
from .sync_transactions import write_savepoint

if TYPE_CHECKING:
    from .sync_db import SyncDatabaseConnection


def has_nonchannel_deletion(conn: SyncDatabaseConnection, dialog_id: int, message_id: int) -> bool:
    """Channel message IDs are independent of the account's nonchannel namespace."""
    if is_channel_peer_id(dialog_id):
        return False
    return (
        conn.execute("SELECT 1 FROM nonchannel_message_deletions WHERE message_id=?", (message_id,)).fetchone()
        is not None
    )


def hydration_observation_current(
    conn: SyncDatabaseConnection, dialog_id: int, message_id: int, observation_order: int | None
) -> bool:
    if has_nonchannel_deletion(conn, dialog_id, message_id):
        return False
    row = cast(
        tuple[int, int] | None,
        conn.execute(
            "SELECT observation_order,is_deleted FROM message_observations WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        ).fetchone(),
    )
    return row is None or (not row[1] and (observation_order is None or observation_order >= row[0]))


def delivered_observation_order(conn: SyncDatabaseConnection) -> int:
    """Reuse admission order only for the currently delivered raw update."""
    current = current_delivered_update()
    return allocate_observation_order(conn) if current is None else current.observation_order


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
