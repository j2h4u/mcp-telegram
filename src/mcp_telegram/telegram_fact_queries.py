"""Persistence helpers for daemon-owned Telegram event facts.

These helpers deliberately keep event availability separate from event time:
an unavailable/forbidden Telegram RPC must not look like a timestamped event.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time
from collections.abc import Sequence
from typing import cast

from .models import DialogType, ReadMessage, ReadReactionEvent
from .telegram_demand import RpcAttemptBudgetExhaustedError
from .telegram_gateway import CATCHABLE_GATEWAY_FAILURES
from .telegram_reading import ReadDateFetchResult, TelegramReadReceiptGateway
from .telegram_rpc_scheduler import RpcAdmissionClosedError


def reaction_event_projection(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
) -> tuple[dict[int, tuple[ReadReactionEvent, ...]], dict[int, str]]:
    """Load daemon-owned individual reaction facts and completeness status.

    Aggregate rows deliberately are not joined here: their contract contains
    only emoji/count.  Missing v28 tables or status rows are represented as
    ``unavailable`` rather than causing a read response to fail.
    """
    if not message_ids:
        return {}, {}
    placeholders = ",".join("?" for _ in message_ids)
    try:
        event_rows = cast(
            list[tuple[object, ...]],
            conn.execute(
                f"SELECT e.message_id, e.reactor_id, e.emoji, e.reacted_at "
                f"FROM message_reaction_events e JOIN message_reaction_event_status s "
                f"ON s.dialog_id=e.dialog_id AND s.message_id=e.message_id "
                f"WHERE e.dialog_id = ? AND e.message_id IN ({placeholders}) "
                "AND e.display_generation = s.display_generation AND e.display_generation > 0 "
                "ORDER BY e.message_id, e.event_id",
                [dialog_id, *message_ids],
            ).fetchall(),
        )
        status_rows = cast(
            list[tuple[object, ...]],
            conn.execute(
                f"SELECT message_id, status FROM message_reaction_event_status "
                f"WHERE dialog_id = ? AND message_id IN ({placeholders})",
                [dialog_id, *message_ids],
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return {}, {}

    events: dict[int, tuple[ReadReactionEvent, ...]] = {}
    grouped: dict[int, list[ReadReactionEvent]] = {}
    for row in event_rows:
        grouped.setdefault(int(cast(int | str, row[0])), []).append(
            ReadReactionEvent(
                reactor_id=None if row[1] is None else int(cast(int | str, row[1])),
                emoji=str(row[2]),
                reacted_at=None if row[3] is None else int(cast(int | str, row[3])),
            )
        )
    for message_id, values in grouped.items():
        events[message_id] = tuple(values)
    statuses = {int(cast(int | str, row[0])): str(row[1]) for row in status_rows}
    return events, statuses


def enrich_reaction_events(
    conn: sqlite3.Connection,
    dialog_id: int,
    messages: Sequence[ReadMessage],
) -> list[ReadMessage]:
    """Project stored individual reaction facts without contacting Telegram."""
    event_map, status_map = reaction_event_projection(conn, dialog_id, [message.id for message in messages])
    return [
        dataclasses.replace(
            message,
            reaction_events=event_map.get(message.id, ()),
            reaction_events_status=status_map.get(message.id, "unavailable"),
        )
        for message in messages
    ]


async def enrich_read_at(  # noqa: PLR0913
    conn: sqlite3.Connection,
    gateway: TelegramReadReceiptGateway | None,
    dialog_id: int,
    messages: Sequence[ReadMessage],
    *,
    dialog_type: str | DialogType | None,
    read_at_ttl_seconds: int,
    checked_at: int | None = None,
    cycle_counts: list[int] | None = None,
) -> list[ReadMessage]:
    """Best-effort enrich own outgoing User-DM messages with Telegram dates.

    ``read_at`` is an event timestamp, never a probe timestamp.  Probe state is
    kept in ``message_read_facts`` so missing/forbidden dates remain nullable
    and are not retried until the bounded TTL expires.
    """
    _validate_read_at_ttl_seconds(read_at_ttl_seconds)
    if gateway is None or DialogType.parse(dialog_type) != DialogType.USER:
        return list(messages)
    candidate_ids = _outgoing_candidate_ids(messages, dialog_id)
    if not candidate_ids:
        return list(messages)
    now = int(checked_at if checked_at is not None else time.time())
    counts = await _refresh_stale_read_at_facts(
        conn,
        gateway,
        dialog_id,
        candidate_ids,
        stale_before_utc=now - read_at_ttl_seconds,
        checked_at=now,
    )
    if cycle_counts is not None:
        cycle_counts.extend(counts)
    values = read_at_map(conn, dialog_id, candidate_ids)
    return [dataclasses.replace(message, read_at=values.get(message.message_id)) for message in messages]


def _validate_read_at_ttl_seconds(read_at_ttl_seconds: int) -> None:
    """Reject invalid cache TTLs before the enrichment shortcut paths."""
    if isinstance(read_at_ttl_seconds, bool) or not isinstance(read_at_ttl_seconds, int) or read_at_ttl_seconds < 1:
        raise ValueError("read_at_ttl_seconds must be an integer >= 1")


def _outgoing_candidate_ids(messages: Sequence[ReadMessage], dialog_id: int) -> list[int]:
    """Return User-DM outgoing ids in their source-message order."""
    return [message.message_id for message in messages if message.out == 1 and message.dialog_id == dialog_id]


async def _refresh_stale_read_at_facts(  # noqa: PLR0913
    conn: sqlite3.Connection,
    gateway: TelegramReadReceiptGateway,
    dialog_id: int,
    message_ids: Sequence[int],
    *,
    stale_before_utc: int,
    checked_at: int,
) -> tuple[int, int, int, bool]:
    """Refresh stale probes, retaining committed earlier facts on a later failure."""
    complete = missing = unavailable = 0
    measurement_complete = True
    for message_id in stale_read_at_ids(conn, dialog_id, message_ids, stale_before_utc):
        try:
            result = await gateway.fetch_outbox_read_date(dialog_id, message_id)
        except RpcAdmissionClosedError, RpcAttemptBudgetExhaustedError:
            raise
        except CATCHABLE_GATEWAY_FAILURES:
            # A single privacy/retention failure must not break list/search.
            result = ReadDateFetchResult(status="unavailable")
        read_at = result.read_at if result.status == "complete" else None
        if result.status == "complete":
            complete += 1
        elif result.status == "missing":
            missing += 1
        elif result.status == "unavailable":
            unavailable += 1
        try:
            persist_read_at(
                conn,
                dialog_id,
                message_id,
                read_at=read_at,
                checked_at=checked_at,
                status=result.status,
            )
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            # Keep pre-v28 read paths usable while the daemon is upgrading.
            measurement_complete = False
            break
    return complete, missing, unavailable, measurement_complete


def persist_read_at(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    read_at: int | None,
    checked_at: int,
    status: str,
) -> None:
    """Store one outbox-read-date probe with terminal and timestamp fences."""

    if status not in {"complete", "missing", "unavailable"}:
        raise ValueError(f"unsupported read-date status: {status}")
    if status == "complete" and read_at is None:
        raise ValueError("complete read-date result requires a non-null read_at")

    with conn:
        conn.execute(
            "INSERT INTO message_read_facts "
            "(dialog_id, message_id, read_at, checked_at, status) "
            "SELECT ?, ?, ?, ?, ? WHERE EXISTS ("
            "SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1) "
            "ON CONFLICT(dialog_id, message_id) DO UPDATE SET "
            "read_at = excluded.read_at, checked_at = excluded.checked_at, status = excluded.status "
            "WHERE NOT (message_read_facts.status = 'complete' AND message_read_facts.read_at IS NOT NULL) "
            "AND ("
            "(excluded.status = 'complete' AND excluded.read_at IS NOT NULL) "
            "OR excluded.checked_at > message_read_facts.checked_at"
            ")",
            (dialog_id, message_id, read_at, checked_at, status, dialog_id),
        )


def read_at_map(conn: sqlite3.Connection, dialog_id: int, message_ids: Sequence[int]) -> dict[int, int | None]:
    """Return persisted read dates; absent rows remain cache misses.

    A missing table is tolerated for lightweight/legacy test databases and
    pre-migration read paths.
    """

    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    try:
        rows = cast(
            list[tuple[object, ...]],
            conn.execute(
                f"SELECT message_id, read_at FROM message_read_facts "
                f"WHERE dialog_id = ? AND message_id IN ({placeholders})",
                [dialog_id, *message_ids],
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return {}
    values: dict[int, int | None] = {}
    for row in rows:
        message_id = int(cast(int | str, row[0]))
        values[message_id] = None if row[1] is None else int(cast(int | str, row[1]))
    return values


def stale_read_at_ids(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
    stale_before_utc: int,
) -> list[int]:
    """Return absent or retryable stale ids; terminal facts are never returned."""

    if not message_ids:
        return []
    placeholders = ",".join("?" for _ in message_ids)
    try:
        rows = cast(
            list[tuple[object, ...]],
            conn.execute(
                f"SELECT message_id, checked_at FROM message_read_facts "
                f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
                "AND ((status = 'complete' AND read_at IS NOT NULL) OR checked_at > ?)",
                [dialog_id, *message_ids, stale_before_utc],
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return list(dict.fromkeys(message_ids))
    fresh = {int(cast(int | str, row[0])) for row in rows}
    selected: list[int] = []
    for message_id in message_ids:
        if message_id not in fresh and message_id not in selected:
            selected.append(message_id)
    return selected
