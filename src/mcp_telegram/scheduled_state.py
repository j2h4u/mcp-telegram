"""Transactional scheduled acquisition access and permission state."""

from __future__ import annotations

import sqlite3
from typing import cast

from .scheduled_write_fence import invalidate_scheduled_dialog_permits
from .sync_transactions import require_write_transaction


def _invalidate_permits(conn: sqlite3.Connection, dialog_id: int) -> None:
    row = cast(
        tuple[int | None] | None,
        conn.execute("SELECT account_id FROM scheduled_sync_state WHERE key='account'").fetchone(),
    )
    if row is not None and row[0] is not None:
        invalidate_scheduled_dialog_permits(row[0], dialog_id)


def invalidate_scheduled_access_loss(conn: sqlite3.Connection, dialog_id: int, now: int) -> None:
    """Invalidate in-flight scheduled observations in the access-loss transaction."""
    require_write_transaction(conn)
    _invalidate_permits(conn, dialog_id)
    conn.execute(
        "UPDATE scheduled_reconciliation_state SET dirty_generation=dirty_generation+1,authorization_pending=1, "
        "discovery_due_at=COALESCE(discovery_due_at,?),updated_at=? WHERE dialog_id=?",
        (now, now, dialog_id),
    )


def apply_scheduled_permission(conn: sqlite3.Connection, dialog_id: int, signature: str, now: int) -> None:
    """Apply only accepted full directory permission evidence."""
    require_write_transaction(conn)
    broadcast, creator, _admin, post = (int(value) for value in signature.split(":", 4)[:4])
    denied = bool(broadcast and not creator and not post)
    previous = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT permission_signature,acquisition_block FROM scheduled_reconciliation_state WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if previous is not None and previous[0] == signature:
        return
    _invalidate_permits(conn, dialog_id)
    conn.execute(
        "INSERT INTO scheduled_reconciliation_state(dialog_id,discovery_due_at,updated_at,coverage_state,coverage_reason,permission_signature,acquisition_block) "
        "VALUES (?,?,?,'unknown','baseline_unknown',?,?) "
        "ON CONFLICT(dialog_id) DO UPDATE SET permission_signature=excluded.permission_signature, "
        "acquisition_block=excluded.acquisition_block,authorization_pending=1,dirty_generation=dirty_generation+1, "
        "discovery_due_at=COALESCE(discovery_due_at,excluded.discovery_due_at),updated_at=excluded.updated_at",
        (dialog_id, now, now, signature, "permission_denied" if denied else None),
    )
