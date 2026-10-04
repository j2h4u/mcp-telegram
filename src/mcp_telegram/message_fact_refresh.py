"""Background acquisition for optional Telegram message facts.

Read tools must remain SQLite-only.  This module owns the daemon-side Telegram
refresh lane that materializes optional facts into local tables for later
projection by read tools.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from .models import ReadMessage
from .reactions import ReactionDetailRefresher
from .sync_transactions import write_transaction
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    acquisition_context,
    demand_context,
)
from .telegram_fact_queries import enrich_read_at
from .telegram_reading import READ_DATE_REASONS, TelegramReadReceiptGateway
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import rpc_attempt_budget

_ReactionCandidate = tuple[int, int, int, str | None]
_ReactionRankedCandidate = tuple[tuple[int, int, int, int, int, int], _ReactionCandidate]

_REACTION_CANDIDATES_SQL = """
{requested_cte}
SELECT m.dialog_id, m.message_id, a.generation, d.next_offset
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN message_reaction_aggregate_state a ON a.dialog_id=m.dialog_id AND a.message_id=m.message_id
LEFT JOIN message_reaction_event_status d ON d.dialog_id=m.dialog_id AND d.message_id=m.message_id
WHERE sd.status = 'synced'
  AND m.is_deleted = 0
  AND (a.aggregate_row_count > 0
       OR d.status IN ('partial','unavailable'))
  AND (d.dialog_id IS NULL OR d.aggregate_generation < a.generation
       OR d.status = 'stale'
       OR (d.status IN ('partial','unavailable') AND d.next_attempt_at IS NOT NULL
           AND d.next_attempt_at <= ?))
  {requested_filter}
ORDER BY
  CASE
    WHEN d.status = 'partial' AND d.next_offset IS NOT NULL THEN 0
    WHEN d.dialog_id IS NULL OR d.aggregate_generation < a.generation OR d.status = 'stale' THEN 1
    ELSE 2
  END,
  CASE
    WHEN d.dialog_id IS NULL OR d.aggregate_generation < a.generation OR d.status = 'stale' THEN 0
    ELSE COALESCE(d.next_attempt_at, 0)
  END,
  COALESCE(d.checked_at, 0),
  m.sent_at DESC, m.dialog_id, m.message_id
LIMIT ?
"""

_REACTION_DISCOVERY_PAGE_SIZE = 256
# ponytail: New read-at facts may take up to 60 seconds to become visible to scheduling.
_REACTION_DISCOVERY_REFRESH_SECONDS = 60
_REACTION_DISCOVERY_PAGE_SQL = """
WITH aggregate_page AS MATERIALIZED (
  SELECT dialog_id, message_id, generation, aggregate_row_count
  FROM message_reaction_aggregate_state
  {keyset}
  ORDER BY dialog_id, message_id
  LIMIT ?
)
SELECT p.dialog_id, p.message_id, p.generation, p.aggregate_row_count,
       sd.status, fhe.enabled, m.is_deleted, m.sent_at,
       d.aggregate_generation, d.status, d.next_offset, d.next_attempt_at, d.checked_at
FROM aggregate_page p
LEFT JOIN synced_dialogs sd ON sd.dialog_id=p.dialog_id
LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=p.dialog_id
LEFT JOIN messages m ON m.dialog_id=p.dialog_id AND m.message_id=p.message_id
LEFT JOIN message_reaction_event_status d ON d.dialog_id=p.dialog_id AND d.message_id=p.message_id
ORDER BY p.dialog_id, p.message_id
"""

_REACTION_UPPER_KEY_SQL = """
SELECT dialog_id, message_id
FROM message_reaction_aggregate_state
ORDER BY dialog_id DESC, message_id DESC
LIMIT 1
"""


_READ_AT_CANDIDATES_SQL = """
SELECT m.dialog_id, m.message_id, m.sent_at
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN entities e ON e.id = m.dialog_id
JOIN read_date_expiry_state x ON x.singleton = 1
LEFT JOIN message_read_facts f
  ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id
WHERE sd.status = 'synced'
  AND lower(e.type) = 'user'
  AND m.out = 1
  AND m.is_deleted = 0
  AND sd.read_outbox_max_id IS NOT NULL
  AND m.message_id <= sd.read_outbox_max_id
  AND (x.expired_through_sent_at IS NULL OR m.sent_at > x.expired_through_sent_at)
  AND (f.dialog_id IS NULL OR (f.next_attempt_at IS NOT NULL AND f.next_attempt_at <= ?))
ORDER BY m.sent_at DESC, m.dialog_id, m.message_id
LIMIT ?
"""


_NEXT_REACTION_RELEASE_SQL = """
SELECT MIN(
  CASE
    WHEN d.dialog_id IS NULL OR d.aggregate_generation < a.generation OR d.status = 'stale' THEN 0
    ELSE COALESCE(d.next_attempt_at, ?)
  END
)
FROM message_reaction_aggregate_state a
JOIN synced_dialogs sd ON sd.dialog_id=a.dialog_id AND sd.status='synced'
JOIN full_history_enrollment fhe ON fhe.dialog_id=a.dialog_id AND fhe.enabled=1
LEFT JOIN message_reaction_event_status d ON d.dialog_id=a.dialog_id AND d.message_id=a.message_id
WHERE EXISTS (
    SELECT 1 FROM messages m
    WHERE m.dialog_id=a.dialog_id AND m.message_id=a.message_id AND m.is_deleted=0
  )
  AND (a.aggregate_row_count > 0 OR d.status IN ('partial','unavailable'))
  AND (d.dialog_id IS NULL OR d.aggregate_generation < a.generation
       OR d.status = 'stale'
       OR (d.status IN ('partial','unavailable') AND d.next_attempt_at IS NOT NULL))
"""


_NEXT_READ_AT_RELEASE_SQL = """
SELECT MIN(CASE WHEN f.dialog_id IS NULL THEN 0 ELSE f.next_attempt_at END)
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN entities e ON e.id = m.dialog_id
JOIN read_date_expiry_state x ON x.singleton = 1
LEFT JOIN message_read_facts f
  ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id
WHERE sd.status = 'synced'
  AND lower(e.type) = 'user'
  AND m.out = 1
  AND m.is_deleted = 0
  AND sd.read_outbox_max_id IS NOT NULL
  AND m.message_id <= sd.read_outbox_max_id
  AND (x.expired_through_sent_at IS NULL OR m.sent_at > x.expired_through_sent_at)
  AND (f.dialog_id IS NULL OR f.next_attempt_at IS NOT NULL)
"""


_NEXT_READ_POSITION_RELEASE_SQL = """
SELECT MIN(COALESCE(sd.read_position_next_attempt_at, 0))
FROM synced_dialogs sd
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
LEFT JOIN dialogs d ON d.dialog_id = sd.dialog_id
WHERE sd.status = 'synced'
  AND (
      sd.read_inbox_max_id IS NULL
      OR sd.read_outbox_max_id IS NULL
      OR COALESCE(d.unread_count, 0) > 0
      OR EXISTS (
          SELECT 1
          FROM messages m
          WHERE m.dialog_id = sd.dialog_id
            AND m.is_deleted = 0
            AND m.is_service = 0
            AND m.out = 0
            AND m.message_id > COALESCE(sd.read_inbox_max_id, -1)
      )
  )
"""


@dataclass(frozen=True, slots=True)
class MessageFactRefreshPolicy:
    """Bounded daemon-side policy for optional Telegram fact acquisition."""

    reaction_max_messages_per_cycle: int
    read_at_max_messages_per_cycle: int
    pause_seconds: float
    read_at_ttl_seconds: int
    reaction_detail_max_pages_per_cycle: int = 5
    reaction_detail_cycle_seconds: int = 600


@dataclass(frozen=True, slots=True)
class MessageFactRefreshResult:
    """One background refresh cycle summary."""

    reaction_refreshed: int


@dataclass(frozen=True, slots=True)
class _ReadAtCycleStats:
    first_attempts: int
    retry_attempts: int
    terminal_suppressed: int
    complete: int
    missing: int
    unavailable: int
    reason_counts: dict[str, int]
    rpc_attempts: int
    message_too_old_responses: int
    invalid_cutoff_witnesses: int
    locally_classified: int
    cutoff_backlog_suppressed: int
    cutoff_skipped: int
    fresh_skipped: int
    candidate_count: int
    measurement_complete: bool


@dataclass(frozen=True, slots=True)
class MessageFactRefreshDeps:
    """Infrastructure dependencies for one optional fact refresh cycle."""

    conn: sqlite3.Connection
    reaction_detail_refresher: ReactionDetailRefresher
    read_receipt_gateway: TelegramReadReceiptGateway
    read_at_observer: Callable[[Mapping[str, object]], None] | None = None
    clock: Callable[[], float] = time.time


def _next_release_at(conn: sqlite3.Connection, query: str, params: tuple[int, ...] = ()) -> float | None:
    row = cast(tuple[object] | None, conn.execute(query, params).fetchone())
    value = None if row is None else row[0]
    return None if value is None else float(cast(int | float, value))


def _reaction_pacing_release_at(conn: sqlite3.Connection) -> float | None:
    row = cast(
        tuple[object] | None,
        conn.execute("SELECT release_at FROM reaction_detail_pacing_state WHERE singleton=1").fetchone(),
    )
    return None if row is None else float(cast(int | float, row[0]))


def _reaction_upper_key(conn: sqlite3.Connection) -> tuple[int, int] | None:
    row = cast(tuple[object, object] | None, conn.execute(_REACTION_UPPER_KEY_SQL).fetchone())
    return None if row is None else (int(cast(int, row[0])), int(cast(int, row[1])))


def _reaction_discovery_page(
    conn: sqlite3.Connection,
    cursor: tuple[int, int] | None,
    upper: tuple[int, int],
) -> list[tuple[object, ...]]:
    keyset = "WHERE (dialog_id,message_id) <= (?,?)"
    params: tuple[object, ...] = (*upper, _REACTION_DISCOVERY_PAGE_SIZE)
    if cursor is not None:
        keyset = "WHERE (dialog_id,message_id) > (?,?) AND (dialog_id,message_id) <= (?,?)"
        params = (*cursor, *upper, _REACTION_DISCOVERY_PAGE_SIZE)
    query = _REACTION_DISCOVERY_PAGE_SQL.format(keyset=keyset)
    return cast(list[tuple[object, ...]], conn.execute(query, params).fetchall())


def _reaction_page_identity(
    row: tuple[object, ...],
) -> tuple[int, int, int, int, str | None, int, object, int] | None:
    dialog_id, message_id, generation, aggregate_count = (int(cast(int, row[i])) for i in range(4))
    if row[4] != "synced" or row[5] != 1 or row[6] != 0 or row[7] is None:
        return None
    detail_status = None if row[9] is None else str(row[9])
    if aggregate_count <= 0 and detail_status not in {"partial", "unavailable"}:
        return None
    sent_at = int(cast(int, row[7]))
    checked_at = 0 if row[12] is None else int(cast(int, row[12]))
    return dialog_id, message_id, generation, aggregate_count, detail_status, sent_at, row[10], checked_at


def _reaction_detail_is_stale(row: tuple[object, ...], generation: int) -> bool:
    detail_generation = None if row[8] is None else int(cast(int, row[8]))
    detail_status = None if row[9] is None else str(row[9])
    return (
        detail_status is None or detail_generation is None or detail_generation < generation or detail_status == "stale"
    )


def _reaction_detail_release(
    row: tuple[object, ...], generation: int, now: float
) -> tuple[float, bool, bool, int | None] | None:
    detail_status = None if row[9] is None else str(row[9])
    stale = _reaction_detail_is_stale(row, generation)
    next_attempt = None if row[11] is None else int(cast(int, row[11]))
    if not stale and (detail_status not in {"partial", "unavailable"} or next_attempt is None):
        return None
    release_at = 0.0 if stale else float(next_attempt or 0)
    return release_at, release_at <= now, stale, next_attempt


def _ranked_reaction_candidate(
    identity: tuple[int, int, int, int, str | None, int, object, int],
    *,
    stale: bool,
    next_attempt: int | None,
) -> _ReactionRankedCandidate:
    dialog_id, message_id, generation, _, detail_status, sent_at, offset_value, checked_at = identity
    offset = None if offset_value is None else str(offset_value)
    partial_first = detail_status == "partial" and offset is not None
    priority = (
        0 if partial_first else 1 if stale else 2,
        0 if stale else next_attempt or 0,
        checked_at,
        -sent_at,
        dialog_id,
        message_id,
    )
    return priority, (dialog_id, message_id, generation, offset)


def _reaction_page_candidate(
    row: tuple[object, ...], now: float
) -> tuple[float, _ReactionRankedCandidate | None] | None:
    identity = _reaction_page_identity(row)
    if identity is None:
        return None
    release = _reaction_detail_release(row, identity[2], now)
    if release is None:
        return None
    release_at, due, stale, next_attempt = release
    if not due:
        return release_at, None
    return release_at, _ranked_reaction_candidate(identity, stale=stale, next_attempt=next_attempt)


def _merge_reaction_page(
    rows: Sequence[tuple[object, ...]],
    candidates: list[_ReactionRankedCandidate],
    *,
    now: float,
    limit: int,
    release_at: float | None,
) -> tuple[list[_ReactionRankedCandidate], float | None]:
    for row in rows:
        entry = _reaction_page_candidate(row, now)
        if entry is None:
            continue
        row_release, candidate = entry
        release_at = row_release if release_at is None else min(release_at, row_release)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda item: item[0])
    return candidates[:limit], release_at


def _reaction_release_at(conn: sqlite3.Connection, now: float) -> float | None:
    """Combine raw reaction due state with the durable pacing window."""
    pacing_release = _reaction_pacing_release_at(conn)
    if pacing_release is not None and pacing_release > now:
        return pacing_release
    raw_release = _next_release_at(conn, _NEXT_REACTION_RELEASE_SQL, (0,))
    if raw_release is None:
        return None
    return raw_release if pacing_release is None else max(raw_release, pacing_release)


class MessageFactRefreshDemandAdapter(DurableDemandAdapter):
    """Read the durable optional-fact candidate state.

    This is the durable orchestration root for background per-message reaction
    and exact read-date candidates. The nested acquisition adapters do not
    report these same rows independently.
    """

    demand_kind = DemandKind.MESSAGE_FACT_REFRESH

    def __init__(
        self,
        deps: MessageFactRefreshDeps,
        policy: MessageFactRefreshPolicy,
        *,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._deps = deps
        self._policy = policy
        self._shutdown_event = shutdown_event
        self._reaction_upper: tuple[int, int] | None = None
        self._reaction_cursor: tuple[int, int] | None = None
        self._reaction_sweep_started = False
        self._reaction_sweep_complete = False
        self._reaction_next_scan_at: float | None = None
        self._reaction_release_at: float | None = None
        self._reaction_candidates: list[_ReactionRankedCandidate] = []
        self._read_at_release_at: float | None = None
        self._read_at_checked_at: float | None = None
        self._reaction_candidate_limit = min(
            policy.reaction_max_messages_per_cycle, policy.reaction_detail_max_pages_per_cycle
        )

    def status(self, now: float) -> DemandStatus | None:
        """Return the first missing or TTL-expired candidate release boundary."""
        releases: list[float] = []
        if self._policy.reaction_max_messages_per_cycle > 0:
            reaction_release = self._reaction_status_release(now)
            if reaction_release is not None:
                releases.append(reaction_release)
        if self._policy.read_at_max_messages_per_cycle > 0 and (
            self._read_at_checked_at is None or now - self._read_at_checked_at >= _REACTION_DISCOVERY_REFRESH_SECONDS
        ):
            self._read_at_release_at = _next_release_at(
                self._deps.conn,
                _NEXT_READ_AT_RELEASE_SQL,
                (),
            )
            self._read_at_checked_at = self._deps.clock()
        if self._policy.read_at_max_messages_per_cycle > 0 and self._read_at_release_at is not None:
            releases.append(self._read_at_release_at)
        if not releases:
            return None
        return DemandStatus(release_at=min(releases))

    def _reaction_status_release(self, now: float) -> float | None:
        pacing_release = _reaction_pacing_release_at(self._deps.conn)
        if pacing_release is not None and pacing_release > now:
            return pacing_release
        if not self._reaction_sweep_complete:
            return now
        if self._reaction_candidates:
            return now
        next_scan = self._reaction_next_scan_at or now
        if self._reaction_release_at is not None:
            next_scan = min(next_scan, self._reaction_release_at)
        if now < next_scan:
            return next_scan
        return now

    def _reset_reaction_sweep(self) -> None:
        self._reaction_upper = None
        self._reaction_cursor = None
        self._reaction_sweep_started = False
        self._reaction_sweep_complete = False
        self._reaction_next_scan_at = None
        self._reaction_release_at = None
        self._reaction_candidates = []

    def _restart_empty_sweep_if_due(self, now: float) -> None:
        restart_at = self._reaction_next_scan_at
        if self._reaction_release_at is not None:
            restart_at = self._reaction_release_at if restart_at is None else min(restart_at, self._reaction_release_at)
        if (
            self._reaction_sweep_complete
            and not self._reaction_candidates
            and restart_at is not None
            and restart_at <= now
        ):
            self._reset_reaction_sweep()

    def _advance_reaction_sweep(self, now: float) -> bool:
        if not self._reaction_sweep_started:
            self._reaction_upper = _reaction_upper_key(self._deps.conn)
            self._reaction_sweep_started = True
            if self._reaction_upper is None:
                self._finish_reaction_sweep(now)
                return True
        upper = self._reaction_upper
        assert upper is not None
        rows = _reaction_discovery_page(self._deps.conn, self._reaction_cursor, upper)
        if rows:
            last = rows[-1]
            self._reaction_cursor = (int(cast(int, last[0])), int(cast(int, last[1])))
            self._reaction_candidates, self._reaction_release_at = _merge_reaction_page(
                rows,
                self._reaction_candidates,
                now=now,
                limit=self._reaction_candidate_limit,
                release_at=self._reaction_release_at,
            )
        if len(rows) < _REACTION_DISCOVERY_PAGE_SIZE:
            self._finish_reaction_sweep(now)
        return self._reaction_sweep_complete

    def _reaction_candidates_for_slice(self, now: float) -> Sequence[_ReactionCandidate] | None:
        if self._policy.reaction_max_messages_per_cycle <= 0:
            return None
        pacing_release = _reaction_pacing_release_at(self._deps.conn)
        if pacing_release is not None and pacing_release > now:
            return ()
        if self._reaction_sweep_complete:
            return tuple(item[1] for item in self._reaction_candidates)
        if self._advance_reaction_sweep(now):
            return tuple(item[1] for item in self._reaction_candidates)
        return ()

    def _finish_reaction_sweep(self, now: float) -> None:
        self._reaction_sweep_complete = True
        del now
        self._reaction_next_scan_at = self._deps.clock() + _REACTION_DISCOVERY_REFRESH_SECONDS

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one bounded candidate cycle under its registered root."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = self._deps.clock()
        status = self.status(now)
        if status is None or not status.is_ready(now):
            return
        self._restart_empty_sweep_if_due(now)
        reaction_candidates = self._reaction_candidates_for_slice(now)
        read_at_due = self._read_at_release_at is not None and self._read_at_release_at <= now
        if not reaction_candidates and not read_at_due:
            return
        if read_at_due:
            self._read_at_checked_at = None
        with demand_context(DemandKind.MESSAGE_FACT_REFRESH):
            with rpc_attempt_budget(budget):
                try:
                    await refresh_message_facts_once(
                        self._deps,
                        self._policy,
                        read_at_due=read_at_due,
                        reaction_candidates=reaction_candidates,
                        shutdown_event=self._shutdown_event,
                    )
                except RpcAttemptBudgetExhaustedError:
                    return
                finally:
                    if reaction_candidates:
                        self._reset_reaction_sweep()


class ReadReceiptDemandAdapter(DurableDemandAdapter):
    """Read the durable read-position reconciliation state.

    Per-message exact read dates are nested acquisitions selected by
    ``MESSAGE_FACT_REFRESH`` and are intentionally absent from this status.
    """

    demand_kind = DemandKind.READ_RECEIPT_BATCH

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_batch: Callable[[], Awaitable[object]],
    ) -> None:
        self._conn = conn
        self._run_batch = run_batch

    def status(self, now: float) -> DemandStatus | None:
        """Return the earliest due read-position batch without changing state."""
        del now
        row = cast(
            tuple[object] | None,
            self._conn.execute(_NEXT_READ_POSITION_RELEASE_SQL).fetchone(),
        )
        release_at = None if row is None else row[0]
        if release_at is None:
            return None
        return DemandStatus(release_at=float(cast(int | float, release_at)))

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one existing read-position batch under the transport budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        status = self.status(time.time())
        if status is None or not status.is_ready(time.time()):
            return
        with demand_context(DemandKind.READ_RECEIPT_BATCH):
            with acquisition_context(AcquisitionKind.READ_RECEIPT_SNAPSHOT):
                with rpc_attempt_budget(budget):
                    try:
                        await self._run_batch()
                    except RpcAttemptBudgetExhaustedError:
                        return


def _row_ints(row: Sequence[object]) -> tuple[int, ...]:
    return tuple(int(cast(int | str, value)) for value in row)


def _reaction_candidates_query(*, keys: Sequence[tuple[int, int]] | None = None) -> tuple[str, tuple[int, ...]]:
    if not keys:
        return _REACTION_CANDIDATES_SQL.format(requested_cte="", requested_filter=""), ()
    values = ",".join("(?,?)" for _ in keys)
    params = tuple(value for key in keys for value in key)
    return (
        _REACTION_CANDIDATES_SQL.format(
            requested_cte=f"WITH requested(dialog_id,message_id) AS (VALUES {values})",
            requested_filter="AND (m.dialog_id,m.message_id) IN (SELECT dialog_id,message_id FROM requested)",
        ),
        params,
    )


def _reaction_candidates(
    conn: sqlite3.Connection,
    *,
    stale_before_utc: int,
    limit: int,
) -> list[tuple[int, int, int, str | None]]:
    query, key_params = _reaction_candidates_query()
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(query, (*key_params, stale_before_utc, limit)).fetchall(),
    )
    return [
        (
            int(cast(int, row[0])),
            int(cast(int, row[1])),
            int(cast(int, row[2])),
            None if row[3] is None else str(row[3]),
        )
        for row in rows
    ]


def _revalidate_reaction_candidates(
    conn: sqlite3.Connection,
    candidates: Sequence[tuple[int, int, int, str | None]],
    *,
    now: int,
    limit: int,
) -> list[tuple[int, int, int, str | None]]:
    if not candidates or limit <= 0:
        return []
    keys = [(dialog_id, message_id) for dialog_id, message_id, _, _ in candidates]
    query, key_params = _reaction_candidates_query(keys=keys)
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(query, (*key_params, now, limit)).fetchall(),
    )
    return [
        (
            int(cast(int, row[0])),
            int(cast(int, row[1])),
            int(cast(int, row[2])),
            None if row[3] is None else str(row[3]),
        )
        for row in rows
    ]


def _reserve_reaction_candidates(  # noqa: PLR0913 - explicit claim transaction inputs
    conn: sqlite3.Connection,
    *,
    now: int,
    cycle_seconds: int,
    limit: int,
    candidates: Sequence[tuple[int, int, int, str | None]] | None,
    selected: list[tuple[int, int, int, str | None]],
) -> list[tuple[int, int, int, str | None]]:
    with write_transaction(conn):
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT release_at, claimed_pages FROM reaction_detail_pacing_state WHERE singleton=1"
            ).fetchone(),
        )
        if row is not None and now < int(cast(int | str, row[0])):
            return []
        selected = _revalidate_reaction_candidates(
            conn, candidates if candidates is not None else selected, now=now, limit=limit
        )
        if not selected:
            return []
        release_at = now + cycle_seconds
        conn.execute(
            "INSERT INTO reaction_detail_pacing_state "
            "(singleton, window_started_at, release_at, claimed_pages, started_pages) "
            "VALUES (1, ?, ?, ?, 0) "
            "ON CONFLICT(singleton) DO UPDATE SET window_started_at=excluded.window_started_at, "
            "release_at=excluded.release_at, claimed_pages=excluded.claimed_pages, started_pages=0",
            (now, release_at, len(selected)),
        )
        return selected


def _claim_reaction_pages(  # noqa: PLR0913 - explicit transactional inputs
    conn: sqlite3.Connection,
    *,
    now: int,
    max_pages: int,
    cycle_seconds: int,
    candidate_limit: int,
    candidates: Sequence[tuple[int, int, int, str | None]] | None = None,
) -> list[tuple[int, int, int, str | None]]:
    """Reserve one pacing window and its candidate pages transactionally."""
    if max_pages <= 0 or candidate_limit <= 0:
        return []
    if conn.in_transaction:
        raise RuntimeError("reaction page claims require an idle connection")
    state = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT release_at, claimed_pages FROM reaction_detail_pacing_state WHERE singleton=1").fetchone(),
    )
    if state is not None and now < int(cast(int | str, state[0])):
        return []
    limit = min(candidate_limit, max_pages)
    if candidates is None:
        selected = _reaction_candidates(conn, stale_before_utc=now, limit=limit)
    elif not candidates:
        return []
    else:
        selected = []
    if candidates is None and not selected:
        return []
    return _reserve_reaction_candidates(
        conn,
        now=now,
        cycle_seconds=cycle_seconds,
        limit=limit,
        candidates=candidates,
        selected=selected,
    )


def _start_reaction_page(conn: sqlite3.Connection) -> bool:
    """Consume a previously claimed page before making its Telegram call."""
    with write_transaction(conn):
        cursor = conn.execute(
            "UPDATE reaction_detail_pacing_state SET started_pages=started_pages+1 "
            "WHERE singleton=1 AND started_pages < claimed_pages"
        )
        return cursor.rowcount == 1


def _extend_reaction_release(conn: sqlite3.Connection, *, now: int, retry_after: int) -> None:
    """Extend the durable window after a FloodWait without reopening it."""
    if retry_after <= 0:
        return
    with write_transaction(conn):
        conn.execute(
            "UPDATE reaction_detail_pacing_state SET release_at=MAX(release_at, ?) WHERE singleton=1",
            (now + retry_after,),
        )


def _read_at_candidates(
    conn: sqlite3.Connection,
    *,
    stale_before_utc: int,
    limit: int,
) -> list[ReadMessage]:
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(_READ_AT_CANDIDATES_SQL, (stale_before_utc, limit)).fetchall(),
    )
    return [
        ReadMessage(message_id=message_id, sent_at=sent_at, dialog_id=dialog_id, out=1)
        for dialog_id, message_id, sent_at in (_row_ints(row) for row in rows)
    ]


def _terminal_read_at_suppressed(conn: sqlite3.Connection) -> int:
    """Count every eligible terminal outcome omitted from acquisition forever."""
    query = (
        "SELECT COUNT(*) FROM messages m "
        "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id "
        "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
        "JOIN entities e ON e.id = m.dialog_id "
        "JOIN message_read_facts f ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id "
        "WHERE sd.status = 'synced' AND lower(e.type) = 'user' AND m.out = 1 "
        "AND m.is_deleted = 0 "
        "AND sd.read_outbox_max_id IS NOT NULL AND m.message_id <= sd.read_outbox_max_id "
        "AND f.next_attempt_at IS NULL "
        "AND f.reason IN ('resolved', 'message_too_old', 'privacy_restricted', "
        "'not_mutual_contact', 'invalid_target', 'access_lost')"
    )
    row = cast(tuple[object] | None, conn.execute(query).fetchone())
    return 0 if row is None else int(cast(int | str, row[0]))


def _cutoff_backlog_suppressed(conn: sqlite3.Connection, checked_at: int) -> int:
    """Count due candidates omitted because their Telegram sent date expired."""
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT COUNT(*) FROM messages m "
            "JOIN synced_dialogs sd ON sd.dialog_id=m.dialog_id "
            "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
            "JOIN entities e ON e.id=m.dialog_id "
            "JOIN read_date_expiry_state x ON x.singleton=1 "
            "LEFT JOIN message_read_facts f ON f.dialog_id=m.dialog_id AND f.message_id=m.message_id "
            "WHERE sd.status='synced' AND lower(e.type)='user' AND m.out=1 AND m.is_deleted=0 "
            "AND sd.read_outbox_max_id IS NOT NULL AND m.message_id <= sd.read_outbox_max_id "
            "AND x.expired_through_sent_at IS NOT NULL AND m.sent_at <= x.expired_through_sent_at "
            "AND (f.dialog_id IS NULL OR (f.next_attempt_at IS NOT NULL AND f.next_attempt_at <= ?))",
            (checked_at,),
        ).fetchone(),
    )
    return 0 if row is None else int(cast(int | str, row[0]))


def _observe_read_at_cycle(  # noqa: PLR0913 - bounded telemetry fields are explicit
    deps: MessageFactRefreshDeps,
    *,
    first_attempts: int,
    retry_attempts: int,
    terminal_suppressed: int,
    complete: int,
    missing: int,
    unavailable: int,
    reason_counts: Mapping[str, int],
    rpc_attempts: int,
    message_too_old_responses: int,
    invalid_cutoff_witnesses: int,
    locally_classified: int,
    cutoff_backlog_suppressed: int,
    cutoff_skipped: int,
    fresh_skipped: int,
    candidate_count: int,
) -> None:
    """Publish one bounded, content-free observation after a complete cycle."""
    observer = deps.read_at_observer
    if observer is None:
        return
    try:
        # invalid_cutoff_witnesses is a subset of age responses quarantined
        # locally; locally_classified counts only propagation to other rows.
        observer(
            {
                "first_attempts": first_attempts,
                "retry_attempts": retry_attempts,
                "terminal_suppressed": terminal_suppressed,
                "complete": complete,
                "missing": missing,
                "unavailable": unavailable,
                "reason_counts": dict(reason_counts),
                "rpc_attempts": rpc_attempts,
                "message_too_old_responses": message_too_old_responses,
                "invalid_cutoff_witnesses": invalid_cutoff_witnesses,
                "locally_classified": locally_classified,
                "cutoff_backlog_suppressed": cutoff_backlog_suppressed,
                "cutoff_skipped": cutoff_skipped,
                "fresh_skipped": fresh_skipped,
                "candidate_count": candidate_count,
                "measurement_complete": True,
            }
        )
    except Exception:  # noqa: BLE001 - telemetry must not affect fact persistence
        # Telemetry is best effort and must not affect fact persistence.
        return


async def _interruptible_pause(shutdown_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def _merge_read_at_counts(
    counts: list[int],
    complete: int,
    missing: int,
    unavailable: int,
) -> tuple[int, int, int]:
    return (
        complete + counts[0],
        missing + counts[1],
        unavailable + counts[2],
    )


async def _refresh_read_at_cycle(
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    checked_at: int,
    shutdown_event: asyncio.Event | None,
) -> _ReadAtCycleStats:
    if shutdown_event is not None and shutdown_event.is_set():
        return _ReadAtCycleStats(
            first_attempts=0,
            retry_attempts=0,
            terminal_suppressed=0,
            complete=0,
            missing=0,
            unavailable=0,
            reason_counts={},
            rpc_attempts=0,
            message_too_old_responses=0,
            invalid_cutoff_witnesses=0,
            locally_classified=0,
            cutoff_backlog_suppressed=0,
            cutoff_skipped=0,
            fresh_skipped=0,
            candidate_count=0,
            measurement_complete=False,
        )
    messages = _read_at_candidates(
        deps.conn,
        stale_before_utc=checked_at,
        limit=policy.read_at_max_messages_per_cycle,
    )
    terminal_suppressed = _terminal_read_at_suppressed(deps.conn)
    cycle_metrics = {
        "first_attempts": 0,
        "retry_attempts": 0,
        "rpc_attempts": 0,
        "message_too_old_responses": 0,
        "invalid_cutoff_witnesses": 0,
        "locally_classified": 0,
        "cutoff_backlog_suppressed": _cutoff_backlog_suppressed(deps.conn, checked_at),
        "cutoff_skipped": 0,
        "fresh_skipped": 0,
    }
    complete = missing = unavailable = 0
    measurement_complete = True
    reason_counts = {reason.value: 0 for reason in READ_DATE_REASONS}
    grouped_by_dialog: dict[int, list[ReadMessage]] = {}
    for message in messages:
        grouped_by_dialog.setdefault(message.dialog_id, []).append(message)
    grouped_messages = list(grouped_by_dialog.values())
    for index, grouped in enumerate(grouped_messages):
        counts: list[int] = []
        await enrich_read_at(
            deps.conn,
            deps.read_receipt_gateway,
            grouped[0].dialog_id,
            grouped,
            dialog_type="user",
            read_at_ttl_seconds=policy.read_at_ttl_seconds,
            checked_at=checked_at,
            cycle_counts=counts,
            cycle_reason_counts=reason_counts,
            cycle_metrics=cycle_metrics,
        )
        complete, missing, unavailable = _merge_read_at_counts(
            counts,
            complete,
            missing,
            unavailable,
        )
        if shutdown_event is not None and shutdown_event.is_set():
            measurement_complete = False
            break
        if shutdown_event is not None and index < len(grouped_messages) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)
    return _ReadAtCycleStats(
        first_attempts=cycle_metrics["first_attempts"],
        retry_attempts=cycle_metrics["retry_attempts"],
        terminal_suppressed=terminal_suppressed,
        complete=complete,
        missing=missing,
        unavailable=unavailable,
        reason_counts=reason_counts,
        rpc_attempts=cycle_metrics["rpc_attempts"],
        message_too_old_responses=cycle_metrics["message_too_old_responses"],
        invalid_cutoff_witnesses=cycle_metrics["invalid_cutoff_witnesses"],
        locally_classified=cycle_metrics["locally_classified"],
        cutoff_backlog_suppressed=cycle_metrics["cutoff_backlog_suppressed"],
        cutoff_skipped=cycle_metrics["cutoff_skipped"],
        fresh_skipped=cycle_metrics["fresh_skipped"],
        candidate_count=len(messages),
        measurement_complete=measurement_complete,
    )


async def _refresh_one_reaction_page(
    deps: MessageFactRefreshDeps,
    row: tuple[int, int, int, str | None],
    *,
    checked_at: int,
    shutdown_event: asyncio.Event | None,
) -> tuple[int, bool]:
    dialog_id, message_id, generation, offset = row
    if not _start_reaction_page(deps.conn):
        return 0, True
    detail = await deps.reaction_detail_refresher.refresh_one(
        dialog_id,
        message_id,
        generation,
        entity=dialog_id,
        offset=offset,
        cancellation_event=shutdown_event,
        now=checked_at,
    )
    if detail.retry_after is not None and detail.stop_cycle:
        _extend_reaction_release(deps.conn, now=int(deps.clock()), retry_after=detail.retry_after)
    return detail.fetched_pages, detail.stop_cycle or detail.status == "cancelled"


async def _refresh_reaction_pages(  # noqa: PLR0913 - keeps transaction and demand inputs explicit
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    checked_at: int,
    claim_at: int,
    candidates: Sequence[_ReactionCandidate] | None,
    shutdown_event: asyncio.Event | None,
) -> int:
    if shutdown_event is not None and shutdown_event.is_set():
        return 0
    selected_reaction_rows = _claim_reaction_pages(
        deps.conn,
        now=claim_at,
        max_pages=policy.reaction_detail_max_pages_per_cycle,
        cycle_seconds=policy.reaction_detail_cycle_seconds,
        candidate_limit=policy.reaction_max_messages_per_cycle,
        candidates=candidates,
    )
    reaction_refreshed = 0
    for index, row in enumerate(selected_reaction_rows):
        fetched_pages, stop_page = await _refresh_one_reaction_page(
            deps,
            row,
            checked_at=checked_at,
            shutdown_event=shutdown_event,
        )
        reaction_refreshed += fetched_pages
        if stop_page:
            break
        if shutdown_event is not None and index < len(selected_reaction_rows) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)
    return reaction_refreshed


async def refresh_message_facts_once(  # noqa: PLR0913 - public orchestration inputs are explicit
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    now: int | None = None,
    read_at_due: bool = True,
    reaction_candidates: Sequence[_ReactionCandidate] | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> MessageFactRefreshResult:
    """Refresh a bounded batch of optional message facts into SQLite."""
    if policy.reaction_max_messages_per_cycle <= 0 and policy.read_at_max_messages_per_cycle <= 0:
        return MessageFactRefreshResult(reaction_refreshed=0)

    checked_at = int(time.time() if now is None else now)
    if policy.read_at_max_messages_per_cycle > 0 and read_at_due:
        stats = await _refresh_read_at_cycle(
            deps,
            policy,
            checked_at=checked_at,
            shutdown_event=shutdown_event,
        )
    else:
        stats = None

    claim_at = checked_at if now is not None else int(deps.clock())
    reaction_refreshed = await _refresh_reaction_pages(
        deps,
        policy,
        checked_at=checked_at,
        claim_at=claim_at,
        candidates=reaction_candidates,
        shutdown_event=shutdown_event,
    )

    if stats is not None and stats.measurement_complete:
        _observe_read_at_cycle(
            deps,
            first_attempts=stats.first_attempts,
            retry_attempts=stats.retry_attempts,
            terminal_suppressed=stats.terminal_suppressed,
            complete=stats.complete,
            missing=stats.missing,
            unavailable=stats.unavailable,
            reason_counts=stats.reason_counts,
            rpc_attempts=stats.rpc_attempts,
            message_too_old_responses=stats.message_too_old_responses,
            invalid_cutoff_witnesses=stats.invalid_cutoff_witnesses,
            locally_classified=stats.locally_classified,
            cutoff_backlog_suppressed=stats.cutoff_backlog_suppressed,
            cutoff_skipped=stats.cutoff_skipped,
            fresh_skipped=stats.fresh_skipped,
            candidate_count=stats.candidate_count,
        )

    return MessageFactRefreshResult(
        reaction_refreshed=reaction_refreshed,
    )
