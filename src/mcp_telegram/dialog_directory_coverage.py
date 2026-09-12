"""Read-only coverage projection for the canonical dialog directory."""

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


def _read_coverage_sources(
    conn: sqlite3.Connection,
) -> tuple[tuple[object, object] | None, tuple[object, object, object] | None]:
    publication = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT generation, observation_started_at FROM dialog_directory_publication WHERE singleton=1"
        ).fetchone(),
    )
    state = cast(
        tuple[object, object, object] | None,
        conn.execute(
            "SELECT status, reason, observation_started_at FROM dialog_directory_state WHERE singleton=1"
        ).fetchone(),
    )
    return publication, state


def _read_identity_aggregate(
    conn: sqlite3.Connection,
) -> tuple[object, object, object, object] | None:
    return cast(
        tuple[object, object, object, object] | None,
        conn.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN COALESCE(d.identity_complete, 0) <> 1 THEN 1 ELSE 0 END), "
            "MIN(CASE WHEN COALESCE(d.identity_complete, 0) = 1 THEN d.identity_observed_at END), "
            "MIN(d.identity_observed_at) "
            "FROM dialogs d WHERE d.hidden=0"
        ).fetchone(),
    )


def _lookup_coverage(
    identity_row: tuple[object, object, object, object] | None, published_started: int | None
) -> tuple[bool, bool]:
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
    return lookup_complete, lookup_fresh


def _coverage_status(
    published_started: int | None,
    acquisition_started: int | None,
    refresh_status: str | None,
    current: int,
) -> tuple[DirectoryCoverageStatus, int | None]:
    if published_started is None:
        status: DirectoryCoverageStatus = (
            "in_progress"
            if acquisition_started is not None and refresh_status in {"pending", "in_progress", "incomplete"}
            else "never"
        )
        return status, None
    age_seconds = max(0, current - published_started)
    status = "stale" if age_seconds >= _STALE_AFTER_SECONDS else "complete"
    return status, age_seconds


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
    sources = _read_coverage_sources(conn)
    publication, state = sources

    generation = _int_or_none(publication[0]) if publication is not None else None
    published_started = _int_or_none(publication[1]) if publication is not None else None
    refresh_status = str(state[0]) if state is not None and state[0] is not None else None
    reason = str(state[1]) if state is not None and state[1] is not None else None
    acquisition_started = _int_or_none(state[2]) if state is not None else None
    observation_started_at = published_started

    lookup_complete, lookup_fresh = _lookup_coverage(_read_identity_aggregate(conn), published_started)
    status, age_seconds = _coverage_status(published_started, acquisition_started, refresh_status, current)

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
