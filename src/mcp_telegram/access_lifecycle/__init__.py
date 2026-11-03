"""Application-owned access-loss and revalidation lifecycle."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import cast

from ..access_predicates import not_access_lost_sql
from ..dialog_fact_observations import claim_dialog_fact
from ..entity_profile.membership import MembershipChange, MembershipSignalCounts
from ..entity_profile.repository import bound_profile_account, request_membership_refreshes
from ..history_enrollment import reset_read_position_retry, restore_access_status
from ..hydration_queue import HydrationPriority, HydrationQueueRepository
from ..messages.sqlite_hydration_jobs import reconcile_fact_hydration_jobs_for_dialog
from ..peer_access import revoke_peer
from ..scheduled_state import invalidate_scheduled_access_loss
from ..sync_transactions import write_savepoint

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AccessLossEvidence:
    reason_code: str
    access_change_cause: str
    actor_id: int


@dataclass(frozen=True, slots=True)
class AccessLossResult:
    changed: bool
    membership_counts: MembershipSignalCounts


def _purge_hydration_jobs(conn: sqlite3.Connection, dialog_id: int) -> None:
    HydrationQueueRepository(conn).remove_active_for_dialog(dialog_id)


def _rearm_terminal_reaction_details(conn: sqlite3.Connection, dialog_id: int, now: int) -> None:
    conn.execute(
        "DELETE FROM message_reaction_events WHERE dialog_id=? AND display_generation=0 "
        "AND EXISTS (SELECT 1 FROM message_reaction_event_status s "
        "WHERE s.dialog_id=message_reaction_events.dialog_id AND s.message_id=message_reaction_events.message_id "
        "AND s.status='unavailable' AND s.failure_kind IN ('access_lost','invalid_target'))",
        (dialog_id,),
    )
    conn.execute(
        "UPDATE message_reaction_event_status SET status='stale', checked_at=?, next_offset=NULL, "
        "next_attempt_at=?, staged_count=0, failure_kind=NULL "
        "WHERE dialog_id=? AND status='unavailable' AND failure_kind IN ('access_lost','invalid_target')",
        (now, now, dialog_id),
    )


def set_access_lost(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    now: int,
    *,
    reason: str | None = None,
    evidence: AccessLossEvidence | None = None,
    refresh_generation: int | None = None,
    profile_owner_account_id: int | None = None,
    observation_order: int | None = None,
    membership_revision_at: int | None = None,
    processing_at: int | None = None,
) -> AccessLossResult:
    """Atomically mark a peer inaccessible and hide its local snapshot."""
    changed = False
    counts = MembershipSignalCounts()
    reason_code = evidence.reason_code if evidence is not None else reason
    with write_savepoint(conn):
        recovery_observed = observation_order is not None and _has_current_recovery_witness(conn, dialog_id)
        if not _accept_access_loss(conn, dialog_id, profile_owner_account_id, observation_order):
            return AccessLossResult(False, counts)
        previous_status = _mark_access_lost_state(conn, dialog_id, now, refresh_generation)
        if previous_status != "access_lost" or recovery_observed:
            _record_access_loss(conn, dialog_id, now, membership_revision_at, previous_status, reason_code, evidence)
            changed = True
            if profile_owner_account_id is not None:
                counts = request_membership_refreshes(
                    conn,
                    profile_owner_account_id,
                    [MembershipChange(dialog_id, frozenset({profile_owner_account_id}), membership_revision_at)],
                    now=now if processing_at is None else processing_at,
                )
    if changed:
        logger.warning("access_lost dialog_id=%d reason_code=%s", dialog_id, reason_code)
    return AccessLossResult(changed, counts)


def _record_access_loss(  # noqa: PLR0913, PLR0917
    conn: sqlite3.Connection,
    dialog_id: int,
    observed_at: int,
    occurred_at: int | None,
    previous_status: str | None,
    reason_code: str | None,
    evidence: AccessLossEvidence | None,
) -> None:
    telegram_time = evidence is not None and occurred_at is not None
    conn.execute(
        """INSERT INTO conversation_history_events(
               kind, occurred_at, time_basis, dialog_id, reason_code, previous_status, access_change_cause, actor_id
           ) VALUES ('access_lost', ?, ?, ?, ?, ?, ?, ?)""",
        (
            occurred_at if telegram_time else observed_at,
            "telegram" if telegram_time else "observed",
            dialog_id,
            reason_code,
            previous_status,
            evidence.access_change_cause if evidence is not None else None,
            evidence.actor_id if evidence is not None else None,
        ),
    )


def _accept_access_loss(
    conn: sqlite3.Connection, dialog_id: int, owner: int | None, observation_order: int | None
) -> bool:
    if owner is not None and (observation_order is None or bound_profile_account(conn) != owner):
        return False
    return observation_order is None or claim_dialog_fact(conn, dialog_id, "access_state", observation_order)


def _mark_access_lost_state(
    conn: sqlite3.Connection, dialog_id: int, now: int, refresh_generation: int | None
) -> str | None:
    revoke_peer(conn, dialog_id)
    row = cast(
        tuple[str | None] | None,
        conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    previous_status = str(row[0]) if row is not None and row[0] is not None else None
    if row is None:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at) VALUES (?, 'access_lost', ?)",
            (dialog_id, now),
        )
    else:
        conn.execute(
            "UPDATE synced_dialogs SET status = 'access_lost', access_lost_at = ?, delta_refresh_requested_at = CASE WHEN ? IS NULL OR delta_refresh_generation=? THEN NULL ELSE delta_refresh_requested_at END WHERE dialog_id = ?",
            (now, refresh_generation, refresh_generation, dialog_id),
        )
    invalidate_scheduled_access_loss(conn, dialog_id, now)
    reset_read_position_retry(conn, dialog_id)
    _purge_hydration_jobs(conn, dialog_id)
    conn.execute("UPDATE dialogs SET hidden = 1, snapshot_at = ? WHERE dialog_id = ?", (now, dialog_id))
    return previous_status


def record_access_recovery_observation(conn: sqlite3.Connection, dialog_id: int, observation_order: int) -> bool:
    """Fence a successful probe or page and retain its exact recovery witness."""
    with write_savepoint(conn):
        if not claim_dialog_fact(conn, dialog_id, "access_state", observation_order):
            return False
        if not claim_dialog_fact(conn, dialog_id, "access_recovery_witness", observation_order):
            raise sqlite3.IntegrityError("access recovery witness order is inconsistent")
    return True


def restore_access_after_revalidation(
    conn: sqlite3.Connection,
    dialog_id: int,
    now: int,
    *,
    total_messages: int | None = None,
    expected_observation_order: int | None = None,
) -> bool:
    """Restore access while preserving snapshot metadata and requesting refresh."""
    changed = False
    with write_savepoint(conn):
        if expected_observation_order is not None and not _current_recovery_witness(
            conn, dialog_id, expected_observation_order
        ):
            return False
        row = cast(
            tuple[str | None] | None,
            conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
        )
        was_access_lost = row is not None and row[0] == "access_lost"
        if was_access_lost:
            revoke_peer(conn, dialog_id)
        conn.execute(
            "UPDATE synced_dialogs SET access_lost_at = NULL, access_last_revalidated_at = ?, access_next_revalidate_at = NULL WHERE dialog_id = ?",
            (now, dialog_id),
        )
        restore_access_status(conn, dialog_id)
        if was_access_lost:
            conn.execute(
                "UPDATE draft_projection_runtime SET recovery_due_at=?,recovery_claimed_at=NULL "
                "WHERE singleton=1 AND EXISTS (SELECT 1 FROM draft_order_uncertainty u "
                "WHERE u.account_id=draft_projection_runtime.account_id AND u.dialog_id=?)",
                (now, dialog_id),
            )
            conn.execute(
                "UPDATE draft_sync_state SET status='recovery_needed',coverage_status='unknown',"
                "reason='scope_order_uncertain' WHERE account_id=(SELECT account_id FROM draft_projection_runtime "
                "WHERE singleton=1) AND EXISTS (SELECT 1 FROM draft_order_uncertainty u "
                "WHERE u.account_id=draft_sync_state.account_id AND u.dialog_id=?)",
                (dialog_id,),
            )
        _rearm_terminal_reaction_details(conn, dialog_id, now)
        conn.execute(
            """INSERT INTO dialogs (dialog_id, hidden, needs_refresh, snapshot_at, archived, pinned,
               unread_mentions_count, unread_reactions_count)
               VALUES (?, 0, 1, ?, 0, 0, 0, 0)
               ON CONFLICT(dialog_id) DO UPDATE SET hidden = 0, needs_refresh = 1, snapshot_at = excluded.snapshot_at""",
            (dialog_id, now),
        )
        if total_messages is not None:
            conn.execute(
                "UPDATE synced_dialogs SET total_messages = ? WHERE dialog_id = ?", (total_messages, dialog_id)
            )
        reconcile_fact_hydration_jobs_for_dialog(
            conn,
            dialog_id,
            due_at=now,
            priority=HydrationPriority.BACKFILL,
        )
        if was_access_lost:
            conn.execute(
                """INSERT INTO conversation_history_events(
                       kind, occurred_at, time_basis, dialog_id, previous_status
                   ) VALUES ('access_restored', ?, 'observed', ?, 'access_lost')""",
                (now, dialog_id),
            )
            changed = True
    if changed:
        logger.info("access_restored dialog_id=%d total_messages=%s", dialog_id, total_messages)
    return changed


def _current_recovery_witness(conn: sqlite3.Connection, dialog_id: int, observation_order: int) -> bool:
    rows = cast(
        list[tuple[str, int]],
        conn.execute(
            "SELECT field,observation_order FROM dialog_fact_observations WHERE dialog_id=? "
            "AND field IN ('access_state','access_recovery_witness')",
            (dialog_id,),
        ).fetchall(),
    )
    return {row[0]: row[1] for row in rows} == {
        "access_state": observation_order,
        "access_recovery_witness": observation_order,
    }


def _has_current_recovery_witness(conn: sqlite3.Connection, dialog_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM dialog_fact_observations state JOIN dialog_fact_observations witness "
            "ON witness.dialog_id=state.dialog_id AND witness.observation_order=state.observation_order "
            "WHERE state.dialog_id=? AND state.field='access_state' AND witness.field='access_recovery_witness'",
            (dialog_id,),
        ).fetchone()
        is not None
    )


def due_access_revalidations(conn: sqlite3.Connection, *, now: int, cooldown_seconds: int, limit: int) -> list[int]:
    rows = cast(
        list[tuple[int]],
        conn.execute(
            """SELECT dialog_id FROM synced_dialogs
           WHERE status = 'access_lost'
             AND MAX(COALESCE(access_next_revalidate_at, 0), COALESCE(access_lost_at, 0) + ?) <= ?
           ORDER BY MAX(COALESCE(access_next_revalidate_at, 0), COALESCE(access_lost_at, 0) + ?), dialog_id LIMIT ?""",
            (cooldown_seconds, now, cooldown_seconds, limit),
        ).fetchall(),
    )
    return [int(row[0]) for row in rows]


def stamp_access_revalidation(conn: sqlite3.Connection, dialog_id: int, checked_at: int, cooldown_seconds: int) -> None:
    with write_savepoint(conn):
        conn.execute(
            "UPDATE synced_dialogs SET access_last_revalidated_at = ?, "
            "access_next_revalidate_at = MAX(COALESCE(access_next_revalidate_at, 0), ?) WHERE dialog_id = ?",
            (checked_at, checked_at + cooldown_seconds, dialog_id),
        )


def complete_access_revalidation(conn: sqlite3.Connection, dialog_id: int, checked_at: int) -> None:
    """Record a successful probe while retaining the access-lost status."""
    with write_savepoint(conn):
        conn.execute(
            "UPDATE synced_dialogs SET access_last_revalidated_at = ?, access_next_revalidate_at = NULL "
            "WHERE dialog_id = ? AND status = 'access_lost'",
            (checked_at, dialog_id),
        )


def unhide_after_realtime_presence(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Expose a catalog row proved present by realtime without reviving access loss."""
    with write_savepoint(conn):
        cursor = conn.execute(
            "UPDATE dialogs SET hidden=0 WHERE dialog_id=? AND hidden=1 AND "
            + not_access_lost_sql("dialogs.dialog_id"),
            (dialog_id,),
        )
    return cursor.rowcount > 0


__all__ = [
    "AccessLossEvidence",
    "AccessLossResult",
    "complete_access_revalidation",
    "due_access_revalidations",
    "not_access_lost_sql",
    "record_access_recovery_observation",
    "restore_access_after_revalidation",
    "set_access_lost",
    "stamp_access_revalidation",
    "unhide_after_realtime_presence",
]
