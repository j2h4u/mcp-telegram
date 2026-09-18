"""Public, transaction-neutral persistence operations for reaction aggregates."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from typing import cast

from .contracts import (
    ReactionAggregate,
    ReactionAggregateSource,
    ReactionObservationBoundary,
)

_DELETE_REACTIONS_SQL = "DELETE FROM message_reactions WHERE dialog_id = ? AND message_id = ?"
_INSERT_REACTION_SQL = (
    "INSERT OR REPLACE INTO message_reactions (dialog_id, message_id, emoji, count) VALUES (?, ?, ?, ?)"
)


def replace_reaction_aggregates(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    aggregates: Sequence[ReactionAggregate],
    *,
    source: ReactionAggregateSource | str = ReactionAggregateSource.HISTORY,
    observed_at: int | None = None,
    observation_sequence: int | None = None,
) -> bool:
    """Apply one aggregate observation under the durable ordering contract.

    The function name is retained for the message projection call sites, but
    all aggregate writers now pass through the same generation and stale-writer
    fence.  Empty ``aggregates`` is an authoritative observation and therefore
    removes the old projection only after the boundary has been accepted.
    """
    return apply_aggregate_observation(
        conn,
        dialog_id,
        message_id,
        aggregates,
        source=source,
        observed_at=observed_at,
        observation_sequence=observation_sequence,
    )


_SOURCE_RANKS = {source.value: source.rank for source in ReactionAggregateSource}


def _source_value(source: ReactionAggregateSource | str) -> str:
    value = source.value if isinstance(source, ReactionAggregateSource) else str(source)
    if value not in _SOURCE_RANKS:
        raise ValueError(f"unknown reaction aggregate source: {value}")
    return value


def allocate_observation_boundary(
    conn: sqlite3.Connection,
    source: ReactionAggregateSource | str,
    *,
    observed_at: int | None = None,
) -> ReactionObservationBoundary:
    """Allocate a durable monotonic sequence without scanning message state."""
    value = _source_value(source)
    when = int(time.time()) if observed_at is None else int(observed_at)
    conn.execute("INSERT OR IGNORE INTO message_reaction_observation_counter(singleton, next_sequence) VALUES (1, 0)")
    row = cast(
        tuple[object] | None,
        conn.execute(
            "UPDATE message_reaction_observation_counter SET next_sequence=next_sequence+1 "
            "WHERE singleton=1 RETURNING next_sequence"
        ).fetchone(),
    )
    if row is None:
        raise sqlite3.OperationalError("reaction observation counter did not return a sequence")
    sequence = int(cast(int | str, row[0]))
    return ReactionObservationBoundary(when, _SOURCE_RANKS[value], sequence, value)


def _boundary_from_values(
    source: ReactionAggregateSource | str,
    observed_at: int | None,
    observation_sequence: int | None,
    conn: sqlite3.Connection,
) -> ReactionObservationBoundary:
    value = _source_value(source)
    if observed_at is None or observation_sequence is None:
        return allocate_observation_boundary(conn, value, observed_at=observed_at)
    return ReactionObservationBoundary(int(observed_at), _SOURCE_RANKS[value], int(observation_sequence), value)


def apply_aggregate_observation(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    aggregates: Sequence[ReactionAggregate],
    *,
    source: ReactionAggregateSource | str,
    observed_at: int | None = None,
    observation_sequence: int | None = None,
) -> bool:
    """Persist an aggregate and invalidate detail only for a newer boundary."""
    if (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='message_reaction_aggregate_state'"
        ).fetchone()
        is None
    ):
        conn.execute(_DELETE_REACTIONS_SQL, (dialog_id, message_id))
        if aggregates:
            conn.executemany(
                _INSERT_REACTION_SQL,
                [(dialog_id, message_id, aggregate.emoji, aggregate.count) for aggregate in aggregates],
            )
        return True
    boundary = _boundary_from_values(source, observed_at, observation_sequence, conn)
    try:
        existing = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT generation, observed_at, source_rank, observation_sequence, source "
                "FROM message_reaction_aggregate_state WHERE dialog_id = ? AND message_id = ?",
                (dialog_id, message_id),
            ).fetchone(),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        # Small isolated unit fixtures may intentionally model only the legacy
        # projection. Production connections always run migration 68.
        conn.execute(_DELETE_REACTIONS_SQL, (dialog_id, message_id))
        if aggregates:
            conn.executemany(
                _INSERT_REACTION_SQL,
                [(dialog_id, message_id, aggregate.emoji, aggregate.count) for aggregate in aggregates],
            )
        return True
    if existing is not None:
        old_boundary = ReactionObservationBoundary(
            int(cast(int | str, existing[1])),
            int(cast(int | str, existing[2])),
            int(cast(int | str, existing[3])),
            str(existing[4]),
        )
        if boundary <= old_boundary:
            return False
        generation = int(cast(int | str, existing[0])) + 1
    else:
        generation = 1

    current_rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            "SELECT emoji, count FROM message_reactions WHERE dialog_id=? AND message_id=? ORDER BY emoji, count",
            (dialog_id, message_id),
        ).fetchall(),
    )
    observed_rows = sorted((aggregate.emoji, aggregate.count) for aggregate in aggregates)
    if existing is not None and current_rows == observed_rows:
        conn.execute(
            "UPDATE message_reaction_aggregate_state SET observed_at=?, observation_sequence=?, source=?, source_rank=? "
            "WHERE dialog_id=? AND message_id=?",
            (
                boundary.observed_at,
                boundary.sequence,
                boundary.source,
                boundary.source_rank,
                dialog_id,
                message_id,
            ),
        )
        return True

    conn.execute(_DELETE_REACTIONS_SQL, (dialog_id, message_id))
    if aggregates:
        conn.executemany(
            _INSERT_REACTION_SQL,
            [(dialog_id, message_id, aggregate.emoji, aggregate.count) for aggregate in aggregates],
        )
    conn.execute(
        "INSERT INTO message_reaction_aggregate_state "
        "(dialog_id, message_id, generation, observed_at, observation_sequence, source, source_rank, aggregate_row_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(dialog_id, message_id) DO UPDATE SET generation=excluded.generation, "
        "observed_at=excluded.observed_at, observation_sequence=excluded.observation_sequence, "
        "source=excluded.source, source_rank=excluded.source_rank, aggregate_row_count=excluded.aggregate_row_count",
        (
            dialog_id,
            message_id,
            generation,
            boundary.observed_at,
            boundary.sequence,
            boundary.source,
            boundary.source_rank,
            len(aggregates),
        ),
    )
    _invalidate_detail(conn, dialog_id, message_id, generation, boundary.observed_at)
    return True


def _invalidate_detail(conn: sqlite3.Connection, dialog_id: int, message_id: int, generation: int, now: int) -> None:
    """Move detail to stale while retaining rows from the last publication."""
    try:
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT display_generation FROM message_reaction_event_status WHERE dialog_id = ? AND message_id = ?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        display_generation = 0 if row is None or row[0] is None else int(cast(int | str, row[0]))
        conn.execute(
            "DELETE FROM message_reaction_events WHERE dialog_id = ? AND message_id = ? AND display_generation = 0",
            (dialog_id, message_id),
        )
        conn.execute(
            "INSERT INTO message_reaction_event_status "
            "(dialog_id, message_id, aggregate_generation, detail_generation, display_generation, "
            "published_generation, checked_at, status, returned_count, staged_count, next_offset, next_attempt_at, failure_kind) "
            "VALUES (?, ?, ?, 0, ?, 0, ?, 'stale', 0, 0, NULL, ?, NULL) "
            "ON CONFLICT(dialog_id, message_id) DO UPDATE SET aggregate_generation=excluded.aggregate_generation, "
            "detail_generation=0, display_generation=MAX(message_reaction_event_status.display_generation, excluded.display_generation), "
            "published_generation=MAX(message_reaction_event_status.published_generation, excluded.published_generation), "
            "checked_at=excluded.checked_at, status='stale', returned_count=0, staged_count=0, next_offset=NULL, "
            "next_attempt_at=excluded.next_attempt_at, failure_kind=NULL",
            (dialog_id, message_id, generation, display_generation, now, now),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        # v67 fixtures can call the aggregate primitive before migration; the
        # v68 database migration installs these tables for production.
        return
