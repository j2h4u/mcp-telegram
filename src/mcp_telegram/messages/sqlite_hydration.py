"""Transaction-neutral SQLite application of media and transcription facts."""

from __future__ import annotations

import sqlite3
from typing import cast

from ..hydration_queue import TRANSCRIPTION_HYDRATION_KIND, HydrationJob, HydrationPriority, HydrationQueueRepository
from ..media_fact import decode_media_fact, is_transcribable_telegram_media
from .sqlite_bundle import persist_transcribed_text, read_message_text
from .sqlite_hydration_jobs import (
    _FACT_HYDRATION_ELIGIBILITY_SQL,
    _MEDIA_METADATA_HYDRATION_ELIGIBILITY_SQL,
    _TRANSCRIBABLE_MEDIA_SQL,
    _is_canonical_media_pair,
    _is_transcribable_media_pair,
)

_SELECT_MESSAGE_MEDIA_SQL = "SELECT media_kind, media_payload FROM messages WHERE dialog_id = ? AND message_id = ?"


def upsert_message_transcription(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    transcribed_text: str,
    transcription_id: int,
    received_at: int,
) -> None:
    """Upsert the latest final Telegram transcription fact."""
    text = transcribed_text.strip()
    if not text:
        return
    conn.execute(
        "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(dialog_id,message_id) DO UPDATE SET text=excluded.text, transcription_id=excluded.transcription_id, received_at=excluded.received_at",
        (dialog_id, message_id, text, transcription_id, received_at),
    )


def apply_message_transcription(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    transcribed_text: str,
    transcription_id: int,
    received_at: int,
) -> bool:
    """Apply one final Telegram transcription through the canonical path."""
    text = transcribed_text.strip()
    if not text:
        return False
    row = cast(
        tuple[object, object] | None, conn.execute(_SELECT_MESSAGE_MEDIA_SQL, (dialog_id, message_id)).fetchone()
    )
    if row is None or not _is_transcribable_media_pair(*row):
        return False
    message = read_message_text(conn, dialog_id, message_id)
    upsert_message_transcription(
        conn, dialog_id, message_id, transcribed_text=text, transcription_id=transcription_id, received_at=received_at
    )
    if message.found and message.text != text:
        persist_transcribed_text(
            conn, dialog_id, message_id, old_text=message.text, transcribed_text=text, transcribed_at=received_at
        )
    _remove_transcription_hydration_job(conn, dialog_id, message_id)
    return True


def stage_message_transcription(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    transcribed_text: str,
    transcription_id: int,
    received_at: int,
) -> bool:
    """Store a final event whose canonical message has not arrived yet."""
    text = transcribed_text.strip()
    if not text or read_message_text(conn, dialog_id, message_id).found:
        return False
    upsert_message_transcription(
        conn, dialog_id, message_id, transcribed_text=text, transcription_id=transcription_id, received_at=received_at
    )
    _remove_transcription_hydration_job(conn, dialog_id, message_id)
    return True


def apply_message_transcription_if_absent(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    transcribed_text: str,
    transcription_id: int,
    received_at: int,
) -> str:
    """Atomically apply a worker result only when no final fact won the race."""
    text = transcribed_text.strip()
    if not text:
        return "not_applied"
    eligible_row = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT m.media_kind, m.media_payload FROM messages m JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id AND sd.status IN ('syncing', 'synced') "
            "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 WHERE m.dialog_id = ? AND m.message_id = ? "
            "AND m.is_deleted = 0 AND ("
            + _TRANSCRIBABLE_MEDIA_SQL
            + ") AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
            "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id)",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if eligible_row is None or not _is_transcribable_media_pair(*eligible_row):
        fact = cast(
            tuple[int] | None,
            conn.execute(
                "SELECT 1 FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        return "already_applied" if fact is not None else "not_applied"
    upsert_message_transcription(
        conn, dialog_id, message_id, transcribed_text=text, transcription_id=transcription_id, received_at=received_at
    )
    old_text = read_message_text(conn, dialog_id, message_id).text
    if old_text != text:
        persist_transcribed_text(
            conn, dialog_id, message_id, old_text=old_text, transcribed_text=text, transcribed_at=received_at
        )
    _remove_transcription_hydration_job(conn, dialog_id, message_id)
    return "applied"


def _remove_transcription_hydration_job(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> None:
    if HydrationQueueRepository(conn).is_available():
        conn.execute(
            "DELETE FROM hydration_jobs WHERE kind = ? AND dialog_id = ? AND message_id = ?",
            (TRANSCRIPTION_HYDRATION_KIND, dialog_id, message_id),
        )


def apply_hydrated_media_fact(
    conn: sqlite3.Connection, dialog_id: int, message_id: int, media_kind: str | None, media_payload: str | None
) -> bool:
    """Apply one media fact only while full-history access remains current."""
    cursor = conn.execute(
        "UPDATE messages SET media_kind = ?, media_payload = ? WHERE dialog_id = ? AND message_id = ? AND "
        + _MEDIA_METADATA_HYDRATION_ELIGIBILITY_SQL.replace("m.", "")
        + " AND EXISTS ("
        + _FACT_HYDRATION_ELIGIBILITY_SQL
        + ")",
        (media_kind, media_payload, dialog_id, message_id, dialog_id),
    )
    return cursor.rowcount > 0


def enqueue_transcription_for_hydrated_media(
    conn: sqlite3.Connection, dialog_id: int, message_id: int, *, due_at: int
) -> bool:
    """Enqueue one backfill transcription after a canonical media update."""
    row = cast(
        tuple[object, object, int] | None,
        conn.execute(
            "SELECT m.media_kind, m.media_payload, m.sent_at FROM messages m WHERE m.dialog_id = ? AND m.message_id = ? "
            "AND m.is_deleted = 0 AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id)",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if row is None:
        return False
    fact = decode_media_fact(row[0], row[1])
    if not _is_canonical_media_pair(row[0], row[1], fact=fact) or not is_transcribable_telegram_media(fact):
        return False
    queue = HydrationQueueRepository(conn)
    if not queue.is_available():
        return False
    queue.enqueue(
        HydrationJob(
            TRANSCRIPTION_HYDRATION_KIND, dialog_id, message_id, due_at, 0, int(row[2]), HydrationPriority.BACKFILL
        )
    )
    return True
