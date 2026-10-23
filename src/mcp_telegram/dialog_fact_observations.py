"""Per-supplied-field ordering for canonical dialog facts."""

from __future__ import annotations

import sqlite3

from .observation_order import delivered_observation_order
from .sync_transactions import require_write_transaction


def claim_dialog_fact(
    conn: sqlite3.Connection, dialog_id: int, field: str, observation_order: int | None = None
) -> bool:
    """Advance a field fence in the same transaction as its fact and projections."""
    require_write_transaction(conn)
    order = delivered_observation_order(conn) if observation_order is None else observation_order
    return (
        conn.execute(
            "INSERT INTO dialog_fact_observations(dialog_id,field,observation_order) VALUES (?,?,?) "
            "ON CONFLICT(dialog_id,field) DO UPDATE SET observation_order=excluded.observation_order "
            "WHERE excluded.observation_order>dialog_fact_observations.observation_order RETURNING 1",
            (dialog_id, field, order),
        ).fetchone()
        is not None
    )
