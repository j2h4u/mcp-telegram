"""Restart-safe, one-page reactor-detail acquisition."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from ..telegram_demand import AcquisitionKind, acquisition_context
from ..telegram_rpc_scheduler import TelegramRpcSource, rpc_scope
from .contracts import GatewayFailure, GatewayFailureKind, ReactionDetailFetchResult, ReactionEvent
from .ports import TelegramReactionGateway


@dataclass(frozen=True, slots=True)
class ReactionDetailPolicy:
    unavailable_retry_seconds: int = 600
    page_size: int = 100

    def __post_init__(self) -> None:
        if self.unavailable_retry_seconds < 1 or self.page_size < 1:
            raise ValueError("reaction detail policy values must be positive")


@dataclass(frozen=True, slots=True)
class ReactionDetailResult:
    status: str
    fetched_pages: int = 0
    next_offset: str | None = None
    failure_kind: str | None = None
    retry_after: int | None = None

    @property
    def stop_cycle(self) -> bool:
        """Stop this cycle after a FloodWait while leaving later cycles eligible."""
        return self.failure_kind == "flood_wait"


@dataclass(frozen=True, slots=True)
class _ExpectedDetailState:
    dialog_id: int
    message_id: int
    requested_generation: int
    aggregate_generation: int | None
    status: str
    offset: str | None
    staged_count: int


class ReactionDetailRefresher:
    """Acquire one page at a time and commit it with generation/offset CAS."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        gateway: TelegramReactionGateway,
        *,
        policy: ReactionDetailPolicy | None = None,
        now: Callable[[], float] = time.time,
        observation_sink: Callable[[str, str], None] | None = None,
    ) -> None:
        self._conn = conn
        self._gateway = gateway
        self._policy = policy or ReactionDetailPolicy()
        self._now = now
        self._observation_sink = observation_sink

    def _observe(self, kind: str, outcome: str) -> None:
        if self._observation_sink is None:
            return
        try:
            self._observation_sink(kind, outcome)
        except Exception:  # noqa: BLE001 - telemetry must not affect acquisition
            return

    async def refresh_one(  # noqa: PLR0913
        self,
        dialog_id: int,
        message_id: int,
        generation: int,
        *,
        entity: object,
        offset: str | None = None,
        cancellation_event: asyncio.Event | None = None,
        now: int | None = None,
    ) -> ReactionDetailResult:
        when = int(self._now() if now is None else now)
        if cancellation_event is not None and cancellation_event.is_set():
            return ReactionDetailResult("cancelled")
        state = self._expected_state(dialog_id, message_id, generation)
        if state.aggregate_generation is not None and state.aggregate_generation != generation:
            return ReactionDetailResult("stale_writer")
        if state.offset != offset:
            return ReactionDetailResult("stale_writer")
        self._observe("reaction.detail", "attempt")
        fetched = await self._fetch_page(entity, message_id, offset)
        if cancellation_event is not None and cancellation_event.is_set():
            return ReactionDetailResult("cancelled")
        return self._persist_fetch_result(
            state=state,
            fetched=fetched,
            when=when,
        )

    def _expected_state(self, dialog_id: int, message_id: int, generation: int) -> _ExpectedDetailState:
        existing = cast(
            tuple[object, ...] | None,
            self._conn.execute(
                "SELECT aggregate_generation, status, next_offset, staged_count "
                "FROM message_reaction_event_status WHERE dialog_id=? AND message_id=?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        return _ExpectedDetailState(
            dialog_id=dialog_id,
            message_id=message_id,
            requested_generation=generation,
            aggregate_generation=None if existing is None else int(cast(int | str, existing[0])),
            status="stale" if existing is None else str(existing[1]),
            offset=None if existing is None or existing[2] is None else str(existing[2]),
            staged_count=0 if existing is None else int(cast(int | str, existing[3])),
        )

    async def _fetch_page(self, entity: object, message_id: int, offset: str | None) -> ReactionDetailFetchResult:
        try:
            with rpc_scope(TelegramRpcSource.MESSAGE_FACT_REFRESH):
                with acquisition_context(AcquisitionKind.REACTION_SNAPSHOT):
                    return await self._gateway.fetch_reaction_page(
                        entity, message_id, offset=offset, limit=self._policy.page_size
                    )
        except asyncio.CancelledError:
            raise

    def _persist_fetch_result(
        self,
        *,
        state: _ExpectedDetailState,
        fetched: ReactionDetailFetchResult,
        when: int,
    ) -> ReactionDetailResult:
        dialog_id = state.dialog_id
        message_id = state.message_id
        generation = state.requested_generation
        if not fetched.ok:
            assert fetched.failure is not None
            return self._persist_failure(
                dialog_id,
                message_id,
                generation,
                expected_status=state.status,
                expected_offset=state.offset,
                staged_count=state.staged_count,
                failure=fetched.failure,
                when=when,
            )
        assert fetched.page is not None
        next_offset = fetched.page.next_offset
        if next_offset is not None and next_offset == state.offset:
            return self._persist_failure(
                dialog_id,
                message_id,
                generation,
                expected_status=state.status,
                expected_offset=state.offset,
                staged_count=state.staged_count,
                failure=None,
                when=when,
                failure_kind="non_advancing_offset",
            )
        return self._persist_page(
            dialog_id,
            message_id,
            generation,
            expected_status=state.status,
            expected_offset=state.offset,
            events=fetched.page.events,
            next_offset=next_offset,
            when=when,
        )

    def _eligible(self, dialog_id: int) -> bool:
        row = cast(
            tuple[object, ...] | None,
            self._conn.execute(
                "SELECT sd.status, fhe.enabled FROM synced_dialogs sd "
                "LEFT JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id "
                "WHERE sd.dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        return row is not None and str(row[0]) == "synced" and bool(row[1])

    def _cas_status(self, dialog_id: int, message_id: int, generation: int, status: str, offset: str | None) -> bool:
        row = cast(
            tuple[object, ...] | None,
            self._conn.execute(
                "SELECT aggregate_generation, status, next_offset FROM message_reaction_event_status "
                "WHERE dialog_id=? AND message_id=?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        if row is None:
            state = cast(
                tuple[object, ...] | None,
                self._conn.execute(
                    "SELECT generation FROM message_reaction_aggregate_state WHERE dialog_id=? AND message_id=?",
                    (dialog_id, message_id),
                ).fetchone(),
            )
            return (
                status == "stale"
                and offset is None
                and state is not None
                and int(cast(int | str, state[0])) == generation
            )
        return int(cast(int | str, row[0])) == generation and str(row[1]) == status and row[2] == offset

    def _ensure_status(self, dialog_id: int, message_id: int, generation: int, when: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO message_reaction_event_status "
            "(dialog_id, message_id, aggregate_generation, detail_generation, display_generation, "
            "published_generation, checked_at, status, returned_count, staged_count, next_offset, next_attempt_at, failure_kind) "
            "VALUES (?, ?, ?, 0, 0, 0, ?, 'stale', 0, 0, NULL, ?, NULL)",
            (dialog_id, message_id, generation, when, when),
        )

    def _begin_persistence(self) -> None:
        """Require the dedicated connection to be idle before taking the write lock."""
        if self._conn.in_transaction:
            raise RuntimeError("reaction detail persistence requires an idle dedicated connection")
        self._conn.execute("BEGIN IMMEDIATE")

    def _discard_staged_page(self, dialog_id: int, message_id: int, generation: int, first_ordinal: int) -> None:
        self._conn.execute(
            "DELETE FROM message_reaction_events WHERE dialog_id=? AND message_id=? "
            "AND detail_generation=? AND page_ordinal>=?",
            (dialog_id, message_id, generation, first_ordinal),
        )

    @staticmethod
    def _positive_retry_after(failure: object | None) -> int | None:
        value = getattr(failure, "retry_after", None)
        return value if isinstance(value, int) and value > 0 else None

    @staticmethod
    def _is_terminal_failure(failure: GatewayFailure | None) -> bool:
        return (
            failure is not None
            and not failure.retryable
            and failure.kind in (GatewayFailureKind.INVALID_TARGET, GatewayFailureKind.ACCESS_LOST)
        )

    @staticmethod
    def _failure_status(staged_count: int, expected_status: str, terminal: bool) -> str:
        if terminal:
            return "unavailable"
        return "partial" if staged_count > 0 and expected_status == "partial" else "unavailable"

    def _failure_next_attempt_at(self, when: int, retry_after: int | None, terminal: bool) -> int | None:
        if terminal:
            return None
        return when + (retry_after or self._policy.unavailable_retry_seconds)

    def _persist_page(  # noqa: PLR0913
        self,
        dialog_id: int,
        message_id: int,
        generation: int,
        *,
        expected_status: str,
        expected_offset: str | None,
        events: tuple[ReactionEvent, ...],
        next_offset: str | None,
        when: int,
    ) -> ReactionDetailResult:
        self._begin_persistence()
        with self._conn:
            if not self._eligible(dialog_id):
                return ReactionDetailResult("ineligible")
            if not self._cas_status(dialog_id, message_id, generation, expected_status, expected_offset):
                return ReactionDetailResult("stale_writer")
            self._ensure_status(dialog_id, message_id, generation, when)
            row = cast(
                tuple[object, ...] | None,
                self._conn.execute(
                    "SELECT staged_count FROM message_reaction_event_status WHERE dialog_id=? AND message_id=?",
                    (dialog_id, message_id),
                ).fetchone(),
            )
            staged_count = 0 if row is None else int(cast(int | str, row[0]))
            self._conn.executemany(
                "INSERT INTO message_reaction_events "
                "(dialog_id, message_id, reactor_id, emoji, reacted_at, fetched_at, detail_generation, page_ordinal, display_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                [
                    (
                        dialog_id,
                        message_id,
                        event.reactor_id,
                        event.emoji,
                        event.reacted_at,
                        when,
                        generation,
                        staged_count + index,
                    )
                    for index, event in enumerate(events)
                ],
            )
            total = staged_count + len(events)
            if next_offset is None:
                self._conn.execute(
                    "UPDATE message_reaction_events SET display_generation=? "
                    "WHERE dialog_id=? AND message_id=? AND detail_generation=?",
                    (generation, dialog_id, message_id, generation),
                )
                self._conn.execute(
                    "DELETE FROM message_reaction_events WHERE dialog_id=? AND message_id=? "
                    "AND display_generation > 0 AND detail_generation != ?",
                    (dialog_id, message_id, generation),
                )
                status_update = self._conn.execute(
                    "UPDATE message_reaction_event_status SET detail_generation=?, display_generation=?, "
                    "published_generation=?, checked_at=?, status='complete', returned_count=?, staged_count=0, "
                    "next_offset=NULL, next_attempt_at=NULL, failure_kind=NULL WHERE dialog_id=? AND message_id=? "
                    "AND aggregate_generation=? AND status=? AND next_offset IS ?",
                    (
                        generation,
                        generation,
                        generation,
                        when,
                        total,
                        dialog_id,
                        message_id,
                        generation,
                        expected_status,
                        expected_offset,
                    ),
                )
                if status_update.rowcount != 1:
                    self._discard_staged_page(dialog_id, message_id, generation, staged_count)
                    self._conn.rollback()
                    return ReactionDetailResult("stale_writer")
                self._observe("reaction.detail", "complete")
                return ReactionDetailResult("complete", fetched_pages=1)
            status_update = self._conn.execute(
                "UPDATE message_reaction_event_status SET detail_generation=?, checked_at=?, status='partial', "
                "returned_count=?, staged_count=?, next_offset=?, next_attempt_at=?, failure_kind=NULL "
                "WHERE dialog_id=? AND message_id=? AND aggregate_generation=? AND status=? AND next_offset IS ?",
                (
                    generation,
                    when,
                    total,
                    total,
                    next_offset,
                    when,
                    dialog_id,
                    message_id,
                    generation,
                    expected_status,
                    expected_offset,
                ),
            )
            if status_update.rowcount != 1:
                self._discard_staged_page(dialog_id, message_id, generation, staged_count)
                self._conn.rollback()
                return ReactionDetailResult("stale_writer")
            self._observe("reaction.detail", "partial")
            return ReactionDetailResult("partial", fetched_pages=1, next_offset=next_offset)

    def _persist_failure(  # noqa: PLR0913
        self,
        dialog_id: int,
        message_id: int,
        generation: int,
        *,
        expected_status: str,
        expected_offset: str | None,
        staged_count: int,
        failure: GatewayFailure | None,
        when: int,
        failure_kind: str | None = None,
    ) -> ReactionDetailResult:
        kind = failure_kind or str(getattr(getattr(failure, "kind", None), "value", "unavailable"))
        retry_after = self._positive_retry_after(failure)
        terminal = self._is_terminal_failure(failure)
        status = self._failure_status(staged_count, expected_status, terminal)
        next_attempt_at = self._failure_next_attempt_at(when, retry_after, terminal)
        self._begin_persistence()
        with self._conn:
            if not self._eligible(dialog_id):
                return ReactionDetailResult("ineligible")
            if not self._cas_status(dialog_id, message_id, generation, expected_status, expected_offset):
                return ReactionDetailResult("stale_writer")
            if terminal:
                self._conn.execute(
                    "DELETE FROM message_reaction_events WHERE dialog_id=? AND message_id=? "
                    "AND detail_generation=? AND display_generation=0",
                    (dialog_id, message_id, generation),
                )
            self._ensure_status(dialog_id, message_id, generation, when)
            status_update = self._conn.execute(
                "UPDATE message_reaction_event_status SET checked_at=?, status=?, next_attempt_at=?, "
                "next_offset=?, staged_count=?, failure_kind=? "
                "WHERE dialog_id=? AND message_id=? AND aggregate_generation=? AND status=? AND next_offset IS ?",
                (
                    when,
                    status,
                    next_attempt_at,
                    None if terminal else expected_offset,
                    0 if terminal else staged_count,
                    kind,
                    dialog_id,
                    message_id,
                    generation,
                    expected_status,
                    expected_offset,
                ),
            )
            if status_update.rowcount != 1:
                self._conn.rollback()
                return ReactionDetailResult("stale_writer")
            self._observe("reaction.detail", "terminal_unavailable" if terminal else status)
        return ReactionDetailResult(
            status,
            next_offset=None if terminal else expected_offset,
            failure_kind=kind,
            retry_after=retry_after,
        )
