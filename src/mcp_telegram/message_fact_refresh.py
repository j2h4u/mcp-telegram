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
from dataclasses import dataclass, field
from typing import cast

from .models import ReadMessage
from .reactions import ReactionDetailRefresher
from .sqlite_writer import SQLiteWriter
from .sync_transactions import write_transaction
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    acquisition_context,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_fact_queries import enrich_read_at
from .telegram_read_receipts import READ_AT_REQUEST_METHOD, READ_RECEIPT_REQUEST_METHOD
from .telegram_reading import READ_DATE_REASONS, TelegramReadReceiptGateway
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import rpc_attempt_budget

_ReactionCandidate = tuple[int, int, int, str | None]

_REACTION_CANDIDATES_SQL = """
{requested_cte}
SELECT m.dialog_id, m.message_id, a.generation, d.next_offset
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN message_reaction_aggregate_state a ON a.dialog_id=m.dialog_id AND a.message_id=m.message_id
LEFT JOIN message_reaction_event_status d ON d.dialog_id=m.dialog_id AND d.message_id=m.message_id
WHERE sd.status = 'synced'
  AND a.can_see_list IS NOT 0
  AND NOT EXISTS (
      SELECT 1 FROM dialogs directory
      WHERE directory.dialog_id=m.dialog_id AND directory.type='channel'
  )
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

# ponytail: New read-at facts may take up to 60 seconds to become visible to scheduling.
_REACTION_DISCOVERY_REFRESH_SECONDS = 60
_REACTION_DISCOVERY_PAGE_SIZE = 256
_REACTION_DISCOVERY_PAGE_SQL = """
WITH aggregate_page AS MATERIALIZED (
  SELECT dialog_id, message_id, generation, aggregate_row_count, can_see_list
  FROM message_reaction_aggregate_state
  {keyset}
  ORDER BY dialog_id, message_id
  LIMIT ?
)
SELECT p.dialog_id, p.message_id, p.generation, p.aggregate_row_count,
       sd.status, fhe.enabled, m.is_deleted, m.sent_at,
       d.aggregate_generation, d.status, d.next_offset, d.next_attempt_at, d.checked_at, p.can_see_list
FROM aggregate_page p
LEFT JOIN synced_dialogs sd ON sd.dialog_id=p.dialog_id
  AND NOT EXISTS (
      SELECT 1 FROM dialogs directory
      WHERE directory.dialog_id=p.dialog_id AND directory.type='channel'
  )
LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=p.dialog_id
LEFT JOIN messages m ON m.dialog_id=p.dialog_id AND m.message_id=p.message_id
LEFT JOIN message_reaction_event_status d ON d.dialog_id=p.dialog_id AND d.message_id=p.message_id
ORDER BY p.dialog_id, p.message_id
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
  AND NOT EXISTS (
      SELECT 1 FROM dialogs directory
      WHERE directory.dialog_id=a.dialog_id AND directory.type='channel'
  )
JOIN full_history_enrollment fhe ON fhe.dialog_id=a.dialog_id AND fhe.enabled=1
LEFT JOIN message_reaction_event_status d ON d.dialog_id=a.dialog_id AND d.message_id=a.message_id
WHERE EXISTS (
    SELECT 1 FROM messages m
    WHERE m.dialog_id=a.dialog_id AND m.message_id=a.message_id AND m.is_deleted=0
  )
  AND a.can_see_list IS NOT 0
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
    writer: SQLiteWriter = field(kw_only=True)
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


def _reaction_dialog_is_eligible(row: tuple[object, ...]) -> bool:
    return row[4] == "synced" and row[5] == 1 and row[6] == 0 and row[7] is not None and row[13] != 0


def _reaction_page_identity(
    row: tuple[object, ...],
) -> tuple[int, int, int, int, str | None, int, object, int] | None:
    dialog_id, message_id, generation, aggregate_count = (int(cast(int, row[i])) for i in range(4))
    if not _reaction_dialog_is_eligible(row):
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
) -> tuple[tuple[int, int, int, int, int, int], _ReactionCandidate]:
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
) -> tuple[float, tuple[tuple[int, int, int, int, int, int], _ReactionCandidate] | None] | None:
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
        account_id: int,
        shutdown_event: asyncio.Event | None = None,
    ) -> None:
        self._deps = deps
        self._policy = policy
        self._shutdown_event = shutdown_event
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id
        self._read_at_release_at: float | None = None
        self._read_at_checked_at: float | None = None
        self._reaction_candidate_limit = min(
            policy.reaction_max_messages_per_cycle, policy.reaction_detail_max_pages_per_cycle
        )

    def _selected(
        self, now: float, excluded: frozenset[WorkRevision] = frozenset(), *, target: str | None = None
    ) -> tuple[WorkRevision, str, _ReactionCandidate | ReadMessage] | None:
        if target is not None:
            if target.startswith("reaction:"):
                return self._reaction_selection(now, excluded, target=target)[0]
            if target.startswith("read_at:"):
                return self._read_at_selection(now, excluded, target=target)
            return None
        selected, fallback = self._reaction_selection(now, excluded, target=target)
        return selected or self._read_at_selection(now, excluded, target=target) or fallback

    def _reaction_selection(
        self, now: float, excluded: frozenset[WorkRevision], *, target: str | None = None
    ) -> tuple[
        tuple[WorkRevision, str, _ReactionCandidate | ReadMessage] | None,
        tuple[WorkRevision, str, _ReactionCandidate | ReadMessage] | None,
    ]:
        future_reaction: tuple[WorkRevision, str, _ReactionCandidate | ReadMessage] | None = None
        pacing_release = _reaction_pacing_release_at(self._deps.conn)
        limit = max(
            5, self._reaction_candidate_limit, _excluded_sibling_count(excluded, self.demand_kind, self._account_id) + 1
        )
        candidates = _reaction_candidates(
            self._deps.conn,
            stale_before_utc=2**63 - 1,
            limit=limit,
            keys=None if target is None else [_parse_work_target(target, "reaction")],
        )
        for dialog_id, message_id, generation, offset in candidates:
            selected, delayed = self._reaction_candidate_work(
                (dialog_id, message_id, generation, offset),
                now=now,
                pacing_release=pacing_release,
                excluded=excluded,
            )
            if selected is not None:
                return selected, future_reaction
            if delayed and future_reaction is None:
                future_reaction = self._revisioned_reaction_candidate(dialog_id, message_id, generation, offset)
        return None, future_reaction

    def _revisioned_reaction_candidate(
        self, dialog_id: int, message_id: int, generation: int, offset: str | None
    ) -> tuple[WorkRevision, str, _ReactionCandidate]:
        selected = WorkRevision(
            WorkKey(self.demand_kind, self._account_id, f"reaction:{dialog_id}:{message_id}"),
            repr((generation, offset)),
        )
        return selected, "reaction", (dialog_id, message_id, generation, offset)

    def _reaction_candidate_work(
        self,
        candidate: _ReactionCandidate,
        *,
        now: float,
        pacing_release: float | None,
        excluded: frozenset[WorkRevision],
    ) -> tuple[tuple[WorkRevision, str, _ReactionCandidate] | None, bool]:
        dialog_id, message_id = candidate[:2]
        state = cast(
            tuple[object, ...] | None,
            self._deps.conn.execute(
                "SELECT next_attempt_at FROM message_reaction_event_status WHERE dialog_id=? AND message_id=?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        retry_at = None if state is None or state[0] is None else float(cast(int | float, state[0]))
        delayed = (pacing_release is not None and pacing_release > now) or (retry_at is not None and retry_at > now)
        selected = self._revisioned_reaction_candidate(*candidate)
        available = selected[0] not in excluded
        return (selected if not delayed and available else None), delayed and available

    def _read_at_selection(
        self, now: float, excluded: frozenset[WorkRevision], *, target: str | None = None
    ) -> tuple[WorkRevision, str, _ReactionCandidate | ReadMessage] | None:
        read_at = _read_at_candidates(
            self._deps.conn,
            stale_before_utc=2**63 - 1 if target is not None else int(now),
            limit=max(
                5,
                self._policy.read_at_max_messages_per_cycle,
                _excluded_sibling_count(excluded, self.demand_kind, self._account_id) + 1,
            ),
            target=None if target is None else _parse_work_target(target, "read_at"),
        )
        for message in read_at:
            row = cast(
                tuple[object] | None,
                self._deps.conn.execute(
                    "SELECT status, read_at, reason FROM message_read_facts WHERE dialog_id=? AND message_id=?",
                    (message.dialog_id, message.message_id),
                ).fetchone(),
            )
            selected = WorkRevision(
                WorkKey(self.demand_kind, self._account_id, f"read_at:{message.dialog_id}:{message.message_id}"),
                repr(None if row is None else tuple(row)),
            )
            if selected not in excluded:
                return selected, "read_at", message
        return None

    def status(
        self,
        now: float,
        *,
        excluded: frozenset[WorkRevision] = frozenset(),
        target: str | None = None,
    ) -> DemandStatus | None:
        """Return the first missing or TTL-expired candidate release boundary."""
        selected_item = self._selected(now, excluded, target=target)
        if selected_item is None:
            return None
        selected, selected_kind, _ = selected_item
        release_at = self._selection_release_at(now, selected_item, target=target)
        return DemandStatus(
            release_at=release_at,
            selected=selected,
            request_method=READ_AT_REQUEST_METHOD if selected_kind == "read_at" else None,
        )

    def _selection_release_at(
        self,
        now: float,
        selected_item: tuple[WorkRevision, str, _ReactionCandidate | ReadMessage],
        *,
        target: str | None = None,
    ) -> float:
        _, selected_kind, candidate = selected_item
        if selected_kind == "read_at":
            if target is None:
                self._message_fact_release_at(now)
            return (
                self._read_at_target_release(cast(ReadMessage, candidate))
                if target is not None
                else self._read_at_selection_release(now)
            )
        if selected_kind == "reaction":
            return self._reaction_selection_release(cast(_ReactionCandidate, candidate))
        return self._message_fact_release_at(now)

    def _message_fact_release_at(self, now: float) -> float:
        releases: list[float] = []
        if self._policy.reaction_max_messages_per_cycle > 0:
            reaction_release = _reaction_release_at(self._deps.conn, now)
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
        return min(releases, default=now)

    def _read_at_target_release(self, target: ReadMessage) -> float:
        row = cast(
            tuple[object] | None,
            self._deps.conn.execute(
                "SELECT next_attempt_at FROM message_read_facts WHERE dialog_id=? AND message_id=?",
                (target.dialog_id, target.message_id),
            ).fetchone(),
        )
        return 0.0 if row is None or row[0] is None else float(cast(int | float, row[0]))

    def _read_at_selection_release(self, now: float) -> float:
        release_at = _next_release_at(self._deps.conn, _NEXT_READ_AT_RELEASE_SQL, ())
        return now if release_at is None else release_at

    def _reaction_selection_release(self, target: _ReactionCandidate) -> float:
        dialog_id, message_id, _, _ = target
        row = cast(
            tuple[object, ...] | None,
            self._deps.conn.execute(
                "SELECT next_attempt_at FROM message_reaction_event_status WHERE dialog_id=? AND message_id=?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        retry_at = 0.0 if row is None or row[0] is None else float(cast(int | float, row[0]))
        pacing_release = _reaction_pacing_release_at(self._deps.conn)
        return max(retry_at, pacing_release or 0.0)

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run one bounded candidate cycle under its registered root."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = self._deps.clock()
        selected = budget.selected
        if selected is None:
            raise ValueError("message fact demand requires a selected revision")
        readiness = self._slice_readiness(budget, selected, now)
        if readiness is not None:
            return readiness
        current = self._selected(now, target=selected.key.target)
        if current is None or current[0] != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        return await self._run_selected_slice(budget, selected, current, now)

    def _slice_readiness(self, budget: RpcAttemptBudget, selected: WorkRevision, now: float) -> SliceOutcome | None:
        status = self.status(now, target=selected.key.target)
        if status is None or status.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if not status.is_ready(now) or (budget.exhausted and not budget.can_prepare_retry):
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return None

    async def _run_selected_slice(
        self,
        budget: RpcAttemptBudget,
        selected: WorkRevision,
        current: tuple[WorkRevision, str, _ReactionCandidate | ReadMessage],
        now: float,
    ) -> SliceOutcome:
        selected_kind = current[1]
        reaction_candidates: Sequence[_ReactionCandidate] | None = (
            (cast(_ReactionCandidate, current[2]),) if selected_kind == "reaction" else ()
        )
        read_at_due = selected_kind == "read_at"
        accepted_revisions: list[str] = []
        terminal_revisions: set[str] = set()
        if read_at_due:
            self._read_at_checked_at = None
        with durable_demand_context(DemandKind.MESSAGE_FACT_REFRESH):
            with rpc_attempt_budget(budget):
                await refresh_message_facts_once(
                    self._deps,
                    self._policy,
                    read_at_due=read_at_due,
                    reaction_candidates=reaction_candidates,
                    read_at_candidates=(cast(ReadMessage, current[2]),) if read_at_due else None,
                    accepted_revisions=accepted_revisions,
                    terminal_revisions=terminal_revisions,
                    shutdown_event=self._shutdown_event,
                )
        committed_revision = next((revision for revision in accepted_revisions if revision != selected.revision), None)
        if committed_revision is not None:
            kind = (
                SliceOutcomeKind.TERMINAL if committed_revision in terminal_revisions else SliceOutcomeKind.PROGRESSED
            )
            return SliceOutcome(selected, kind, committed_revision)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)


class ReadReceiptDemandAdapter(DurableDemandAdapter):
    """Read the durable read-position reconciliation state.

    Per-message exact read dates are nested acquisitions selected by
    ``MESSAGE_FACT_REFRESH`` and are intentionally absent from this status.
    """

    demand_kind = DemandKind.READ_RECEIPT_BATCH

    def __init__(
        self,
        conn: sqlite3.Connection,
        run_batch: Callable[[WorkRevision], Awaitable[SliceOutcome]],
        *,
        account_id: int,
    ) -> None:
        self._conn = conn
        self._run_batch = run_batch
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id

    def status(
        self,
        now: float,
        *,
        excluded: frozenset[WorkRevision] = frozenset(),
        target: str | None = None,
    ) -> DemandStatus | None:
        """Return the earliest due read-position batch without changing state."""
        excluded_count = sum(
            revision.key.demand_kind is self.demand_kind and revision.key.account_id == self._account_id
            for revision in excluded
        )
        target_id = _read_receipt_target_id(target)
        if target is not None and target_id is None:
            return None
        rows = _read_receipt_status_rows(self._conn, target_id, excluded_count + 1)
        for row in rows:
            dialog_id = int(cast(int | str, row[0]))
            selected = WorkRevision(
                WorkKey(self.demand_kind, self._account_id, f"dialog:{dialog_id}"),
                repr((row[1], row[2], row[3])),
            )
            if selected in excluded:
                continue
            release_at = now if row[4] is None else float(cast(int | float, row[4]))
            return DemandStatus(release_at=release_at, selected=selected, request_method=READ_RECEIPT_REQUEST_METHOD)
        return None

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run one existing read-position batch under the transport budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise ValueError("read receipt demand requires a selected revision")
        now = time.time()
        status = self.status(now, target=selected.key.target)
        if status is None or status.selected != selected:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if not status.is_ready(now) or (budget.exhausted and not budget.can_prepare_retry):
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        with durable_demand_context(DemandKind.READ_RECEIPT_BATCH):
            with acquisition_context(AcquisitionKind.READ_RECEIPT_SNAPSHOT):
                with rpc_attempt_budget(budget):
                    outcome = await self._run_batch(selected)
        if outcome.selected != selected:
            raise ValueError("read receipt owner returned a receipt for another selection")
        return outcome


def _read_receipt_target_id(target: str | None) -> int | None:
    if target is None:
        return None
    prefix, separator, value = target.partition(":")
    if prefix != "dialog" or not separator:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _read_receipt_status_rows(conn: sqlite3.Connection, target_id: int | None, limit: int) -> list[tuple[object, ...]]:
    where_target = "AND sd.dialog_id=? " if target_id is not None else ""
    params: tuple[object, ...] = (target_id, limit) if target_id is not None else (limit,)
    return cast(
        list[tuple[object, ...]],
        conn.execute(
            "SELECT sd.dialog_id, sd.read_inbox_max_id, sd.read_outbox_max_id, "
            "COALESCE(d.unread_count, 0), sd.read_position_next_attempt_at "
            "FROM synced_dialogs sd "
            "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
            "LEFT JOIN dialogs d ON d.dialog_id=sd.dialog_id "
            "WHERE sd.status='synced' AND (sd.read_inbox_max_id IS NULL "
            "OR sd.read_outbox_max_id IS NULL OR COALESCE(d.unread_count,0)>0 "
            "OR EXISTS (SELECT 1 FROM messages m WHERE m.dialog_id=sd.dialog_id "
            "AND m.is_deleted=0 AND m.is_service=0 AND m.out=0 "
            "AND m.message_id>COALESCE(sd.read_inbox_max_id,-1))) "
            f"{where_target}ORDER BY COALESCE(sd.read_position_next_attempt_at,0), sd.dialog_id LIMIT ?",
            params,
        ).fetchall(),
    )


def _row_ints(row: Sequence[object]) -> tuple[int, ...]:
    return tuple(int(cast(int | str, value)) for value in row)


def _parse_work_target(target: str, kind: str) -> tuple[int, int]:
    try:
        prefix, dialog_id, message_id = target.split(":")
    except ValueError:
        raise ValueError(f"invalid {kind} target") from None
    if prefix != kind:
        raise ValueError(f"invalid {kind} target")
    try:
        return int(dialog_id), int(message_id)
    except ValueError as exc:
        raise ValueError(f"invalid {kind} target") from exc


def _excluded_sibling_count(excluded: frozenset[WorkRevision], kind: DemandKind, account_id: int) -> int:
    return sum(revision.key.demand_kind is kind and revision.key.account_id == account_id for revision in excluded)


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
    keys: Sequence[tuple[int, int]] | None = None,
) -> list[tuple[int, int, int, str | None]]:
    query, key_params = _reaction_candidates_query(keys=keys)
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
    target: tuple[int, int] | None = None,
) -> list[ReadMessage]:
    query = _READ_AT_CANDIDATES_SQL
    params: tuple[object, ...] = (stale_before_utc, limit)
    if target is not None:
        query = query.replace(
            "WHERE sd.status = 'synced'",
            "WHERE sd.status = 'synced' AND m.dialog_id=? AND m.message_id=?",
        )
        params = (*target, stale_before_utc, limit)
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(query, params).fetchall(),
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


async def _refresh_read_at_cycle(  # noqa: PLR0913 - accepted receipt capture is explicit
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    checked_at: int,
    candidates: Sequence[ReadMessage] | None,
    shutdown_event: asyncio.Event | None,
    accepted_revisions: list[str] | None = None,
) -> _ReadAtCycleStats:
    if shutdown_event is not None and shutdown_event.is_set():
        return _empty_read_at_cycle_stats()
    messages = (
        list(candidates)
        if candidates is not None
        else _read_at_candidates(
            deps.conn,
            stale_before_utc=checked_at,
            limit=policy.read_at_max_messages_per_cycle,
        )
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
    reason_counts = {reason.value: 0 for reason in READ_DATE_REASONS}
    grouped_by_dialog: dict[int, list[ReadMessage]] = {}
    for message in messages:
        grouped_by_dialog.setdefault(message.dialog_id, []).append(message)
    grouped_messages = list(grouped_by_dialog.values())
    complete, missing, unavailable, measurement_complete = await _process_read_at_groups(
        deps,
        policy,
        grouped_messages,
        checked_at=checked_at,
        reason_counts=reason_counts,
        cycle_metrics=cycle_metrics,
        accepted_revisions=accepted_revisions,
        shutdown_event=shutdown_event,
    )
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


async def _process_read_at_groups(  # noqa: PLR0913 - exact receipt inputs are explicit
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    grouped_messages: Sequence[Sequence[ReadMessage]],
    *,
    checked_at: int,
    reason_counts: dict[str, int],
    cycle_metrics: dict[str, int],
    accepted_revisions: list[str] | None,
    shutdown_event: asyncio.Event | None,
) -> tuple[int, int, int, bool]:
    complete = missing = unavailable = 0
    measurement_complete = True
    for index, grouped in enumerate(grouped_messages):
        counts: list[int] = []
        await enrich_read_at(
            deps.conn,
            deps.read_receipt_gateway,
            grouped[0].dialog_id,
            grouped,
            writer=deps.writer,
            dialog_type="user",
            read_at_ttl_seconds=policy.read_at_ttl_seconds,
            checked_at=checked_at,
            cycle_counts=counts,
            cycle_reason_counts=reason_counts,
            cycle_metrics=cycle_metrics,
            accepted_revisions=accepted_revisions,
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
    return complete, missing, unavailable, measurement_complete


async def _refresh_one_reaction_page(
    deps: MessageFactRefreshDeps,
    row: tuple[int, int, int, str | None],
    *,
    checked_at: int,
    shutdown_event: asyncio.Event | None,
) -> tuple[int, bool, str | None, bool]:
    dialog_id, message_id, generation, offset = row
    if not await deps.writer.run(_start_reaction_page):
        return 0, True, None, False
    detail = await deps.reaction_detail_refresher.refresh_one(
        dialog_id,
        message_id,
        generation,
        entity=dialog_id,
        offset=offset,
        cancellation_event=shutdown_event,
        now=checked_at,
    )
    retry_after = detail.retry_after
    if retry_after is not None and detail.stop_cycle:
        release_now = int(deps.clock())
        await deps.writer.run(lambda conn: _extend_reaction_release(conn, now=release_now, retry_after=retry_after))
    return (
        detail.fetched_pages,
        detail.stop_cycle or detail.status == "cancelled",
        detail.committed_revision,
        detail.committed_terminal,
    )


def _empty_read_at_cycle_stats() -> _ReadAtCycleStats:
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


def _record_committed_reaction_revision(
    revision: str | None,
    terminal: bool,
    accepted_revisions: list[str] | None,
    terminal_revisions: set[str] | None,
) -> None:
    if revision is None or accepted_revisions is None:
        return
    accepted_revisions.append(revision)
    if terminal and terminal_revisions is not None:
        terminal_revisions.add(revision)


async def _refresh_reaction_pages(  # noqa: PLR0913 - keeps transaction and demand inputs explicit
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    checked_at: int,
    claim_at: int,
    candidates: Sequence[_ReactionCandidate] | None,
    shutdown_event: asyncio.Event | None,
    accepted_revisions: list[str] | None = None,
    terminal_revisions: set[str] | None = None,
) -> int:
    if shutdown_event is not None and shutdown_event.is_set():
        return 0
    selected_reaction_rows = await deps.writer.run(
        lambda conn: _claim_reaction_pages(
            conn,
            now=claim_at,
            max_pages=policy.reaction_detail_max_pages_per_cycle,
            cycle_seconds=policy.reaction_detail_cycle_seconds,
            candidate_limit=policy.reaction_max_messages_per_cycle,
            candidates=candidates,
        )
    )
    reaction_refreshed = 0
    for index, row in enumerate(selected_reaction_rows):
        fetched_pages, stop_page, committed_revision, terminal = await _refresh_one_reaction_page(
            deps,
            row,
            checked_at=checked_at,
            shutdown_event=shutdown_event,
        )
        reaction_refreshed += fetched_pages
        _record_committed_reaction_revision(committed_revision, terminal, accepted_revisions, terminal_revisions)
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
    read_at_candidates: Sequence[ReadMessage] | None = None,
    accepted_revisions: list[str] | None = None,
    terminal_revisions: set[str] | None = None,
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
            candidates=read_at_candidates,
            shutdown_event=shutdown_event,
            accepted_revisions=accepted_revisions,
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
        accepted_revisions=accepted_revisions,
        terminal_revisions=terminal_revisions,
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
