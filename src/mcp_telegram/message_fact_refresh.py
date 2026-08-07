"""Background acquisition for optional Telegram message facts.

Read tools must remain SQLite-only.  This module owns the daemon-side Telegram
refresh lane that materializes optional facts into local tables for later
projection by read tools.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast

from .models import ReadMessage
from .reactions.refresh import ReactionFreshener
from .telegram_fact_queries import enrich_read_at
from .telegram_reading import TelegramReadReceiptGateway

logger = logging.getLogger(__name__)


_REACTION_CANDIDATES_SQL = """
SELECT m.dialog_id, m.message_id
FROM messages m
JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
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


@dataclass(frozen=True, slots=True)
class MessageFactRefreshPolicy:
    """Bounded daemon-side policy for optional Telegram fact acquisition."""

    interval_seconds: float
    max_messages_per_cycle: int
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
    if policy.max_messages_per_cycle <= 0:
        return MessageFactRefreshResult(0, 0, 0)

    checked_at = int(time.time() if now is None else now)
    reaction_rows = _reaction_candidates(
        deps.conn,
        stale_before_utc=checked_at - policy.reaction_ttl_seconds,
        limit=policy.max_messages_per_cycle,
    )
    reaction_refreshed = 0
    reaction_groups = _group_message_ids(reaction_rows)
    for index, (dialog_id, message_ids) in enumerate(reaction_groups.items()):
        freshness = await deps.reaction_freshener.refresh(dialog_id, dialog_id, message_ids)
        reaction_refreshed += freshness.refreshed_count
        if shutdown_event is not None and index < len(reaction_groups) - 1:
            await _interruptible_pause(shutdown_event, policy.pause_seconds)

    read_at_limit = max(0, policy.max_messages_per_cycle - len(reaction_rows))
    read_at_messages = _read_at_candidates(
        deps.conn,
        stale_before_utc=checked_at - policy.read_at_ttl_seconds,
        limit=read_at_limit,
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


async def run_message_fact_refresh_loop(
    conn: sqlite3.Connection,
    reaction_freshener: ReactionFreshener,
    read_receipt_gateway: TelegramReadReceiptGateway,
    shutdown_event: asyncio.Event,
    policy: MessageFactRefreshPolicy,
) -> None:
    """Run low-priority optional fact acquisition until shutdown."""
    if policy.max_messages_per_cycle <= 0:
        logger.info("message_fact_refresh_loop disabled — max_messages_per_cycle=%d", policy.max_messages_per_cycle)
        return

    while not shutdown_event.is_set():
        try:
            result = await refresh_message_facts_once(
                MessageFactRefreshDeps(conn, reaction_freshener, read_receipt_gateway),
                policy,
                shutdown_event=shutdown_event,
            )
            logger.info(
                "message_fact_refresh_cycle complete — reaction_candidates=%d reaction_refreshed=%d "
                "read_at_candidates=%d",
                result.reaction_candidates,
                result.reaction_refreshed,
                result.read_at_candidates,
            )
        except Exception:
            logger.warning("message_fact_refresh_cycle failed", exc_info=True)
        await _interruptible_pause(shutdown_event, policy.interval_seconds)
