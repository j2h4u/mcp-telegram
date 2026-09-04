"""Transaction-neutral SQLite policy for durable hydration jobs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, cast

from .. import message_contracts as _message_contracts
from ..hydration_queue import (
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationPriority,
    HydrationQueueRepository,
)
from ..media_fact import MediaFact, decode_media_fact, encode_media_payload, is_transcribable_telegram_media

_EMPTY_MEDIA_PAYLOAD = "{}"
_FACT_HYDRATION_EMPTY_KINDS = frozenset(("contact", "other"))
_FACT_HYDRATION_ELIGIBILITY_SQL = (
    "SELECT 1 FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.dialog_id = ? AND sd.status IN ('syncing', 'synced')"
)
_TRANSCRIBABLE_MEDIA_SQL = (
    "(json_valid(m.media_payload) "
    "AND json_type(CASE WHEN json_valid(m.media_payload) THEN m.media_payload ELSE '{}' END) = 'object' "
    "AND (m.media_kind = 'voice' OR (m.media_kind = 'video' "
    "AND json_type(CASE WHEN json_valid(m.media_payload) THEN m.media_payload ELSE '{}' END, "
    "'$.round_message') = 'true')))"
)
_TRANSCRIPTION_HYDRATION_MESSAGE_SQL = (
    "m.is_deleted = 0 AND (" + _TRANSCRIBABLE_MEDIA_SQL + ") "
    "AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
    "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id)"
)


@dataclass(frozen=True, slots=True)
class TranscriptionHydrationRepair:
    """Bounded result of repairing missing transcription jobs."""

    enqueued: int
    has_more: bool


@dataclass(frozen=True, slots=True)
class MediaMetadataHydrationRepair:
    """Bounded result of repairing missing media metadata jobs."""

    enqueued: int
    has_more: bool


def _first_json_object_key_wins(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Decode JSON objects with SQLite JSON1's first-key-wins semantics."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key not in result:
            result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    """Reject Python JSON extensions that SQLite JSON1 does not accept."""
    raise ValueError("non-finite JSON number")


def _is_transcribable_media_pair(kind: object, payload: object) -> bool:
    """Apply the domain predicate to one projected SQLite media pair."""
    if not isinstance(payload, str):
        return False
    try:
        decoded_payload = cast(
            object,
            json.loads(
                payload,
                object_pairs_hook=_first_json_object_key_wins,
                parse_constant=_reject_json_constant,
            ),
        )
    except TypeError, ValueError, json.JSONDecodeError:
        return False
    if not isinstance(decoded_payload, dict):
        return False
    fact = decode_media_fact(kind, decoded_payload)
    return fact is not None and fact.payload == decoded_payload and is_transcribable_telegram_media(fact)


def _is_canonical_media_pair(kind: object, payload: object, *, fact: MediaFact | None = None) -> bool:
    """Return whether a stored media pair round-trips through the codec."""
    decoded = fact if fact is not None else decode_media_fact(kind, payload)
    return isinstance(payload, str) and decoded is not None and encode_media_payload(decoded) == payload


def fact_hydration_eligible(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Return whether the dialog currently permits full-history hydration."""
    return conn.execute(_FACT_HYDRATION_ELIGIBILITY_SQL, (dialog_id,)).fetchone() is not None


def media_fact_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether one unresolved media message may be hydrated now."""
    return (
        conn.execute(
            "SELECT 1 FROM messages m WHERE m.dialog_id = ? AND m.message_id = ? "
            "AND m.is_deleted = 0 AND ((m.media_kind IN ('contact', 'other') AND m.media_payload = '{}') "
            "OR (m.media_kind = 'video' AND json_valid(m.media_payload) "
            "AND json_type(m.media_payload) = 'object' "
            "AND json_type(m.media_payload, '$.round_message') IS NULL)) "
            "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone()
        is not None
    )


def transcription_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether one undeleted transcribable message still needs work."""
    row = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT m.media_kind, m.media_payload FROM messages m "
            "WHERE m.dialog_id = ? AND m.message_id = ? AND m.is_deleted = 0 "
            "AND (" + _TRANSCRIBABLE_MEDIA_SQL + ") "
            "AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
            "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id) "
            "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone(),
    )
    return row is not None and _is_transcribable_media_pair(*row)


_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL = (
    "FROM messages m INDEXED BY idx_messages_transcribable_undeleted_sent "
    "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id AND sd.status IN ('syncing', 'synced') "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 "
    "LEFT JOIN hydration_jobs hj ON hj.kind = 'transcription' "
    "AND hj.dialog_id = m.dialog_id AND hj.message_id = m.message_id "
    f"WHERE {_TRANSCRIPTION_HYDRATION_MESSAGE_SQL} AND hj.message_id IS NULL"
)
_MEDIA_METADATA_HYDRATION_ELIGIBILITY_SQL = (
    "m.is_deleted = 0 AND ((m.media_kind IN ('contact', 'other') AND m.media_payload = '{}') "
    "OR (m.media_kind = 'video' AND json_valid(m.media_payload) "
    "AND json_type(m.media_payload) = 'object' "
    "AND json_type(m.media_payload, '$.round_message') IS NULL))"
)
_REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL = (
    "FROM messages m INDEXED BY idx_messages_media_unresolved_contact_other "
    "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id AND sd.status IN ('syncing', 'synced') "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 "
    "LEFT JOIN hydration_jobs hj ON hj.kind = 'media_metadata' "
    "AND hj.dialog_id = m.dialog_id AND hj.message_id = m.message_id "
    "WHERE m.is_deleted = 0 AND m.media_kind IN ('contact', 'other') AND m.media_payload = '{}' "
    "AND hj.message_id IS NULL"
)
_REPAIR_MEDIA_METADATA_VIDEO_SQL = (
    "FROM messages m INDEXED BY idx_messages_media_unresolved_video "
    "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id AND sd.status IN ('syncing', 'synced') "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 "
    "LEFT JOIN hydration_jobs hj ON hj.kind = 'media_metadata' "
    "AND hj.dialog_id = m.dialog_id AND hj.message_id = m.message_id "
    "WHERE m.is_deleted = 0 AND m.media_kind = 'video' AND json_valid(m.media_payload) "
    "AND json_type(m.media_payload) = 'object' "
    "AND json_type(m.media_payload, '$.round_message') IS NULL "
    "AND hj.message_id IS NULL"
)


def repair_transcription_hydration_jobs(
    conn: sqlite3.Connection, *, due_at: int, max_jobs: int
) -> TranscriptionHydrationRepair:
    """Bound the recurring repair of missing transcribable-media jobs."""
    if max_jobs <= 0:
        return TranscriptionHydrationRepair(0, False)
    candidates_sql = (
        "SELECT 'transcription', m.dialog_id, m.message_id, ?, 0, ?, m.sent_at, 0 "
        f"{_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL} "
        "ORDER BY m.sent_at DESC, m.dialog_id, m.message_id LIMIT ?"
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO hydration_jobs "
        "(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at, terminal) "
        f"{candidates_sql}",
        (due_at, int(HydrationPriority.BACKFILL), max_jobs),
    )
    has_more = False
    if cursor.rowcount >= max_jobs:
        has_more = (
            conn.execute(
                "SELECT 1 "
                f"{_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL} "
                "ORDER BY m.sent_at DESC, m.dialog_id, m.message_id LIMIT 1",
            ).fetchone()
            is not None
        )
    return TranscriptionHydrationRepair(cursor.rowcount, has_more)


def repair_media_metadata_hydration_jobs(
    conn: sqlite3.Connection, *, due_at: int, max_jobs: int
) -> MediaMetadataHydrationRepair:
    """Bound recurring repair of unresolved contact/other and video metadata."""
    if max_jobs <= 0:
        return MediaMetadataHydrationRepair(0, False)
    candidates_sql = (
        "SELECT 'media_metadata', m.dialog_id, m.message_id, ?, 0, ?, m.sent_at, 0 "
        f"{_REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL} "
        "UNION ALL "
        "SELECT 'media_metadata', m.dialog_id, m.message_id, ?, 0, ?, m.sent_at, 0 "
        f"{_REPAIR_MEDIA_METADATA_VIDEO_SQL} "
        "ORDER BY 7 DESC, 2, 3 LIMIT ?"
    )
    cursor = conn.execute(
        "INSERT OR IGNORE INTO hydration_jobs "
        "(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at, terminal) "
        f"{candidates_sql}",
        (due_at, int(HydrationPriority.BACKFILL), due_at, int(HydrationPriority.BACKFILL), max_jobs),
    )
    has_more = False
    if cursor.rowcount >= max_jobs:
        has_more = (
            conn.execute(
                "SELECT 1 FROM (SELECT 1 "
                f"{_REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL} "
                "UNION ALL SELECT 1 "
                f"{_REPAIR_MEDIA_METADATA_VIDEO_SQL}) LIMIT 1",
            ).fetchone()
            is not None
        )
    return MediaMetadataHydrationRepair(cursor.rowcount, has_more)


def reconcile_fact_hydration_job(
    conn: sqlite3.Connection,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
) -> None:
    """Reconcile one persisted message and its queue row in caller's transaction."""
    queue = HydrationQueueRepository(conn)
    if not queue.is_available():
        return
    _reconcile_media_hydration_job(conn, queue, message, due_at=due_at)
    _reconcile_transcription_hydration_job(conn, queue, message, due_at=due_at, priority=priority)


def _reconcile_media_hydration_job(
    conn: sqlite3.Connection, queue: HydrationQueueRepository, message: _message_contracts.StoredMessage, *, due_at: int
) -> None:
    job = HydrationJob(
        MEDIA_METADATA_KIND,
        message.dialog_id,
        message.message_id,
        due_at,
        0,
        message.sent_at,
        HydrationPriority.BACKFILL,
    )
    unresolved = message.media_kind in _FACT_HYDRATION_EMPTY_KINDS and message.media_payload == _EMPTY_MEDIA_PAYLOAD
    if unresolved and media_fact_hydration_eligible(conn, message.dialog_id, message.message_id):
        queue.enqueue(job)
    elif unresolved:
        queue.remove_active(job)
    else:
        queue.remove(job)


def _reconcile_transcription_hydration_job(
    conn: sqlite3.Connection,
    queue: HydrationQueueRepository,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    priority: HydrationPriority,
) -> None:
    job = HydrationJob(
        TRANSCRIPTION_HYDRATION_KIND, message.dialog_id, message.message_id, due_at, 0, message.sent_at, priority
    )
    transcription_row = cast(
        tuple[int] | None,
        conn.execute(
            "SELECT 1 FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?",
            (message.dialog_id, message.message_id),
        ).fetchone(),
    )
    transcribable = _is_transcribable_media_pair(message.media_kind, message.media_payload)
    if (
        transcribable
        and transcription_row is None
        and transcription_hydration_eligible(conn, message.dialog_id, message.message_id)
    ):
        queue.enqueue(job)
    elif transcribable and transcription_row is None:
        queue.remove_active(job)
    else:
        queue.remove(job)


def reconcile_fact_hydration_jobs_for_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    due_at: int,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    """Enqueue all unresolved media for an eligible dialog after revalidation."""
    queue = HydrationQueueRepository(conn)
    if not queue.is_available() or not fact_hydration_eligible(conn, dialog_id):
        return
    rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            "SELECT message_id, sent_at FROM messages WHERE dialog_id = ? AND is_deleted = 0 "
            "AND media_kind IN ('contact', 'other') AND media_payload = '{}' UNION ALL "
            "SELECT message_id, sent_at FROM messages WHERE dialog_id = ? AND is_deleted = 0 AND media_kind = 'video' "
            "AND json_valid(media_payload) AND json_type(media_payload) = 'object' "
            "AND json_type(media_payload, '$.round_message') IS NULL",
            (dialog_id, dialog_id),
        ).fetchall(),
    )
    for message_id, sent_at in rows:
        queue.enqueue(
            HydrationJob(
                MEDIA_METADATA_KIND, dialog_id, int(message_id), due_at, 0, int(sent_at), HydrationPriority.BACKFILL
            )
        )
    transcribable_rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            "SELECT m.message_id, m.sent_at FROM messages m LEFT JOIN message_transcriptions mt "
            "ON mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id WHERE m.dialog_id = ? "
            "AND m.is_deleted = 0 AND (" + _TRANSCRIBABLE_MEDIA_SQL + ") AND mt.message_id IS NULL",
            (dialog_id,),
        ).fetchall(),
    )
    for message_id, sent_at in transcribable_rows:
        queue.enqueue(
            HydrationJob(TRANSCRIPTION_HYDRATION_KIND, dialog_id, int(message_id), due_at, 0, int(sent_at), priority)
        )
