"""Read-only coverage projection for the canonical dialog directory."""

# ruff: noqa: PLR0914

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Literal, cast

DirectoryCoverageStatus = Literal["never", "in_progress", "stale", "complete"]
_STALE_AFTER_SECONDS = 900


@dataclass(frozen=True, slots=True)
class DialogDirectoryCoverage:
    """The local directory receipt and identity-bundle coverage boundary."""

    status: DirectoryCoverageStatus
    publication_generation: int | None
    observation_started_at: int | None
    age_seconds: int | None
    refresh_status: str | None
    lookup_complete: bool
    lookup_fresh: bool
    reason: str | None = None

    def to_wire(self) -> dict[str, object]:
        return {
            "status": self.status,
            "publication_generation": self.publication_generation,
            "observation_started_at": self.observation_started_at,
            "age_seconds": self.age_seconds,
            "refresh_status": self.refresh_status,
            "reason": self.reason,
            "lookup_complete": self.lookup_complete,
            "lookup_fresh": self.lookup_fresh,
        }


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(cast(int | str, value))
    except TypeError, ValueError:
        return None


def read_dialog_directory_coverage(
    conn: sqlite3.Connection,
    *,
    now: int | None = None,
) -> DialogDirectoryCoverage:
    """Read directory state without writing counters or refreshing Telegram facts.

    The identity query intentionally uses the canonical ``dialogs`` table only.
    Entity cache rows are not part of the directory boundary.
    """
    current = int(time.time()) if now is None else now
    try:
        publication = cast(
            tuple[object, object] | None,
            conn.execute(
                "SELECT generation, observation_started_at "
                "FROM dialog_directory_publication WHERE singleton=1"
            ).fetchone(),
        )
        state = cast(
            tuple[object, object, object] | None,
            conn.execute(
                "SELECT status, reason, observation_started_at FROM dialog_directory_state WHERE singleton=1"
            ).fetchone(),
        )
    except sqlite3.OperationalError:
        try:
            state = cast(
            tuple[object, object, object] | None,
            conn.execute(
                "SELECT status, NULL AS reason, observation_started_at FROM dialog_directory_state WHERE singleton=1"
                ).fetchone(),
            )
            publication = cast(
                tuple[object, object] | None,
                conn.execute(
                    "SELECT generation, observation_started_at FROM dialog_directory_publication WHERE singleton=1"
                ).fetchone(),
            )
        except sqlite3.OperationalError:
            # Small legacy/test databases may predate the canonical directory.
            return DialogDirectoryCoverage("never", None, None, None, None, False, False)

    generation = _int_or_none(publication[0]) if publication is not None else None
    published_started = _int_or_none(publication[1]) if publication is not None else None
    refresh_status = str(state[0]) if state is not None and state[0] is not None else None
    reason = str(state[1]) if state is not None and state[1] is not None else None
    acquisition_started = _int_or_none(state[2]) if state is not None else None
    observation_started_at = published_started

    try:
        identity_row = cast(
            tuple[object, object, object, object] | None,
            conn.execute(
                "SELECT COUNT(*), "
                "SUM(CASE WHEN COALESCE(d.identity_complete, 0) <> 1 THEN 1 ELSE 0 END), "
                "MIN(CASE WHEN COALESCE(d.identity_complete, 0) = 1 THEN d.identity_observed_at END), "
                "MIN(d.identity_observed_at) "
                "FROM dialogs d WHERE d.hidden=0"
            ).fetchone(),
        )
    except sqlite3.OperationalError:
        identity_row = None

    candidate_count = _int_or_none(identity_row[0]) if identity_row is not None else 0
    unknown_count = (_int_or_none(identity_row[1]) or 0) if identity_row is not None else 0
    lookup_complete = unknown_count == 0
    min_identity_observed = _int_or_none(identity_row[3]) if identity_row is not None else None
    lookup_fresh = lookup_complete and (
        candidate_count == 0
        or (
            published_started is not None
            and min_identity_observed is not None
            and min_identity_observed >= published_started
        )
    )

    if published_started is None:
        status: DirectoryCoverageStatus = (
            "in_progress"
            if acquisition_started is not None and refresh_status in {"pending", "in_progress", "incomplete"}
            else "never"
        )
        age_seconds = None
    else:
        age_seconds = max(0, current - published_started)
        status = "stale" if age_seconds >= _STALE_AFTER_SECONDS else "complete"

    return DialogDirectoryCoverage(
        status=status,
        publication_generation=generation,
        observation_started_at=observation_started_at,
        age_seconds=age_seconds,
        refresh_status=refresh_status,
        lookup_complete=lookup_complete,
        lookup_fresh=lookup_fresh,
        reason=reason,
    )
