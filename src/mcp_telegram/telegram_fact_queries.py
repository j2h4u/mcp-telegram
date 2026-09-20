"""Persistence helpers for daemon-owned Telegram event facts.

These helpers deliberately keep event availability separate from event time:
an unavailable/forbidden Telegram RPC must not look like a timestamped event.

This module owns exact read-date fact persistence and cutoff classification SQL;
general message queries remain in their canonical query modules.
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
from .telegram_read_receipts import classify_read_date_exception
from .telegram_reading import (
    ReadDateFetchResult,
    ReadDateReason,
    TelegramReadReceiptGateway,
    is_read_date_reason_retryable,
    normalize_read_date_reason,
)
from .telegram_rpc_scheduler import RpcAdmissionClosedError

_READ_DATE_EXPIRY_INSERT_SQL = (
    "INSERT INTO message_read_facts "
    "(dialog_id, message_id, read_at, checked_at, status, reason, next_attempt_at) "
    "SELECT m.dialog_id, m.message_id, NULL, ?, 'unavailable', 'message_too_old', NULL "
    "FROM messages m "
    "JOIN synced_dialogs sd ON sd.dialog_id=m.dialog_id "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
    "JOIN entities e ON e.id=m.dialog_id "
    "WHERE sd.status='synced' AND lower(e.type)='user' AND m.out=1 AND m.is_deleted=0 "
    "AND sd.read_outbox_max_id IS NOT NULL AND m.message_id <= sd.read_outbox_max_id "
    "AND m.sent_at <= ? "
    "AND NOT (m.dialog_id=? AND m.message_id=?) "
    "AND NOT EXISTS (SELECT 1 FROM message_read_facts f "
    "WHERE f.dialog_id=m.dialog_id AND f.message_id=m.message_id)"
)

# Cutoff propagation records current exact-date availability: retryable and
# legacy "not read yet" rows below a proven cutoff become unavailable.
_READ_DATE_EXPIRY_UPDATE_SQL = (
    "UPDATE message_read_facts SET read_at=NULL, checked_at=?, status='unavailable', "
    "reason='message_too_old', next_attempt_at=NULL "
    "WHERE reason IN ('legacy', 'date_omitted', 'message_not_read_yet', 'flood_wait', 'transient') "
    "AND EXISTS (SELECT 1 FROM messages m "
    "JOIN synced_dialogs sd ON sd.dialog_id=m.dialog_id "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id=sd.dialog_id AND fhe.enabled=1 "
    "JOIN entities e ON e.id=m.dialog_id "
    "WHERE sd.status='synced' AND lower(e.type)='user' AND m.out=1 AND m.is_deleted=0 "
    "AND sd.read_outbox_max_id IS NOT NULL AND m.message_id <= sd.read_outbox_max_id "
    "AND m.sent_at <= ? "
    "AND NOT (m.dialog_id=? AND m.message_id=?) "
    "AND m.dialog_id=message_read_facts.dialog_id "
    "AND m.message_id=message_read_facts.message_id)"
)


class InvalidReadDateWitnessError(ValueError):
    """A Telegram age witness cannot be used for a future local message date."""


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
    cycle_reason_counts: dict[str, int] | None = None,
    cycle_metrics: dict[str, int] | None = None,
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
        [message for message in messages if message.message_id in candidate_ids],
        stale_before_utc=now,
        checked_at=now,
        read_at_ttl_seconds=read_at_ttl_seconds,
        cycle_reason_counts=cycle_reason_counts,
        cycle_metrics=cycle_metrics,
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


async def _fetch_read_date_result(
    gateway: TelegramReadReceiptGateway,
    dialog_id: int,
    message_id: int,
) -> ReadDateFetchResult:
    try:
        result = await gateway.fetch_outbox_read_date(dialog_id, message_id)
    except RpcAdmissionClosedError, RpcAttemptBudgetExhaustedError:
        raise
    except CATCHABLE_GATEWAY_FAILURES as exc:
        return classify_read_date_exception(exc)
    if result.status not in {"complete", "missing", "unavailable"}:
        raise ValueError(f"unsupported read-date status: {result.status}")
    if result.status == "complete" and result.read_at is None:
        raise ValueError("complete read-date result requires a non-null read_at")
    return result


def _read_date_result_counts(result: ReadDateFetchResult) -> tuple[int, int, int]:
    return {
        "complete": (1, 0, 0),
        "missing": (0, 1, 0),
        "unavailable": (0, 0, 1),
    }[result.status]


def _read_date_retry_deadline(
    result: ReadDateFetchResult,
    reason: ReadDateReason,
    *,
    checked_at: int,
    read_at_ttl_seconds: int,
) -> int | None:
    if not is_read_date_reason_retryable(reason):
        return None
    retry_after = result.failure.retry_after if result.failure is not None else None
    return checked_at + max(read_at_ttl_seconds, retry_after or 0)


def _persist_read_date_result(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    sent_at: int,
    result: ReadDateFetchResult,
    *,
    reason: ReadDateReason,
    checked_at: int,
    next_attempt_at: int | None,
) -> int:
    if reason is ReadDateReason.MESSAGE_TOO_OLD:
        return _persist_message_too_old(
            conn,
            dialog_id,
            message_id,
            sent_at=sent_at,
            checked_at=checked_at,
        )
    persist_read_at(
        conn,
        dialog_id,
        message_id,
        read_at=result.read_at if result.status == "complete" else None,
        checked_at=checked_at,
        status=result.status,
        reason=reason,
        next_attempt_at=next_attempt_at,
    )
    return 0


def _has_read_date_fact(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM message_read_facts WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        ).fetchone()
        is not None
    )


def _read_date_rpc_still_eligible(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    sent_at: int,
    stale_before_utc: int,
) -> tuple[bool, bool, bool]:
    """Recheck cutoff and fact freshness immediately before one Telegram call."""
    state = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT expired_through_sent_at FROM read_date_expiry_state WHERE singleton=1").fetchone(),
    )
    if state is None:
        raise RuntimeError("read_date_expiry_state singleton is missing")
    cutoff = None if state[0] is None else int(cast(int | str, state[0]))
    if cutoff is not None and sent_at <= cutoff:
        return False, True, False
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT next_attempt_at FROM message_read_facts WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if row is None:
        return True, False, False
    next_attempt_at = row[0]
    return (
        next_attempt_at is not None and int(cast(int | str, next_attempt_at)) <= stale_before_utc,
        False,
        True,
    )


def _persist_message_too_old(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    sent_at: int,
    checked_at: int,
) -> int:
    """Persist one age witness in a transaction owned entirely by this helper.

    Callers must finish any prior local transaction before invoking this helper;
    Telegram I/O always happens before this transaction begins.
    """
    if conn.in_transaction:
        raise RuntimeError("_persist_message_too_old requires no open transaction")
    if sent_at > checked_at:
        raise InvalidReadDateWitnessError("read-date witness sent_at cannot be later than checked_at")
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = cast(
            tuple[object, ...],
            conn.execute("SELECT expired_through_sent_at FROM read_date_expiry_state WHERE singleton=1").fetchone(),
        )
        current_cutoff = None if state[0] is None else int(cast(int | str, state[0]))
        cutoff = sent_at if current_cutoff is None else max(current_cutoff, sent_at)
        if current_cutoff is None or sent_at >= current_cutoff:
            conn.execute(
                "UPDATE read_date_expiry_state SET expired_through_sent_at=?, observed_at=?, "
                "witness_dialog_id=?, witness_message_id=? WHERE singleton=1",
                (cutoff, checked_at, dialog_id, message_id),
            )
        else:
            conn.execute(
                "UPDATE read_date_expiry_state SET observed_at=? WHERE singleton=1",
                (checked_at,),
            )

        inserted = conn.execute(
            _READ_DATE_EXPIRY_INSERT_SQL,
            (checked_at, cutoff, dialog_id, message_id),
        ).rowcount
        updated = conn.execute(
            _READ_DATE_EXPIRY_UPDATE_SQL,
            (checked_at, cutoff, dialog_id, message_id),
        ).rowcount
        _persist_read_at_v70(
            conn,
            dialog_id,
            message_id,
            read_at=None,
            checked_at=checked_at,
            status="unavailable",
            reason=ReadDateReason.MESSAGE_TOO_OLD,
            next_attempt_at=None,
        )
        conn.commit()
        return int(inserted or 0) + int(updated or 0)
    except BaseException:
        conn.rollback()
        raise


async def _refresh_stale_read_at_facts(  # noqa: PLR0913
    conn: sqlite3.Connection,
    gateway: TelegramReadReceiptGateway,
    dialog_id: int,
    messages: Sequence[ReadMessage],
    *,
    stale_before_utc: int,
    checked_at: int,
    read_at_ttl_seconds: int,
    cycle_reason_counts: dict[str, int] | None = None,
    cycle_metrics: dict[str, int] | None = None,
) -> tuple[int, int, int]:
    """Refresh stale probes, retaining committed earlier facts on a later failure."""
    complete = missing = unavailable = 0
    message_by_id = {message.message_id: message for message in messages}
    candidate_ids = stale_read_at_ids(conn, dialog_id, tuple(message_by_id), stale_before_utc)
    for message_id in candidate_ids:
        message = message_by_id[message_id]
        eligible, cutoff_skipped, fresh_skipped = _read_date_rpc_still_eligible(
            conn,
            dialog_id,
            message_id,
            sent_at=message.sent_at,
            stale_before_utc=stale_before_utc,
        )
        if not eligible:
            _record_cutoff_skip(cycle_metrics, cutoff_skipped)
            _record_fresh_skip(cycle_metrics, fresh_skipped)
            continue
        attempt_kind = _read_date_attempt_kind(conn, dialog_id, message_id)
        result = await _fetch_read_date_result(gateway, dialog_id, message_id)
        _record_read_date_attempt(cycle_metrics, attempt_kind)
        result_complete, result_missing, result_unavailable = _persist_fetched_read_date(
            conn,
            dialog_id,
            message,
            result,
            checked_at=checked_at,
            read_at_ttl_seconds=read_at_ttl_seconds,
            cycle_reason_counts=cycle_reason_counts,
            cycle_metrics=cycle_metrics,
        )
        complete += result_complete
        missing += result_missing
        unavailable += result_unavailable
    return complete, missing, unavailable


def _record_cutoff_skip(metrics: dict[str, int] | None, cutoff_skipped: bool) -> None:
    if metrics is not None and cutoff_skipped:
        metrics["cutoff_skipped"] = metrics.get("cutoff_skipped", 0) + 1


def _record_fresh_skip(metrics: dict[str, int] | None, fresh_skipped: bool) -> None:
    if metrics is not None and fresh_skipped:
        metrics["fresh_skipped"] = metrics.get("fresh_skipped", 0) + 1


def _read_date_attempt_kind(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
) -> str:
    return "retry_attempts" if _has_read_date_fact(conn, dialog_id, message_id) else "first_attempts"


def _record_read_date_attempt(
    metrics: dict[str, int] | None,
    attempt_kind: str,
) -> None:
    if metrics is None:
        return
    metrics["rpc_attempts"] = metrics.get("rpc_attempts", 0) + 1
    metrics[attempt_kind] = metrics.get(attempt_kind, 0) + 1


def _quarantine_invalid_read_date_witness(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    checked_at: int,
) -> None:
    """Store a malformed age witness through the ordinary monotonic fence."""
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT checked_at FROM message_read_facts WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        ).fetchone(),
    )
    persisted_checked_at = checked_at
    if row is not None:
        persisted_checked_at = max(persisted_checked_at, int(cast(int | str, row[0])) + 1)
    persist_read_at(
        conn,
        dialog_id,
        message_id,
        read_at=None,
        checked_at=persisted_checked_at,
        status="unavailable",
        reason=ReadDateReason.MESSAGE_TOO_OLD,
        next_attempt_at=None,
    )


def _persist_fetched_read_date(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message: ReadMessage,
    result: ReadDateFetchResult,
    *,
    checked_at: int,
    read_at_ttl_seconds: int,
    cycle_reason_counts: dict[str, int] | None,
    cycle_metrics: dict[str, int] | None,
) -> tuple[int, int, int]:
    reason = normalize_read_date_reason(result)
    if cycle_reason_counts is not None:
        cycle_reason_counts[reason.value] = cycle_reason_counts.get(reason.value, 0) + 1
    next_attempt_at = _read_date_retry_deadline(
        result,
        reason,
        checked_at=checked_at,
        read_at_ttl_seconds=read_at_ttl_seconds,
    )
    try:
        locally_classified = _persist_read_date_result(
            conn,
            dialog_id,
            message.message_id,
            message.sent_at,
            result,
            reason=reason,
            checked_at=checked_at,
            next_attempt_at=next_attempt_at,
        )
    except InvalidReadDateWitnessError:
        # Telegram explicitly returned MESSAGE_TOO_OLD. Quarantine only this
        # malformed local witness; it must not advance or classify globally.
        _quarantine_invalid_read_date_witness(conn, dialog_id, message.message_id, checked_at=checked_at)
        locally_classified = 0
        if cycle_metrics is not None:
            cycle_metrics["invalid_cutoff_witnesses"] = cycle_metrics.get("invalid_cutoff_witnesses", 0) + 1
    if cycle_metrics is not None:
        cycle_metrics["locally_classified"] = cycle_metrics.get("locally_classified", 0) + locally_classified
        if reason is ReadDateReason.MESSAGE_TOO_OLD:
            cycle_metrics["message_too_old_responses"] = cycle_metrics.get("message_too_old_responses", 0) + 1
    return _read_date_result_counts(result)


def _normalize_persist_reason(status: str, reason: ReadDateReason | str | None) -> ReadDateReason:
    normalized = (
        ReadDateReason.RESOLVED
        if status == "complete"
        else ReadDateReason(reason)
        if reason is not None
        else ReadDateReason.DATE_OMITTED
        if status == "missing"
        else ReadDateReason.TRANSIENT
    )
    valid_by_status = {
        "complete": frozenset({ReadDateReason.RESOLVED}),
        "missing": frozenset({ReadDateReason.DATE_OMITTED, ReadDateReason.MESSAGE_NOT_READ_YET}),
        "unavailable": frozenset(
            {
                ReadDateReason.FLOOD_WAIT,
                ReadDateReason.TRANSIENT,
                ReadDateReason.MESSAGE_TOO_OLD,
                ReadDateReason.PRIVACY_RESTRICTED,
                ReadDateReason.NOT_MUTUAL_CONTACT,
                ReadDateReason.INVALID_TARGET,
                ReadDateReason.ACCESS_LOST,
            }
        ),
    }
    if normalized not in valid_by_status[status]:
        raise ValueError(f"read-date reason {normalized.value!r} is invalid for status {status!r}")
    return normalized


def _persist_read_at_v70(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    read_at: int | None,
    checked_at: int,
    status: str,
    reason: ReadDateReason,
    next_attempt_at: int | None,
) -> None:
    conn.execute(
        "INSERT INTO message_read_facts "
        "(dialog_id, message_id, read_at, checked_at, status, reason, next_attempt_at) "
        "SELECT ?, ?, ?, ?, ?, ?, ? WHERE EXISTS ("
        "SELECT 1 FROM full_history_enrollment WHERE dialog_id = ? AND enabled = 1) "
        "ON CONFLICT(dialog_id, message_id) DO UPDATE SET "
        "read_at = excluded.read_at, checked_at = excluded.checked_at, status = excluded.status, "
        "reason = excluded.reason, next_attempt_at = excluded.next_attempt_at "
        "WHERE NOT (message_read_facts.status = 'complete' AND message_read_facts.read_at IS NOT NULL) "
        "AND ((excluded.status = 'complete' AND excluded.read_at IS NOT NULL) "
        "OR (message_read_facts.reason != 'message_too_old' "
        "AND excluded.checked_at > message_read_facts.checked_at))",
        (dialog_id, message_id, read_at, checked_at, status, reason.value, next_attempt_at, dialog_id),
    )


def persist_read_at(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    read_at: int | None,
    checked_at: int,
    status: str,
    reason: ReadDateReason | str | None = None,
    next_attempt_at: int | None = None,
) -> None:
    """Store one outbox-read-date probe with terminal and timestamp fences."""

    if status not in {"complete", "missing", "unavailable"}:
        raise ValueError(f"unsupported read-date status: {status}")
    if status == "complete" and read_at is None:
        raise ValueError("complete read-date result requires a non-null read_at")
    normalized_reason = _normalize_persist_reason(status, reason)
    if not is_read_date_reason_retryable(normalized_reason):
        next_attempt_at = None
    elif next_attempt_at is None:
        raise ValueError("retryable read-date result requires next_attempt_at")

    with conn:
        _persist_read_at_v70(
            conn,
            dialog_id,
            message_id,
            read_at=read_at,
            checked_at=checked_at,
            status=status,
            reason=normalized_reason,
            next_attempt_at=next_attempt_at,
        )


def read_at_map(conn: sqlite3.Connection, dialog_id: int, message_ids: Sequence[int]) -> dict[int, int | None]:
    """Return persisted read dates; absent rows remain cache misses."""

    if not message_ids:
        return {}
    placeholders = ",".join("?" for _ in message_ids)
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            f"SELECT message_id, read_at FROM message_read_facts "
            f"WHERE dialog_id = ? AND message_id IN ({placeholders})",
            [dialog_id, *message_ids],
        ).fetchall(),
    )
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
    rows = cast(
        list[tuple[object, ...]],
        conn.execute(
            f"SELECT message_id, next_attempt_at FROM message_read_facts "
            f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
            "AND (next_attempt_at IS NULL OR next_attempt_at > ?)",
            [dialog_id, *message_ids, stale_before_utc],
        ).fetchall(),
    )
    fresh = {int(cast(int | str, row[0])) for row in rows}
    selected: list[int] = []
    for message_id in message_ids:
        if message_id not in fresh and message_id not in selected:
            selected.append(message_id)
    return selected
