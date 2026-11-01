"""Durable authorization for fetching a dialog's complete history.

``synced_dialogs.status`` describes observed coverage and work state.  This
capability owns the separate, durable operator intent which authorizes full
history, delta, and body-event fetching.  The small SQLite implementation is
deliberately concrete: callers share the daemon's writer connection and each
mutation owns a short write unit or a verified nested savepoint.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, replace
from typing import cast

from .hydration_queue import HydrationQueueRepository
from .sync_transactions import require_write_transaction, write_savepoint


@dataclass(frozen=True, slots=True)
class EnrollmentIntent:
    dialog_id: int
    enabled: bool | None


@dataclass(frozen=True, slots=True)
class EnrollmentOutcome:
    dialog_id: int
    enabled: bool
    coverage_status: str | None
    action: str
    blocked_reason: str | None
    full_history_will_be_fetched: bool


@dataclass(frozen=True, slots=True)
class _TransitionDecision:
    action: str
    next_status: str | None
    blocked_reason: str | None
    fetch: bool


_COVERAGE_STATUSES = frozenset({"not_synced", "own_only", "fragment", "syncing", "synced", "access_lost"})


def request_delta_refresh(conn: sqlite3.Connection, dialog_id: int, now: int) -> None:
    """Queue a new refresh generation in the caller's write unit."""
    require_write_transaction(conn)
    conn.execute(
        "UPDATE synced_dialogs SET delta_refresh_requested_at=?, "
        "delta_refresh_generation=delta_refresh_generation+1 WHERE dialog_id=?",
        (now, dialog_id),
    )


def reset_read_position_retry(conn: sqlite3.Connection, dialog_id: int) -> int:
    """Clear reconciliation backoff when enrollment or access makes work relevant."""
    require_write_transaction(conn)
    cur = conn.execute(
        "UPDATE synced_dialogs SET read_position_next_attempt_at = NULL, "
        "read_position_attempt_count = 0 WHERE dialog_id = ?",
        (dialog_id,),
    )
    return cur.rowcount


def read_intent(conn: sqlite3.Connection, dialog_id: int) -> EnrollmentIntent:
    row = cast(
        tuple[int] | None,
        conn.execute("SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    return EnrollmentIntent(dialog_id, None if row is None else bool(row[0]))


def full_history_enabled(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Return true only for a persisted enabled intent; absence fails closed."""
    row = cast(
        tuple[int] | None,
        conn.execute(
            "SELECT enabled FROM full_history_enrollment WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    return row is not None and int(row[0]) == 1


def _coverage_status(conn: sqlite3.Connection, dialog_id: int) -> str | None:
    row = cast(
        tuple[str] | None,
        conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id = ?", (dialog_id,)).fetchone(),
    )
    if row is None:
        return None
    status = str(row[0])
    if status not in _COVERAGE_STATUSES:
        return status
    return status


def _store_intent(
    conn: sqlite3.Connection,
    dialog_id: int,
    enabled: bool,
    now: int,
) -> None:
    conn.execute(
        """INSERT INTO full_history_enrollment(dialog_id, enabled, updated_at)
           VALUES (?, ?, ?)
           ON CONFLICT(dialog_id) DO UPDATE SET
             enabled = excluded.enabled,
             updated_at = excluded.updated_at""",
        (dialog_id, int(enabled), now),
    )


def _outcome(  # noqa: PLR0913
    dialog_id: int,
    *,
    enabled: bool,
    coverage_status: str | None,
    action: str,
    blocked_reason: str | None = None,
    fetch: bool | None = None,
) -> EnrollmentOutcome:
    return EnrollmentOutcome(
        dialog_id=dialog_id,
        enabled=enabled,
        coverage_status=coverage_status,
        action=action,
        blocked_reason=blocked_reason,
        full_history_will_be_fetched=(
            fetch
            if fetch is not None
            else enabled and coverage_status in {None, "not_synced", "own_only", "fragment", "syncing"}
        ),
    )


def _decide_transition(coverage: str | None, *, enabled: bool) -> _TransitionDecision:  # noqa: PLR0911
    if not enabled:
        return _TransitionDecision("disabled_history", "not_synced" if coverage == "syncing" else coverage, None, False)
    if coverage is None:
        return _TransitionDecision("queue_full_history", "not_synced", None, True)
    if coverage in {"not_synced", "own_only", "fragment"}:
        return _TransitionDecision("queue_full_history", "syncing", None, True)
    if coverage == "syncing":
        return _TransitionDecision("already_syncing", "syncing", None, True)
    if coverage == "synced":
        return _TransitionDecision("request_delta_refresh", "synced", None, False)
    if coverage == "access_lost":
        return _TransitionDecision("blocked_access_lost", "access_lost", "access_lost", False)
    return _TransitionDecision("unsupported_coverage", coverage, "unsupported_coverage", False)


def enable_history(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    now: int | None = None,
) -> EnrollmentOutcome:
    """Persist an enabled intent and queue work according to factual coverage."""
    timestamp = int(time.time()) if now is None else now
    with write_savepoint(conn):
        coverage = _coverage_status(conn, dialog_id)
        _store_intent(conn, dialog_id, True, timestamp)
        reset_read_position_retry(conn, dialog_id)
        decision = _decide_transition(coverage, enabled=True)
        if coverage is None:
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, ?)",
                (dialog_id, decision.next_status),
            )
            coverage = decision.next_status
        elif decision.next_status != coverage and decision.next_status == "syncing":
            conn.execute(
                "UPDATE synced_dialogs SET status = 'syncing' WHERE dialog_id = ? AND status IN ('not_synced', 'own_only', 'fragment')",
                (dialog_id,),
            )
            coverage = decision.next_status
        elif decision.action == "request_delta_refresh":
            request_delta_refresh(conn, dialog_id, timestamp)
        return _outcome(
            dialog_id,
            enabled=True,
            coverage_status=coverage,
            action=decision.action,
            blocked_reason=decision.blocked_reason,
            fetch=decision.fetch,
        )


def disable_history(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    now: int | None = None,
) -> EnrollmentOutcome:
    """Persist an explicit disable without destroying factual local coverage."""
    timestamp = int(time.time()) if now is None else now
    with write_savepoint(conn):
        coverage = _coverage_status(conn, dialog_id)
        _store_intent(conn, dialog_id, False, timestamp)
        reset_read_position_retry(conn, dialog_id)
        HydrationQueueRepository(conn).remove_active_for_dialog(dialog_id)
        if coverage == "syncing":
            conn.execute("UPDATE synced_dialogs SET status = 'not_synced' WHERE dialog_id = ?", (dialog_id,))
            coverage = "not_synced"
        if coverage is not None:
            conn.execute(
                "UPDATE synced_dialogs SET delta_refresh_requested_at = NULL WHERE dialog_id = ?",
                (dialog_id,),
            )
        return _outcome(
            dialog_id,
            enabled=False,
            coverage_status=coverage,
            action="disabled_history" if coverage is not None else "disabled_history_tombstone",
        )


def ensure_automatic_dm_enrollment(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    now: int | None = None,
) -> EnrollmentOutcome:
    """Enroll a first-seen private dialog unless a decision already exists."""
    with write_savepoint(conn):
        intent = read_intent(conn, dialog_id)
        if intent.enabled is not None:
            return _outcome(
                dialog_id,
                enabled=intent.enabled,
                coverage_status=_coverage_status(conn, dialog_id),
                action="preserved_enabled_intent" if intent.enabled else "preserved_disabled_intent",
                blocked_reason=None if intent.enabled else "disabled",
            )
        outcome = enable_history(conn, dialog_id, now=now)
        if outcome.coverage_status == "not_synced":
            conn.execute("UPDATE synced_dialogs SET status='syncing' WHERE dialog_id=?", (dialog_id,))
            return replace(outcome, coverage_status="syncing")
        return outcome


def record_automatic_group_decision(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    enabled: bool,
    total_messages: int | None = None,
    now: int | None = None,
) -> EnrollmentIntent:
    """Persist a first-page group decision while preserving an existing decision."""
    timestamp = int(time.time()) if now is None else now
    with write_savepoint(conn):
        previous = read_intent(conn, dialog_id)
        if previous.enabled is not None:
            return previous
        _store_intent(conn, dialog_id, enabled, timestamp)
        if enabled:
            reset_read_position_retry(conn, dialog_id)
            if _coverage_status(conn, dialog_id) is None:
                conn.execute("INSERT INTO synced_dialogs(dialog_id, status) VALUES (?, 'syncing')", (dialog_id,))
        elif total_messages is not None:
            conn.execute(
                "INSERT INTO synced_dialogs(dialog_id, status, total_messages) VALUES (?, 'not_synced', ?) "
                "ON CONFLICT(dialog_id) DO UPDATE SET total_messages=excluded.total_messages",
                (dialog_id, total_messages),
            )
        return read_intent(conn, dialog_id)


def restore_access_status(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Restore coverage according to intent; return whether sync is authorized."""
    require_write_transaction(conn)
    enabled = full_history_enabled(conn, dialog_id)
    conn.execute(
        "UPDATE synced_dialogs SET status = ?, delta_refresh_requested_at = NULL WHERE dialog_id = ?",
        ("syncing" if enabled else "not_synced", dialog_id),
    )
    reset_read_position_retry(conn, dialog_id)
    return enabled


__all__ = [
    "EnrollmentIntent",
    "EnrollmentOutcome",
    "disable_history",
    "enable_history",
    "ensure_automatic_dm_enrollment",
    "full_history_enabled",
    "read_intent",
    "record_automatic_group_decision",
    "request_delta_refresh",
    "reset_read_position_retry",
    "restore_access_status",
]
