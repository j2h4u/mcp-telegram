"""Background acquisition for optional Telegram message facts.

Read tools must remain SQLite-only.  This module owns the daemon-side Telegram
refresh lane that materializes optional facts into local tables for later
projection by read tools.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import cast

from .models import ReadMessage
from .reactions.refresh import ReactionFreshener
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
SELECT m.dialog_id, m.message_id
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
WHERE sd.status = 'synced'
  AND EXISTS (
      SELECT 1
      FROM message_reactions r
      WHERE r.dialog_id = m.dialog_id
        AND r.message_id = m.message_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM message_reactions_freshness f
      WHERE f.dialog_id = m.dialog_id
        AND f.message_id = m.message_id
        AND f.checked_at > ?
  )
ORDER BY m.sent_at DESC, m.dialog_id, m.message_id
LIMIT ?
"""


_READ_AT_CANDIDATES_SQL = """
SELECT m.dialog_id, m.message_id, m.sent_at
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
JOIN entities e ON e.id = m.dialog_id
WHERE sd.status = 'synced'
  AND lower(e.type) = 'user'
  AND m.out = 1
  AND NOT EXISTS (
      SELECT 1
      FROM message_read_facts f
      WHERE f.dialog_id = m.dialog_id
        AND f.message_id = m.message_id
        AND f.checked_at > ?
  )
ORDER BY m.sent_at DESC, m.dialog_id, m.message_id
LIMIT ?
"""


_NEXT_REACTION_RELEASE_SQL = """
SELECT MIN(CASE WHEN f.checked_at IS NULL THEN 0 ELSE f.checked_at + ? END)
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1
LEFT JOIN message_reactions_freshness f
  ON f.dialog_id = m.dialog_id AND f.message_id = m.message_id
WHERE sd.status = 'synced'
  AND EXISTS (
      SELECT 1
      FROM message_reactions r
      WHERE r.dialog_id = m.dialog_id
        AND r.message_id = m.message_id
  )
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

    interval_seconds: float
    reaction_max_messages_per_cycle: int
    read_at_max_messages_per_cycle: int
    pause_seconds: float
    reaction_ttl_seconds: int
    read_at_ttl_seconds: int


@dataclass(frozen=True, slots=True)
class MessageFactRefreshResult:
    """One background refresh cycle summary."""

    reaction_candidates: int
    reaction_refreshed: int
    read_at_candidates: int


@dataclass(frozen=True, slots=True)
class MessageFactRefreshDeps:
    """Infrastructure dependencies for one optional fact refresh cycle."""

    conn: sqlite3.Connection
    reaction_freshener: ReactionFreshener
    read_receipt_gateway: TelegramReadReceiptGateway


def _next_release_at(conn: sqlite3.Connection, query: str, ttl_seconds: int) -> float | None:
    row = cast(tuple[object] | None, conn.execute(query, (ttl_seconds,)).fetchone())
    value = None if row is None else row[0]
    return None if value is None else float(cast(int | float, value))


class MessageFactRefreshDemandAdapter(DurableDemandAdapter):
    """Read the existing optional-fact candidate state for PR1 shadow mode.

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
            reaction_release = _next_release_at(
                self._deps.conn,
                _NEXT_REACTION_RELEASE_SQL,
                self._policy.reaction_ttl_seconds,
            )
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
    """Read the durable read-position reconciliation state in shadow mode.

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
) -> list[tuple[int, int]]:
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(_REACTION_CANDIDATES_SQL, (stale_before_utc, limit)).fetchall(),
    )
    return [cast(tuple[int, int], _row_ints(row)) for row in rows]


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


async def refresh_message_facts_once(
    deps: MessageFactRefreshDeps,
    policy: MessageFactRefreshPolicy,
    *,
    now: int | None = None,
    shutdown_event: asyncio.Event | None = None,
) -> MessageFactRefreshResult:
    """Refresh a bounded batch of optional message facts into SQLite."""
    if policy.reaction_max_messages_per_cycle <= 0 and policy.read_at_max_messages_per_cycle <= 0:
        return MessageFactRefreshResult(0, 0, 0)

    checked_at = int(time.time() if now is None else now)
    reaction_rows = _reaction_candidates(
        deps.conn,
        stale_before_utc=checked_at - policy.reaction_ttl_seconds,
        limit=policy.reaction_max_messages_per_cycle,
    )
    reaction_refreshed = 0
    reaction_groups = _group_message_ids(reaction_rows)
    for index, (dialog_id, message_ids) in enumerate(reaction_groups.items()):
        freshness = await deps.reaction_freshener.refresh(dialog_id, dialog_id, message_ids)
        reaction_refreshed += freshness.refreshed_count
        if shutdown_event is not None and index < len(reaction_groups) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)

    read_at_messages = _read_at_candidates(
        deps.conn,
        stale_before_utc=checked_at - policy.read_at_ttl_seconds,
        limit=policy.read_at_max_messages_per_cycle,
    )
    read_at_groups = _group_messages(read_at_messages)
    for index, (dialog_id, messages) in enumerate(read_at_groups.items()):
        await enrich_read_at(
            deps.conn,
            deps.read_receipt_gateway,
            dialog_id,
            messages,
            dialog_type="user",
            read_at_ttl_seconds=policy.read_at_ttl_seconds,
            checked_at=checked_at,
        )
        if shutdown_event is not None and index < len(read_at_groups) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)

    return MessageFactRefreshResult(
        reaction_candidates=len(reaction_rows),
        reaction_refreshed=reaction_refreshed,
        read_at_candidates=len(read_at_messages),
    )
