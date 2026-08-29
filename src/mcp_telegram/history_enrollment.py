"""Durable authorization for fetching a dialog's complete history.

``synced_dialogs.status`` describes observed coverage and work state.  This
capability owns the separate, durable operator intent which authorizes full
history, delta, and body-event fetching.  The small SQLite implementation is
deliberately concrete: callers share the daemon's writer connection and each
mutation is isolated by a SAVEPOINT.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from itertools import count
from typing import cast

from .hydration_queue import HydrationQueueRepository


class EnrollmentSource(StrEnum):
    EXPLICIT = "explicit"
    AUTOMATIC = "automatic"
    MIGRATION = "migration"


@dataclass(frozen=True, slots=True)
class EnrollmentIntent:
    dialog_id: int
    enabled: bool | None
    source: EnrollmentSource | None


@dataclass(frozen=True, slots=True)
class EnrollmentOutcome:
    dialog_id: int
    enabled: bool
    source: EnrollmentSource
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


_SAVEPOINTS = count()
_COVERAGE_STATUSES = frozenset({"not_synced", "own_only", "fragment", "syncing", "synced", "access_lost"})
_ENROLLMENT_SOURCES = frozenset(
    (EnrollmentSource.EXPLICIT.value, EnrollmentSource.AUTOMATIC.value, EnrollmentSource.MIGRATION.value)
)


def reset_read_position_retry(conn: sqlite3.Connection, dialog_id: int) -> int:
    """Clear reconciliation backoff when enrollment or access makes work relevant."""
    cur = conn.execute(
        "UPDATE synced_dialogs SET read_position_next_attempt_at = NULL, "
        "read_position_attempt_count = 0 WHERE dialog_id = ?",
        (dialog_id,),
    )
    return cur.rowcount


@contextmanager
def _savepoint(conn: sqlite3.Connection) -> Iterator[None]:
    name = f"history_enrollment_{next(_SAVEPOINTS)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def read_intent(conn: sqlite3.Connection, dialog_id: int) -> EnrollmentIntent:
    row = cast(
        tuple[int, str] | None,
        conn.execute(
            "SELECT enabled, source FROM full_history_enrollment WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None:
        return EnrollmentIntent(dialog_id, None, None)
    source = row[1]
    if source not in _ENROLLMENT_SOURCES:
        raise ValueError(f"invalid history enrollment source: {source!r}")
    return EnrollmentIntent(dialog_id, bool(row[0]), EnrollmentSource(source))


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
    source: EnrollmentSource,
    now: int,
) -> None:
    conn.execute(
        """INSERT INTO full_history_enrollment(dialog_id, enabled, source, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(dialog_id) DO UPDATE SET
             enabled = excluded.enabled,
             source = excluded.source,
             updated_at = excluded.updated_at""",
        (dialog_id, int(enabled), source.value, now),
    )


def _outcome(  # noqa: PLR0913
    dialog_id: int,
    *,
    enabled: bool,
    source: EnrollmentSource,
    coverage_status: str | None,
    action: str,
    blocked_reason: str | None = None,
    fetch: bool | None = None,
) -> EnrollmentOutcome:
    return EnrollmentOutcome(
        dialog_id=dialog_id,
        enabled=enabled,
        source=source,
        coverage_status=coverage_status,
        action=action,
        blocked_reason=blocked_reason,
        full_history_will_be_fetched=(
            fetch
            if fetch is not None
            else enabled and coverage_status in {None, "not_synced", "own_only", "fragment", "syncing"}
        ),
    )


def _decide_transition(coverage: str | None, *, enabled: bool, automatic: bool = False) -> _TransitionDecision:  # noqa: PLR0911
    if not enabled:
        return _TransitionDecision("disabled_history", "not_synced" if coverage == "syncing" else coverage, None, False)
    if coverage is None:
        return _TransitionDecision("queue_full_history", "syncing" if automatic else "not_synced", None, True)
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
    source: EnrollmentSource = EnrollmentSource.EXPLICIT,
    now: int | None = None,
) -> EnrollmentOutcome:
    """Persist an enabled intent and queue work according to factual coverage."""
    timestamp = int(time.time()) if now is None else now
    with _savepoint(conn):
        previous = read_intent(conn, dialog_id)
        coverage = _coverage_status(conn, dialog_id)
        if source is EnrollmentSource.AUTOMATIC and previous.source is EnrollmentSource.EXPLICIT:
            if previous.enabled is False:
                return _outcome(
                    dialog_id,
                    enabled=False,
                    source=previous.source,
                    coverage_status=coverage,
                    action="blocked_explicit_disable",
                    blocked_reason="explicit_disable",
                )
            return _outcome(
                dialog_id,
                enabled=True,
                source=previous.source,
                coverage_status=coverage,
                action="preserved_explicit_enable",
            )

        _store_intent(conn, dialog_id, True, source, timestamp)
        reset_read_position_retry(conn, dialog_id)
        decision = _decide_transition(coverage, enabled=True, automatic=source is EnrollmentSource.AUTOMATIC)
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
            conn.execute(
                "UPDATE synced_dialogs SET delta_refresh_requested_at = ? WHERE dialog_id = ?",
                (timestamp, dialog_id),
            )
        return _outcome(
            dialog_id,
            enabled=True,
            source=source,
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
    with _savepoint(conn):
        coverage = _coverage_status(conn, dialog_id)
        _store_intent(conn, dialog_id, False, EnrollmentSource.EXPLICIT, timestamp)
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
            source=EnrollmentSource.EXPLICIT,
            coverage_status=coverage,
            action="disabled_history" if coverage is not None else "disabled_history_tombstone",
        )


def ensure_automatic_dm_enrollment(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    now: int | None = None,
) -> EnrollmentOutcome:
    """Enroll a first-seen private dialog unless an explicit decision exists."""
    intent = read_intent(conn, dialog_id)
    if intent.source is EnrollmentSource.EXPLICIT:
        return _outcome(
            dialog_id,
            enabled=bool(intent.enabled),
            source=intent.source,
            coverage_status=_coverage_status(conn, dialog_id),
            action="preserved_explicit_decision",
            blocked_reason="explicit_disable" if intent.enabled is False else None,
        )
    if intent.enabled is True:
        return _outcome(
            dialog_id,
            enabled=True,
            source=cast(EnrollmentSource, intent.source),
            coverage_status=_coverage_status(conn, dialog_id),
            action="preserved_enabled_intent",
        )
    return enable_history(conn, dialog_id, source=EnrollmentSource.AUTOMATIC, now=now)


def restore_access_status(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Restore coverage according to intent; return whether sync is authorized."""
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
    "EnrollmentSource",
    "disable_history",
    "enable_history",
    "ensure_automatic_dm_enrollment",
    "full_history_enabled",
    "read_intent",
    "reset_read_position_retry",
    "restore_access_status",
]
