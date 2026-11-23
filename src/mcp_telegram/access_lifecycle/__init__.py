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


@dataclass(frozen=True, slots=True)
class _AccessTransition:
    record_event: bool
    select_event: bool
    observe_scope: bool


def _loss_observation(
    reason: str | None, scope: str | None, evidence: AccessLossEvidence | None
) -> tuple[str | None, str | None]:
    if evidence is None:
        return reason, scope
    if evidence.access_change_cause in {"self_left", "removed_by_admin", "banned_by_admin"}:
        scope = "membership"
    return evidence.reason_code, scope


def _classify_access_transition(
    previous_status: str | None,
    prior_scope: str | None,
    scope: str | None,
    *,
    recovery_observed: bool,
    has_evidence: bool,
) -> _AccessTransition:
    """Decide which current-episode facts a newly accepted loss can replace."""
    new_episode = previous_status != "access_lost" or recovery_observed
    accepted_scope = scope is not None and (prior_scope != "membership" or scope == "membership")
    stronger_evidence = accepted_scope and (prior_scope != scope or has_evidence)
    select_event = new_episode or stronger_evidence
    return _AccessTransition(
        record_event=select_event or has_evidence,
        select_event=select_event,
        observe_scope=select_event and accepted_scope,
    )


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
    access_scope: str | None = None,
    evidence: AccessLossEvidence | None = None,
    refresh_generation: int | None = None,
    profile_owner_account_id: int | None = None,
    observation_order: int | None = None,
    membership_revision_at: int | None = None,
    processing_at: int | None = None,
) -> AccessLossResult:
    """Atomically mark a peer inaccessible while retaining its local snapshot."""
    reason_code, access_scope = _loss_observation(reason, access_scope, evidence)
    with write_savepoint(conn):
        recovery_observed = observation_order is not None and _has_current_recovery_witness(conn, dialog_id)
        if not _accept_access_loss(conn, dialog_id, profile_owner_account_id, observation_order):
            return AccessLossResult(False, MembershipSignalCounts())
        if recovery_observed:
            conn.execute(
                "UPDATE synced_dialogs SET access_lost_at=?,access_next_revalidate_at=NULL,"
                "access_loss_scope=NULL,access_scope_observed_at=NULL,access_loss_event_seq=NULL WHERE dialog_id=?",
                (now, dialog_id),
            )
        prior_scope_row = cast(
            tuple[str | None] | None,
            conn.execute("SELECT access_loss_scope FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
        )
        prior_scope = prior_scope_row[0] if prior_scope_row else None
        previous_status = _mark_access_lost_state(conn, dialog_id, now, refresh_generation, access_scope)
        transition = _classify_access_transition(
            previous_status,
            prior_scope,
            access_scope,
            recovery_observed=recovery_observed,
            has_evidence=evidence is not None,
        )
        if transition.observe_scope:
            conn.execute("UPDATE synced_dialogs SET access_scope_observed_at=? WHERE dialog_id=?", (now, dialog_id))
        if not transition.record_event:
            return AccessLossResult(False, MembershipSignalCounts())
        event_seq = _record_access_loss(
            conn, dialog_id, now, membership_revision_at, previous_status, reason_code, evidence
        )
        if transition.select_event:
            conn.execute("UPDATE synced_dialogs SET access_loss_event_seq=? WHERE dialog_id=?", (event_seq, dialog_id))
        counts = _request_loss_membership_refresh(
            conn,
            dialog_id,
            profile_owner_account_id,
            membership_revision_at,
            now if processing_at is None else processing_at,
        )
    logger.warning("access_lost dialog_id=%d reason_code=%s", dialog_id, reason_code)
    return AccessLossResult(True, counts)


def _request_loss_membership_refresh(
    conn: sqlite3.Connection, dialog_id: int, owner: int | None, revision_at: int | None, now: int
) -> MembershipSignalCounts:
    if owner is None:
        return MembershipSignalCounts()
    return request_membership_refreshes(
        conn, owner, [MembershipChange(dialog_id, frozenset({owner}), revision_at)], now=now
    )


def _record_access_loss(  # noqa: PLR0913, PLR0917
    conn: sqlite3.Connection,
    dialog_id: int,
    observed_at: int,
    occurred_at: int | None,
    previous_status: str | None,
    reason_code: str | None,
    evidence: AccessLossEvidence | None,
) -> int:
    telegram_time = evidence is not None and occurred_at is not None
    cursor = conn.execute(
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
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _accept_access_loss(
    conn: sqlite3.Connection, dialog_id: int, owner: int | None, observation_order: int | None
) -> bool:
    if owner is not None and (observation_order is None or bound_profile_account(conn) != owner):
        return False
    return observation_order is None or claim_dialog_fact(conn, dialog_id, "access_state", observation_order)


def _mark_access_lost_state(
    conn: sqlite3.Connection, dialog_id: int, now: int, refresh_generation: int | None, access_scope: str | None
) -> str | None:
    revoke_peer(conn, dialog_id)
    row = cast(
        tuple[str | None] | None,
        conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    previous_status = str(row[0]) if row is not None and row[0] is not None else None
    if row is None:
        conn.execute(
            "INSERT INTO synced_dialogs (dialog_id, status, access_lost_at, access_loss_scope) VALUES (?, 'access_lost', ?, ?)",
            (dialog_id, now, access_scope),
        )
    else:
        conn.execute(
            "UPDATE synced_dialogs SET access_loss_scope = CASE WHEN status!='access_lost' THEN ? WHEN access_loss_scope='membership' THEN access_loss_scope ELSE COALESCE(?,access_loss_scope) END, access_next_revalidate_at = CASE WHEN status!='access_lost' THEN NULL ELSE access_next_revalidate_at END, access_lost_at = CASE WHEN status='access_lost' THEN access_lost_at ELSE ? END, status = 'access_lost', delta_refresh_requested_at = CASE WHEN ? IS NULL OR delta_refresh_generation=? THEN NULL ELSE delta_refresh_requested_at END WHERE dialog_id = ?",
            (access_scope, access_scope, now, refresh_generation, refresh_generation, dialog_id),
        )
    invalidate_scheduled_access_loss(conn, dialog_id, now)
    reset_read_position_retry(conn, dialog_id)
    _purge_hydration_jobs(conn, dialog_id)
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
            "UPDATE synced_dialogs SET access_lost_at = NULL, access_loss_scope = NULL, access_scope_observed_at = NULL, access_loss_event_seq = NULL, access_last_revalidated_at = ?, access_next_revalidate_at = NULL WHERE dialog_id = ?",
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
        conn.execute("UPDATE dialogs SET needs_refresh = 1 WHERE dialog_id = ?", (dialog_id,))
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
             AND COALESCE(access_next_revalidate_at, COALESCE(access_lost_at, 0) + ?) <= ?
           ORDER BY COALESCE(access_next_revalidate_at, COALESCE(access_lost_at, 0) + ?), dialog_id LIMIT ?""",
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
]
