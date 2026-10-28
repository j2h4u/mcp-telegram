"""Tier B — ColdBackfill — full-history low-priority per-peer self-search scheduler.

Walks each enrolled peer's complete authored history BACKWARD (no time ceiling),
advancing cold_offset_id toward the oldest message and marking cold_status='complete'
only when a genuine HISTORY_FLOOR is reached.

Key design constraints (from plan reviews):
- cold_status='complete' is set ONLY on SkipReason.HISTORY_FLOOR — a genuine empty
  batch from a reachable peer (concern 3).
- SkipReason.ACCESS_SKIP sets cold_next_retry_at + cold_status='pending' and leaves
  cold_offset_id unchanged — a transient cache/session miss can NEVER permanently
  mark cold backfill complete.
- SkipReason.FLOOD_WAIT sets cold_next_retry_at (not any hot_* field) — Tier B is
  the sole owner of durable FloodWait retry for the cold path (concern 5).
- cold_offset_id walks downward: each non-empty batch sets
  cold_offset_id = result.min_id.
- NO hot_* column is ever written here.
- run_cold_backfill_pass returns a structured ColdPassResult for one bounded demand
  slice, preserving the distinction between idle and zero-write work.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from .activity_peer_sweep import (
    SkipReason,
    SweepResult,
    _load_working_set_enrollment_state,
    run_working_set_enrollment_slice,
    sweep_peer_once,
    working_set_enrollment_release_at,
)
from .activity_substrate import ActivityClient
from .hydration_queue import HydrationPriority
from .sqlite_writer import SQLiteWriter
from .sync_read_model import SyncStatus
from .sync_transactions import write_transaction
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import TelegramRpcSource, rpc_attempt_budget, rpc_scope

logger = logging.getLogger(__name__)


@contextmanager
def _cold_demand_scope() -> Iterator[None]:
    """Install the cold-page root for direct legacy or adapter execution."""
    with durable_demand_context(DemandKind.COLD_PEER_PAGE):
        yield


class _ColdBackfillScheduling(Protocol):
    """Scheduling values injected by the daemon-owned configuration tree."""

    @property
    def activity_cold_backfill_batch_pause_seconds(self) -> float: ...

    @property
    def activity_cold_enroll_seconds(self) -> float: ...

    @property
    def activity_cold_access_retry_seconds(self) -> float: ...


# ---------------------------------------------------------------------------
# Pacing is owned by the frozen runtime configuration model.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ColdBackfillHistoryPacing:
    batch_s: float
    enroll_s: float
    access_retry_s: float


@dataclass(frozen=True, slots=True)
class ColdBackfillPacing:
    history: ColdBackfillHistoryPacing

    @classmethod
    def from_scheduling(cls, scheduling: _ColdBackfillScheduling) -> ColdBackfillPacing:
        return cls(
            history=ColdBackfillHistoryPacing(
                batch_s=scheduling.activity_cold_backfill_batch_pause_seconds,
                enroll_s=scheduling.activity_cold_enroll_seconds,
                access_retry_s=scheduling.activity_cold_access_retry_seconds,
            ),
        )


@dataclass(slots=True)
class ColdPeerPageDemandAdapter(DurableDemandAdapter):
    """Expose cold peer-page readiness over ``activity_dialog_state``."""

    client: ActivityClient
    conn: sqlite3.Connection
    shutdown_event: asyncio.Event
    pacing: ColdBackfillPacing
    timeout_s: float
    writer: SQLiteWriter = field(kw_only=True)
    account_id: int = field(kw_only=True)
    demand_kind = DemandKind.COLD_PEER_PAGE

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Return the earliest release among enrollment and peer pages."""
        page_release = _next_cold_release_at(self.conn, now=now)
        enrollment_release = working_set_enrollment_release_at(
            self.conn, now=now, cadence_s=self.pacing.history.enroll_s
        )
        if page_release is None and enrollment_release is None:
            return None
        enrollment_revision = self._enrollment_revision()
        if (
            enrollment_release is not None
            and (page_release is None or enrollment_release <= page_release)
            and enrollment_revision not in excluded
        ):
            return self._enrollment_status(enrollment_revision, enrollment_release, excluded)
        return self._peer_or_enrollment_status(enrollment_revision, enrollment_release, page_release, excluded)

    def _enrollment_revision(self) -> WorkRevision:
        state = _load_working_set_enrollment_state(self.conn)
        revision = repr((state.get("activity_working_set_phase"), state.get("activity_working_set_cursor")))
        return WorkRevision(WorkKey(self.demand_kind, self.account_id, "enrollment"), revision)

    @staticmethod
    def _enrollment_status(
        revision: WorkRevision, release_at: float, excluded: frozenset[WorkRevision]
    ) -> DemandStatus | None:
        return None if revision in excluded else DemandStatus(release_at=release_at, selected=revision)

    def _peer_or_enrollment_status(
        self,
        enrollment_revision: WorkRevision,
        enrollment_release: float | None,
        page_release: float | None,
        excluded: frozenset[WorkRevision],
    ) -> DemandStatus | None:
        candidates = []
        for dialog_id, offset_id, retry_at in self._cold_peer_rows():
            selected = WorkRevision(
                WorkKey(self.demand_kind, self.account_id, f"peer:{int(dialog_id)}"), str(int(offset_id or 0))
            )
            if selected not in excluded:
                candidates.append((float(retry_at or 0), int(dialog_id), selected))
        if candidates:
            release, _, selected = min(candidates, key=lambda item: (item[0], item[1]))
            if enrollment_release is not None and enrollment_release <= release and enrollment_revision not in excluded:
                return DemandStatus(release_at=enrollment_release, selected=enrollment_revision)
            return DemandStatus(release_at=release, selected=selected)
        return self._enrollment_status(enrollment_revision, page_release or 0.0, excluded)

    def _cold_peer_rows(self) -> list[tuple[int, int | None, int | None]]:
        return cast(
            list[tuple[int, int | None, int | None]],
            self.conn.execute(
                "SELECT dialog_id,COALESCE(cold_offset_id,0),cold_next_retry_at FROM activity_dialog_state "
                "WHERE cold_status!='complete' "
                "AND COALESCE((SELECT status FROM synced_dialogs WHERE dialog_id=activity_dialog_state.dialog_id),'')!='access_lost' "
                "ORDER BY updated_at,dialog_id"
            ).fetchall(),
        )

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run one enrollment unit or one claimed peer page."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise RuntimeError("cold activity slice requires an owner-selected revision")
        if self.shutdown_event.is_set():
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if selected.key.demand_kind is not self.demand_kind or selected.key.account_id != self.account_id:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        validation = self._validate_cold_selection(selected, time.time())
        if validation is not None:
            return validation
        if selected.key.target == "enrollment":
            await self._run_cold_enrollment(budget)
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        return await self._run_cold_peer(selected, budget)

    def _validate_cold_selection(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        if selected.key.target == "enrollment":
            return self._validate_cold_enrollment(selected, now)
        return self._validate_cold_peer_selection(selected, now)

    def _validate_cold_enrollment(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        state = _load_working_set_enrollment_state(self.conn)
        revision = repr((state.get("activity_working_set_phase"), state.get("activity_working_set_cursor")))
        release = working_set_enrollment_release_at(self.conn, now=now, cadence_s=self.pacing.history.enroll_s)
        if revision != selected.revision or release is None:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if release > now:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return None

    def _validate_cold_peer_selection(self, selected: WorkRevision, now: float) -> SliceOutcome | None:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        row = cast(
            tuple[int, str | None, int | None, str | None] | None,
            self.conn.execute(
                "SELECT COALESCE(activity.cold_offset_id,0),activity.cold_status,activity.cold_next_retry_at,synced.status "
                "FROM activity_dialog_state activity LEFT JOIN synced_dialogs synced "
                "ON synced.dialog_id=activity.dialog_id WHERE activity.dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        if (
            row is None
            or int(row[0]) != int(selected.revision)
            or row[1] == "complete"
            or row[3] == SyncStatus.ACCESS_LOST.value
        ):
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if float(row[2] or 0) > now:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return None

    async def _run_cold_enrollment(self, budget: RpcAttemptBudget) -> None:
        with _cold_demand_scope():
            with rpc_attempt_budget(budget):
                await run_working_set_enrollment_slice(
                    self.client,
                    self.conn,
                    source=TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
                    cadence_s=self.pacing.history.enroll_s,
                    timeout_s=self.timeout_s,
                    writer=self.writer,
                )

    async def _run_cold_peer(self, selected: WorkRevision, budget: RpcAttemptBudget) -> SliceOutcome:
        dialog_id = int(selected.key.target.split(":", 1)[1])
        with _cold_demand_scope():
            with rpc_attempt_budget(budget):
                result = await run_cold_backfill_pass(
                    self.client,
                    self.conn,
                    self.shutdown_event,
                    pacing=self.pacing,
                    timeout_s=self.timeout_s,
                    writer=self.writer,
                    expected_dialog=dialog_id,
                    expected_offset=int(selected.revision),
                )
        return self._cold_result(selected, result)

    @staticmethod
    def _cold_result(selected: WorkRevision, result: ColdPassResult) -> SliceOutcome:
        if not result.accepted:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if result.flood_wait_seconds is not None:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        revision = result.committed_revision
        if revision is None or revision == selected.revision:
            return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)
        terminal = result.outcome is ColdPassOutcome.ZERO_PERSISTED and result.persisted == 0
        return SliceOutcome(selected, SliceOutcomeKind.TERMINAL if terminal else SliceOutcomeKind.PROGRESSED, revision)


_BACKFILL_BATCH_LIMIT = 100


# ---------------------------------------------------------------------------
# Structured result — cycle-4 MEDIUM: NOT a bare int
# ---------------------------------------------------------------------------


class ColdPassOutcome(StrEnum):
    NO_DUE_PEER = "no_due_peer"
    # No peer is due; loop should sleep the long idle interval.
    WROTE = "wrote"
    # A peer was processed and persisted > 0 rows.
    ZERO_PERSISTED = "zero_persisted"
    # A peer WAS processed but wrote 0 rows (HISTORY_FLOOR or ACCESS_SKIP).
    # The loop must NOT idle 300s while more peers are due.
    FLOOD_WAIT = "flood_wait"
    # A peer encountered FloodWait; cold_next_retry_at was written.


@dataclass
class ColdPassResult:
    """Structured result from run_cold_backfill_pass."""

    outcome: ColdPassOutcome
    persisted: int  # count of rows written this pass; 0 unless outcome==WROTE
    flood_wait_seconds: int | None = None
    accepted: bool = True
    committed_revision: str | None = None


@dataclass(frozen=True, slots=True)
class _ColdPeerFinishContext:
    conn: sqlite3.Connection
    dialog_id: int
    offset_id: int
    started_at: float
    now: int
    claim_until: int
    result: SweepResult


def _next_cold_release_at(conn: sqlite3.Connection, *, now: float) -> float | None:
    """Return the earliest authoritative cold-page release boundary."""
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite non-negative timestamp")
    row = cast(
        tuple[int] | None,
        conn.execute(
            """
            SELECT MIN(COALESCE(ads.cold_next_retry_at, 0))
            FROM activity_dialog_state AS ads
            LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
            WHERE ads.cold_status != 'complete'
              AND COALESCE(sd.status, '') != :ineligible_status
            """,
            {"ineligible_status": SyncStatus.ACCESS_LOST.value},
        ).fetchone(),
    )
    if row is None or row[0] is None:
        return None
    return float(row[0])


def _claim_cold_backfill_peer(
    conn: sqlite3.Connection,
    *,
    now: int,
    claim_until: int,
    expected_dialog: int | None = None,
    expected_offset: int | None = None,
) -> tuple[int, int | None] | None:
    """Atomically claim the oldest due peer using the existing retry field as a lease."""
    with write_transaction(conn):
        if expected_dialog is not None:
            cursor = conn.execute(
                "UPDATE activity_dialog_state SET cold_status='running',cold_next_retry_at=?,updated_at=? "
                "WHERE dialog_id=? AND COALESCE(cold_offset_id,0)=? AND cold_status!='complete' "
                "AND (cold_next_retry_at IS NULL OR cold_next_retry_at<=?) "
                "AND COALESCE((SELECT status FROM synced_dialogs WHERE dialog_id=activity_dialog_state.dialog_id),'')!=? "
                "RETURNING dialog_id,cold_offset_id",
                (claim_until, now, expected_dialog, expected_offset or 0, now, SyncStatus.ACCESS_LOST.value),
            )
            return cast(tuple[int, int | None] | None, cursor.fetchone())
        return cast(
            tuple[int, int | None] | None,
            conn.execute(
                """
                UPDATE activity_dialog_state
                SET cold_status = 'running',
                    cold_next_retry_at = :claim_until,
                    updated_at = :now
                WHERE dialog_id = (
                    SELECT ads.dialog_id
                    FROM activity_dialog_state AS ads
                    LEFT JOIN synced_dialogs AS sd ON sd.dialog_id = ads.dialog_id
                    WHERE ads.cold_status != 'complete'
                      AND (ads.cold_next_retry_at IS NULL OR ads.cold_next_retry_at <= :now)
                      AND COALESCE(sd.status, '') != :ineligible_status
                    ORDER BY ads.updated_at ASC, ads.dialog_id ASC
                    LIMIT 1
                )
                  AND cold_status != 'complete'
                  AND (cold_next_retry_at IS NULL OR cold_next_retry_at <= :now)
                RETURNING dialog_id, cold_offset_id
                """,
                {
                    "now": now,
                    "claim_until": claim_until,
                    "ineligible_status": SyncStatus.ACCESS_LOST.value,
                },
            ).fetchone(),
        )


def _save_claimed_cold_state(ctx: _ColdPeerFinishContext, **fields: object) -> bool:
    """Apply a result only while this slice still owns the peer lease."""
    allowed = {"cold_offset_id", "cold_status", "cold_next_retry_at", "cold_last_error"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown claimed cold fields {unknown!r}")
    assignments = ", ".join(f"{column} = ?" for column in fields)
    values = [*fields.values(), int(time.time()), ctx.dialog_id, ctx.claim_until]
    with write_transaction(ctx.conn):
        cursor = ctx.conn.execute(
            f"UPDATE activity_dialog_state SET {assignments}, updated_at = ? "
            "WHERE dialog_id = ? AND cold_status = 'running' AND cold_next_retry_at = ?",
            values,
        )
    return cursor.rowcount == 1


def _cold_claim_matches(conn: sqlite3.Connection, dialog_id: int, offset_id: int, claim_until: int) -> bool:
    row = cast(
        tuple[int, str | None, int | None] | None,
        conn.execute(
            "SELECT COALESCE(cold_offset_id,0),cold_status,cold_next_retry_at FROM activity_dialog_state WHERE dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    return row is not None and (int(row[0]), row[1], row[2]) == (offset_id, "running", claim_until)


def _release_cold_claim(conn: sqlite3.Connection, *, dialog_id: int, claim_until: int) -> bool:
    """Return an unfinished peer to the queue after a slice boundary."""
    with write_transaction(conn):
        cursor = conn.execute(
            "UPDATE activity_dialog_state "
            "SET cold_status = 'pending', cold_next_retry_at = NULL, updated_at = ? "
            "WHERE dialog_id = ? AND cold_status = 'running' AND cold_next_retry_at = ?",
            (int(time.time()), dialog_id, claim_until),
        )
    return cursor.rowcount == 1


def _finish_cold_backfill_peer(ctx: _ColdPeerFinishContext, pacing: ColdBackfillPacing) -> ColdPassResult:
    """Apply one peer result and emit the matching telemetry."""
    result = ctx.result
    if result.skip_reason is SkipReason.FLOOD_WAIT:
        flood_wait_seconds = cast(int, result.flood_wait_seconds)
        accepted = _release_cold_claim(ctx.conn, dialog_id=ctx.dialog_id, claim_until=ctx.claim_until)
        return ColdPassResult(
            outcome=ColdPassOutcome.FLOOD_WAIT,
            persisted=0,
            flood_wait_seconds=flood_wait_seconds,
            accepted=accepted,
        )

    if result.skip_reason is SkipReason.ACCESS_SKIP:
        next_retry_at = int(ctx.now + pacing.history.access_retry_s)
        accepted = _save_claimed_cold_state(
            ctx,
            cold_status="pending",
            cold_next_retry_at=next_retry_at,
            cold_last_error="access_skip",
        )
        logger.debug(
            "activity_cold_backfill_access_skip dialog_id=%r retry_delay_s=%.3f cold_next_retry_at=%d duration_s=%.3f",
            ctx.dialog_id,
            pacing.history.access_retry_s,
            next_retry_at,
            time.monotonic() - ctx.started_at,
        )
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0, accepted=accepted)

    if result.skip_reason is SkipReason.HISTORY_FLOOR:
        accepted = _save_claimed_cold_state(
            ctx,
            cold_status="complete",
            cold_next_retry_at=None,
        )
        logger.info(
            "activity_cold_backfill_complete dialog_id=%r offset_id=%d duration_s=%.3f",
            ctx.dialog_id,
            ctx.offset_id,
            time.monotonic() - ctx.started_at,
        )
        return ColdPassResult(
            outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0, accepted=accepted, committed_revision="complete"
        )

    new_offset = result.min_id
    accepted = _save_claimed_cold_state(
        ctx,
        cold_offset_id=new_offset,
        cold_status="pending",
        cold_next_retry_at=None,
    )
    logger.debug(
        "activity_cold_backfill_batch dialog_id=%r old_offset=%d new_offset=%r persisted=%d duration_s=%.3f",
        ctx.dialog_id,
        ctx.offset_id,
        new_offset,
        result.persisted,
        time.monotonic() - ctx.started_at,
    )
    if result.persisted and result.persisted > 0:
        return ColdPassResult(
            outcome=ColdPassOutcome.WROTE,
            persisted=result.persisted,
            accepted=accepted,
            committed_revision=str(new_offset or 0) if accepted else None,
        )
    return ColdPassResult(
        outcome=ColdPassOutcome.ZERO_PERSISTED,
        persisted=0,
        accepted=accepted,
        committed_revision=str(new_offset or 0) if accepted else None,
    )


async def run_cold_backfill_pass(  # noqa: PLR0913 - explicit worker dependencies
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    pacing: ColdBackfillPacing,
    timeout_s: float,
    writer: SQLiteWriter,
    expected_dialog: int | None = None,
    expected_offset: int | None = None,
) -> ColdPassResult:
    """Run one cold peer-page operation under its precise durable root."""
    with _cold_demand_scope():
        return await _run_cold_backfill_pass(
            client,
            conn,
            shutdown_event,
            pacing=pacing,
            timeout_s=timeout_s,
            writer=writer,
            expected_dialog=expected_dialog,
            expected_offset=expected_offset,
        )


async def _run_cold_backfill_pass(  # noqa: PLR0913 - explicit worker dependencies
    client: ActivityClient,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    pacing: ColdBackfillPacing,
    timeout_s: float,
    writer: SQLiteWriter,
    expected_dialog: int | None = None,
    expected_offset: int | None = None,
) -> ColdPassResult:
    """Run one Tier-B ColdBackfill pass.

    Selects ONE due peer (cold_status != 'complete', cold_next_retry_at due),
    calls sweep_peer_once with offset_id=cold_offset_id (backward walk), and
    branches strictly on result.skip_reason.

    Due-peer selection uses ORDER BY updated_at ASC, dialog_id ASC — the
    intentional round-robin anti-starvation mechanism so no single deep-history
    peer monopolises the queue.

    Returns a ColdPassResult distinguishing idle (NO_DUE_PEER) from processed
    outcomes (WROTE / ZERO_PERSISTED / FLOOD_WAIT) so the caller can choose
    the correct sleep interval.

    Only cold_* columns are written — hot_* columns are never touched.
    """
    started_at = time.monotonic()
    now = int(time.time())

    claim_until = now + max(1, math.ceil(timeout_s + pacing.history.batch_s))
    row = await writer.run(
        lambda db: _claim_cold_backfill_peer(
            db, now=now, claim_until=claim_until, expected_dialog=expected_dialog, expected_offset=expected_offset
        )
    )

    if row is None:
        logger.debug("activity_cold_backfill_pass_no_due_peer")
        return ColdPassResult(outcome=ColdPassOutcome.NO_DUE_PEER, persisted=0)

    dialog_id, cold_offset_id = row

    # offset_id=0 means "start from newest and walk down"; thereafter use the
    # stored cold_offset_id which shrinks toward the history floor each pass.
    offset_id = cold_offset_id or 0

    logger.debug(
        "activity_cold_backfill_pass_start dialog_id=%r offset_id=%d",
        dialog_id,
        offset_id,
    )

    try:
        with rpc_scope(
            TelegramRpcSource.ACTIVITY_COLD_BACKFILL,
            timeout_seconds=timeout_s,
            acquisition_kind=AcquisitionKind.MESSAGE_SEARCH_PAGE,
        ):
            result = await sweep_peer_once(
                client,
                conn,
                dialog_id,
                offset_id=offset_id,
                min_id=0,  # no time/id ceiling — full history walk
                limit=_BACKFILL_BATCH_LIMIT,
                timeout_s=timeout_s,
                writer=writer,
                hydration_priority=HydrationPriority.BACKFILL,
                accept_page=lambda db: _cold_claim_matches(db, dialog_id, offset_id, claim_until),
            )
    except RpcAttemptBudgetExhaustedError:
        # A slice bound is not an access failure. Release the short claim so
        # status() exposes immediate durable continuation on the next slice.
        await writer.run(lambda db: _release_cold_claim(db, dialog_id=dialog_id, claim_until=claim_until))
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0)

    if not result.accepted:
        await writer.run(lambda db: _release_cold_claim(db, dialog_id=dialog_id, claim_until=claim_until))
        return ColdPassResult(outcome=ColdPassOutcome.ZERO_PERSISTED, persisted=0, accepted=False)

    return await writer.run(
        lambda db: _finish_cold_backfill_peer(
            _ColdPeerFinishContext(
                conn=db,
                dialog_id=dialog_id,
                offset_id=offset_id,
                started_at=started_at,
                now=now,
                claim_until=claim_until,
                result=result,
            ),
            pacing,
        )
    )
