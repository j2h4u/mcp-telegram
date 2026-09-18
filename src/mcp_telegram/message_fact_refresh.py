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
from .telegram_reading import TelegramReadReceiptGateway
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import rpc_attempt_budget

_REACTION_CANDIDATES_SQL = """
SELECT m.dialog_id, m.message_id, a.generation, d.next_offset
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN message_reaction_aggregate_state a ON a.dialog_id=m.dialog_id AND a.message_id=m.message_id
LEFT JOIN message_reaction_event_status d ON d.dialog_id=m.dialog_id AND d.message_id=m.message_id
WHERE sd.status = 'synced'
  AND (a.aggregate_row_count > 0
       OR d.status IN ('partial','unavailable'))
  AND (d.dialog_id IS NULL OR d.aggregate_generation < a.generation
       OR (d.status IN ('stale','partial','unavailable') AND COALESCE(d.next_attempt_at, 0) <= ?))
ORDER BY m.sent_at DESC, m.dialog_id, m.message_id
LIMIT ?
"""


_READ_AT_CANDIDATES_SQL = """
SELECT m.dialog_id, m.message_id, m.sent_at
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN entities e ON e.id = m.dialog_id
LEFT JOIN message_read_facts f
  ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id
WHERE sd.status = 'synced'
  AND lower(e.type) = 'user'
  AND m.out = 1
  AND sd.read_outbox_max_id IS NOT NULL
  AND m.message_id <= sd.read_outbox_max_id
  AND (f.dialog_id IS NULL OR f.status != 'complete' OR f.read_at IS NULL)
  AND (f.dialog_id IS NULL OR f.checked_at <= ?)
ORDER BY m.sent_at DESC, m.dialog_id, m.message_id
LIMIT ?
"""


_NEXT_REACTION_RELEASE_SQL = """
SELECT MIN(COALESCE(d.next_attempt_at, ?))
FROM message_reaction_aggregate_state a
JOIN synced_dialogs sd ON sd.dialog_id=a.dialog_id AND sd.status='synced'
JOIN full_history_enrollment fhe ON fhe.dialog_id=a.dialog_id AND fhe.enabled=1
LEFT JOIN message_reaction_event_status d ON d.dialog_id=a.dialog_id AND d.message_id=a.message_id
WHERE (a.aggregate_row_count > 0 OR d.status IN ('partial','unavailable'))
  AND (d.dialog_id IS NULL OR d.aggregate_generation < a.generation
       OR d.status IN ('stale','partial','unavailable'))
"""

_TERMINAL_REACTION_SUPPRESSED_SQL = """
SELECT COUNT(*)
FROM message_reaction_aggregate_state a
JOIN synced_dialogs sd ON sd.dialog_id=a.dialog_id AND sd.status='synced'
JOIN full_history_enrollment fhe ON fhe.dialog_id=a.dialog_id AND fhe.enabled=1
JOIN message_reaction_event_status d ON d.dialog_id=a.dialog_id AND d.message_id=a.message_id
WHERE d.status='complete' AND d.aggregate_generation=a.generation
"""


_NEXT_READ_AT_RELEASE_SQL = """
SELECT MIN(CASE WHEN f.checked_at IS NULL THEN 0 ELSE f.checked_at + ? END)
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN entities e ON e.id = m.dialog_id
LEFT JOIN message_read_facts f
  ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id
WHERE sd.status = 'synced'
  AND lower(e.type) = 'user'
  AND m.out = 1
  AND sd.read_outbox_max_id IS NOT NULL
  AND m.message_id <= sd.read_outbox_max_id
  AND (f.dialog_id IS NULL OR f.status != 'complete' OR f.read_at IS NULL)
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

_READ_AT_CYCLE_COUNT_FIELDS = 4


@dataclass(frozen=True, slots=True)
class MessageFactRefreshPolicy:
    """Bounded daemon-side policy for optional Telegram fact acquisition."""

    reaction_max_messages_per_cycle: int
    read_at_max_messages_per_cycle: int
    pause_seconds: float
    read_at_ttl_seconds: int
    reaction_detail_max_pages_per_cycle: int = 5


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
    measurement_complete: bool


@dataclass(frozen=True, slots=True)
class MessageFactRefreshDeps:
    """Infrastructure dependencies for one optional fact refresh cycle."""

    conn: sqlite3.Connection
    reaction_detail_refresher: ReactionDetailRefresher
    read_receipt_gateway: TelegramReadReceiptGateway
    read_at_observer: Callable[[Mapping[str, object]], None] | None = None


def _next_release_at(conn: sqlite3.Connection, query: str, ttl_seconds: int) -> float | None:
    row = cast(tuple[object] | None, conn.execute(query, (ttl_seconds,)).fetchone())
    value = None if row is None else row[0]
    return None if value is None else float(cast(int | float, value))


class MessageFactRefreshDemandAdapter(DurableDemandAdapter):
    """Read the durable optional-fact candidate state.

    This is the durable orchestration root for background per-message reaction
    and exact read-date candidates. The nested acquisition adapters do not
    report these same rows independently.
    """

    demand_kind = DemandKind.MESSAGE_FACT_REFRESH

    def __init__(self, deps: MessageFactRefreshDeps, policy: MessageFactRefreshPolicy) -> None:
        self._deps = deps
        self._policy = policy

    def status(self, now: float) -> DemandStatus | None:
        """Return the first missing or TTL-expired candidate release boundary."""
        del now
        releases: list[float] = []
        if self._policy.reaction_max_messages_per_cycle > 0:
            reaction_release = _next_release_at(self._deps.conn, _NEXT_REACTION_RELEASE_SQL, 0)
            if reaction_release is not None:
                releases.append(reaction_release)
        if self._policy.read_at_max_messages_per_cycle > 0:
            read_at_release = _next_release_at(
                self._deps.conn,
                _NEXT_READ_AT_RELEASE_SQL,
                self._policy.read_at_ttl_seconds,
            )
            if read_at_release is not None:
                releases.append(read_at_release)
        if not releases:
            return None
        return DemandStatus(release_at=min(releases))

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Run one bounded candidate cycle under its registered root."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        status = self.status(time.time())
        if status is None or not status.is_ready(time.time()):
            return
        with demand_context(DemandKind.MESSAGE_FACT_REFRESH):
            with rpc_attempt_budget(budget):
                try:
                    await refresh_message_facts_once(self._deps, self._policy)
                except RpcAttemptBudgetExhaustedError:
                    return


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


def _reaction_candidates(
    conn: sqlite3.Connection,
    *,
    stale_before_utc: int,
    limit: int,
) -> list[tuple[int, int, int, str | None]]:
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(_REACTION_CANDIDATES_SQL, (stale_before_utc, limit)).fetchall(),
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


def _terminal_reaction_suppressed(conn: sqlite3.Connection) -> bool:
    row = cast(tuple[object] | None, conn.execute(_TERMINAL_REACTION_SUPPRESSED_SQL).fetchone())
    return row is not None and int(cast(int | str, row[0])) > 0


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


def _read_at_attempt_counts(
    conn: sqlite3.Connection,
    messages: Sequence[ReadMessage],
) -> tuple[int, int]:
    """Return first-attempt and retry counts for one selected batch."""
    if not messages:
        return 0, 0
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            "SELECT dialog_id, message_id FROM message_read_facts "
            "WHERE (dialog_id, message_id) IN (" + ",".join("(?, ?)" for _ in messages) + ")",
            [value for message in messages for value in (message.dialog_id, message.message_id)],
        ).fetchall(),
    )
    retry_keys = {(int(cast(int | str, row[0])), int(cast(int | str, row[1]))) for row in rows}
    return len(messages) - len(retry_keys), len(retry_keys)


def _terminal_read_at_suppressed(conn: sqlite3.Connection) -> int:
    """Count eligible terminal rows omitted from acquisition forever."""
    row = cast(
        tuple[object] | None,
        conn.execute(
            "SELECT COUNT(*) "
            "FROM messages m "
            "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id "
            "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
            "JOIN entities e ON e.id = m.dialog_id "
            "JOIN message_read_facts f ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id "
            "WHERE sd.status = 'synced' AND lower(e.type) = 'user' AND m.out = 1 "
            "AND sd.read_outbox_max_id IS NOT NULL AND m.message_id <= sd.read_outbox_max_id "
            "AND f.status = 'complete' AND f.read_at IS NOT NULL"
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
) -> None:
    """Publish one bounded, content-free observation after a complete cycle."""
    observer = deps.read_at_observer
    if observer is None:
        return
    try:
        observer(
            {
                "first_attempts": first_attempts,
                "retry_attempts": retry_attempts,
                "terminal_suppressed": terminal_suppressed,
                "complete": complete,
                "missing": missing,
                "unavailable": unavailable,
                "measurement_complete": True,
            }
        )
    except Exception:  # noqa: BLE001 - telemetry must not affect fact persistence
        # Telemetry is best effort and must not affect fact persistence.
        return


def _group_message_ids(rows: Sequence[tuple[int, int]]) -> dict[int, list[int]]:
    grouped: dict[int, list[int]] = {}
    for dialog_id, message_id in rows:
        grouped.setdefault(dialog_id, []).append(message_id)
    return grouped


def _group_messages(messages: Sequence[ReadMessage]) -> dict[int, list[ReadMessage]]:
    grouped: dict[int, list[ReadMessage]] = {}
    for message in messages:
        grouped.setdefault(message.dialog_id, []).append(message)
    return grouped


async def _interruptible_pause(shutdown_event: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
    except TimeoutError:
        return


async def _refresh_read_at_cycle(
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    checked_at: int,
    shutdown_event: asyncio.Event | None,
) -> _ReadAtCycleStats:
    if shutdown_event is not None and shutdown_event.is_set():
        return _ReadAtCycleStats(0, 0, 0, 0, 0, 0, False)
    messages = _read_at_candidates(
        deps.conn,
        stale_before_utc=checked_at - policy.read_at_ttl_seconds,
        limit=policy.read_at_max_messages_per_cycle,
    )
    first_attempts, retry_attempts = _read_at_attempt_counts(deps.conn, messages)
    terminal_suppressed = _terminal_read_at_suppressed(deps.conn)
    complete = missing = unavailable = 0
    measurement_complete = True
    groups = _group_messages(messages)
    for index, (dialog_id, group) in enumerate(groups.items()):
        counts: list[int] = []
        await enrich_read_at(
            deps.conn,
            deps.read_receipt_gateway,
            dialog_id,
            group,
            dialog_type="user",
            read_at_ttl_seconds=policy.read_at_ttl_seconds,
            checked_at=checked_at,
            cycle_counts=counts,
        )
        if len(counts) != _READ_AT_CYCLE_COUNT_FIELDS:
            measurement_complete = False
        else:
            complete += counts[0]
            missing += counts[1]
            unavailable += counts[2]
            measurement_complete = measurement_complete and bool(counts[3])
        if shutdown_event is not None and shutdown_event.is_set():
            measurement_complete = False
            break
        if shutdown_event is not None and index < len(groups) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)
    return _ReadAtCycleStats(
        first_attempts=first_attempts,
        retry_attempts=retry_attempts,
        terminal_suppressed=terminal_suppressed,
        complete=complete,
        missing=missing,
        unavailable=unavailable,
        measurement_complete=measurement_complete,
    )


async def refresh_message_facts_once(
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    now: int | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> MessageFactRefreshResult:
    """Refresh a bounded batch of optional message facts into SQLite."""
    if policy.reaction_max_messages_per_cycle <= 0 and policy.read_at_max_messages_per_cycle <= 0:
        return MessageFactRefreshResult(reaction_refreshed=0)

    checked_at = int(time.time() if now is None else now)
    reaction_rows = _reaction_candidates(
        deps.conn,
        stale_before_utc=checked_at,
        limit=policy.reaction_max_messages_per_cycle,
    )
    if policy.reaction_max_messages_per_cycle > 0 and _terminal_reaction_suppressed(deps.conn):
        observe_terminal_suppressed = getattr(deps.reaction_detail_refresher, "observe_terminal_suppressed", None)
        if callable(observe_terminal_suppressed):
            observe_terminal_suppressed()
    reaction_refreshed = 0
    selected_reaction_rows = reaction_rows[: policy.reaction_detail_max_pages_per_cycle]
    for index, (dialog_id, message_id, generation, offset) in enumerate(selected_reaction_rows):
        detail = await deps.reaction_detail_refresher.refresh_one(
            dialog_id,
            message_id,
            generation,
            entity=dialog_id,
            offset=offset,
            cancellation_event=shutdown_event,
            now=checked_at,
        )
        reaction_refreshed += detail.fetched_pages
        if shutdown_event is not None and index < len(selected_reaction_rows) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)

    if policy.read_at_max_messages_per_cycle > 0:
        stats = await _refresh_read_at_cycle(
            deps,
            policy,
            checked_at=checked_at,
            shutdown_event=shutdown_event,
        )
    else:
        stats = None

    if stats is not None and stats.measurement_complete:
        _observe_read_at_cycle(
            deps,
            first_attempts=stats.first_attempts,
            retry_attempts=stats.retry_attempts,
            terminal_suppressed=stats.terminal_suppressed,
            complete=stats.complete,
            missing=stats.missing,
            unavailable=stats.unavailable,
        )

    return MessageFactRefreshResult(
        reaction_refreshed=reaction_refreshed,
    )
