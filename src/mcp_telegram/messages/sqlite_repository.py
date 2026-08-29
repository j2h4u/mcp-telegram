"""Canonical SQLite persistence for extracted Telegram messages."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import cast

from .. import message_contracts as _message_contracts
from ..fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from ..hydration_queue import (
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationPriority,
    HydrationQueueRepository,
)
from ..reactions.contracts import ReactionAggregate
from ..reactions.persistence import replace_reaction_aggregates

_EMPTY_MEDIA_PAYLOAD = "{}"
_FACT_HYDRATION_EMPTY_KINDS = frozenset(("contact", "other"))
_FACT_HYDRATION_ELIGIBILITY_SQL = (
    "SELECT 1 FROM synced_dialogs sd "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = sd.dialog_id AND fhe.enabled = 1 "
    "WHERE sd.dialog_id = ? AND sd.status IN ('syncing', 'synced')"
)


def _insert_sql(table: str, dataclass_type: type) -> str:
    column_names = tuple(field.name for field in fields(dataclass_type))
    return (
        f"INSERT OR REPLACE INTO {table} ({', '.join(column_names)}) "
        f"VALUES ({', '.join(':' + name for name in column_names)})"
    )


_STORED_MESSAGE_FIELDS = tuple(field.name for field in fields(_message_contracts.StoredMessage))
_INSERT_MESSAGE_SQL = (
    f"INSERT OR REPLACE INTO messages ({', '.join(_STORED_MESSAGE_FIELDS)}, reply_count, is_deleted) "
    f"VALUES ({', '.join(':' + name for name in _STORED_MESSAGE_FIELDS)}, :reply_count, 0)"
)
_INSERT_ENTITY_SQL = _insert_sql("message_entities", _message_contracts.EntityRecord)
_INSERT_FORWARD_SQL = _insert_sql("message_forwards", _message_contracts.ForwardRecord)
_DELETE_ENTITIES_SQL = "DELETE FROM message_entities WHERE dialog_id = ? AND message_id = ?"
_DELETE_FORWARD_SQL = "DELETE FROM message_forwards WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_TEXT_SQL = "SELECT text FROM messages WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_EXISTS_SQL = "SELECT 1 FROM messages WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_OUT_SQL = "SELECT out FROM messages WHERE dialog_id = ? AND message_id = ?"
_NEXT_VERSION_SQL = "SELECT COALESCE(MAX(version), 0) + 1 FROM message_versions WHERE dialog_id = ? AND message_id = ?"
_INSERT_VERSION_SQL = (
    "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date) VALUES (?, ?, ?, ?, ?)"
)
_UPDATE_MESSAGE_TEXT_SQL = "UPDATE messages SET text = ? WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_TRANSCRIPTION_SQL = (
    "SELECT text, transcription_id FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?"
)
_MARK_DELETED_SQL = (
    "UPDATE messages SET is_deleted = 1, deleted_at = ? WHERE dialog_id = ? AND message_id = ? AND is_deleted = 0"
)
_SELECT_UNDELETED_MESSAGES_SQL = (
    "SELECT message_id FROM messages WHERE dialog_id = ? AND is_deleted = 0 AND sent_at < ?"
)


@dataclass(frozen=True, slots=True)
class MessageTextLookup:
    """Result of reading a message text, retaining missing vs SQL NULL."""

    found: bool
    text: str | None


@dataclass(frozen=True, slots=True)
class MessageOutLookup:
    """Result of reading the canonical outgoing marker for one message."""

    found: bool
    outgoing: bool


@dataclass(frozen=True, slots=True)
class TranscriptionHydrationRepair:
    """Bounded result of repairing missing voice transcription jobs."""

    enqueued: int
    has_more: bool


def message_exists(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether the canonical message key is already persisted."""
    return conn.execute(_SELECT_MESSAGE_EXISTS_SQL, (dialog_id, message_id)).fetchone() is not None


def read_message_text(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> MessageTextLookup:
    """Read one message text without opening or committing a transaction."""
    row = cast(
        tuple[str | None] | None,
        conn.execute(_SELECT_MESSAGE_TEXT_SQL, (dialog_id, message_id)).fetchone(),
    )
    return MessageTextLookup(found=row is not None, text=None if row is None else row[0])


def read_message_out(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> MessageOutLookup:
    """Read the canonical outgoing marker without opening a transaction."""
    row = cast(tuple[int] | None, conn.execute(_SELECT_MESSAGE_OUT_SQL, (dialog_id, message_id)).fetchone())
    return MessageOutLookup(found=row is not None, outgoing=bool(row[0]) if row is not None else False)


def persist_edited_message(
    conn: sqlite3.Connection,
    extracted: _message_contracts.ExtractedMessage,
    *,
    old_text: str | None,
    edit_date: int,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
) -> int | None:
    """Version and persist a changed message in the caller's transaction.

    Returns the new version number, or ``None`` when the text is unchanged.
    The canonical bundle writer owns the message row, FTS, reactions, entities,
    forwards, and transcription-preservation behavior.
    """
    if old_text == extracted.message.text:
        return None

    dialog_id = extracted.message.dialog_id
    message_id = extracted.message.message_id
    version_row = cast(tuple[int], conn.execute(_NEXT_VERSION_SQL, (dialog_id, message_id)).fetchone())
    next_version = int(version_row[0])
    conn.execute(
        _INSERT_VERSION_SQL,
        (dialog_id, message_id, next_version, old_text, edit_date),
    )
    insert_messages_with_fts(conn, [extracted], priority=priority)
    return next_version


def persist_transcribed_text(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    old_text: str | None,
    transcribed_text: str,
    transcribed_at: int,
) -> int | None:
    """Persist a changed transcription, preserving all other message projections."""
    if old_text == transcribed_text:
        return None

    version_row = cast(tuple[int], conn.execute(_NEXT_VERSION_SQL, (dialog_id, message_id)).fetchone())
    next_version = int(version_row[0])
    conn.execute(
        _INSERT_VERSION_SQL,
        (dialog_id, message_id, next_version, old_text, transcribed_at),
    )
    conn.execute(_UPDATE_MESSAGE_TEXT_SQL, (transcribed_text, dialog_id, message_id))
    conn.execute(DELETE_FTS_SQL, (dialog_id, message_id))
    conn.execute(INSERT_FTS_SQL, (dialog_id, message_id, stem_text(transcribed_text)))
    return next_version


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
        "INSERT INTO message_transcriptions(dialog_id, message_id, text, transcription_id, received_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(dialog_id, message_id) DO UPDATE SET text=excluded.text, "
        "transcription_id=excluded.transcription_id, received_at=excluded.received_at",
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
    """Apply one final Telegram transcription through the canonical path.

    The fact row, canonical message text, FTS projection, and matching
    hydration job are changed in the caller-owned transaction. This method is
    for an existing canonical message; real-time events for a not-yet-imported
    message use ``stage_message_transcription`` below.
    """
    text = transcribed_text.strip()
    if not text:
        return False
    message = read_message_text(conn, dialog_id, message_id)
    if not message.found:
        return False
    upsert_message_transcription(
        conn,
        dialog_id,
        message_id,
        transcribed_text=text,
        transcription_id=transcription_id,
        received_at=received_at,
    )
    if message.found and message.text != text:
        persist_transcribed_text(
            conn,
            dialog_id,
            message_id,
            old_text=message.text,
            transcribed_text=text,
            transcribed_at=received_at,
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
        conn,
        dialog_id,
        message_id,
        transcribed_text=text,
        transcription_id=transcription_id,
        received_at=received_at,
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
    eligible = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT 1 FROM messages m "
            "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id AND sd.status IN ('syncing', 'synced') "
            "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 "
            "WHERE m.dialog_id = ? AND m.message_id = ? AND m.media_kind = 'voice' AND m.is_deleted = 0 "
            "AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
            "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id)",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if eligible is None:
        fact = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT 1 FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?",
                (dialog_id, message_id),
            ).fetchone(),
        )
        return "already_applied" if fact is not None else "not_applied"
    upsert_message_transcription(
        conn,
        dialog_id,
        message_id,
        transcribed_text=text,
        transcription_id=transcription_id,
        received_at=received_at,
    )
    old_text = read_message_text(conn, dialog_id, message_id).text
    if old_text != text:
        persist_transcribed_text(
            conn,
            dialog_id,
            message_id,
            old_text=old_text,
            transcribed_text=text,
            transcribed_at=received_at,
        )
    _remove_transcription_hydration_job(conn, dialog_id, message_id)
    return "applied"


def _remove_transcription_hydration_job(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> None:
    if HydrationQueueRepository(conn).is_available():
        conn.execute(
            "DELETE FROM hydration_jobs WHERE kind = ? AND dialog_id = ? AND message_id = ?",
            (TRANSCRIPTION_HYDRATION_KIND, dialog_id, message_id),
        )


def mark_message_deleted(conn: sqlite3.Connection, dialog_id: int, message_id: int, deleted_at: int) -> bool:
    """Tombstone one message and report whether this call changed its state."""
    cursor = conn.execute(_MARK_DELETED_SQL, (deleted_at, dialog_id, message_id))
    if cursor.rowcount > 0:
        HydrationQueueRepository(conn).remove_for_message(dialog_id, message_id)
    return cursor.rowcount > 0


def list_undeleted_message_ids(conn: sqlite3.Connection, dialog_id: int, sent_before: int) -> tuple[int, ...]:
    """List undeleted message IDs sent strictly before the caller's cutoff."""
    rows = cast(
        Sequence[tuple[int]],
        conn.execute(_SELECT_UNDELETED_MESSAGES_SQL, (dialog_id, sent_before)).fetchall(),
    )
    return tuple(int(message_id) for (message_id,) in rows)


def insert_messages_with_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
) -> None:
    """Persist message bundles in the caller-owned transaction.

    Replaces FTS and child projections so edits are idempotent, while durable
    transcription facts override source captions before the row is written.
    It deliberately does not open or commit a transaction; callers compose it
    with their own state changes.
    """
    preserved = _preserve_transcribed_texts(conn, extracted)
    projected = _overlay_message_transcriptions(conn, preserved)
    _write_message_rows_and_fts(conn, projected, priority=priority)
    _delete_entity_and_forward_projections(conn, projected)
    _replace_reaction_projections(conn, projected)
    _insert_entity_and_forward_projections(conn, projected)


def fact_hydration_eligible(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Return whether the dialog currently permits full-history hydration."""
    return conn.execute(_FACT_HYDRATION_ELIGIBILITY_SQL, (dialog_id,)).fetchone() is not None


def media_fact_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether one unresolved media message may be hydrated now."""
    return (
        conn.execute(
            "SELECT 1 FROM messages m WHERE m.dialog_id = ? AND m.message_id = ? "
            "AND m.is_deleted = 0 AND m.media_kind IN ('contact', 'other') AND m.media_payload = '{}' "
            "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone()
        is not None
    )


def transcription_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether one undeleted voice message still needs a transcription."""
    return (
        conn.execute(
            "SELECT 1 FROM messages m WHERE m.dialog_id = ? AND m.message_id = ? "
            "AND m.is_deleted = 0 AND m.media_kind = 'voice' "
            "AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
            "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id) "
            "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone()
        is not None
    )


_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL = (
    "FROM messages m INDEXED BY idx_messages_voice_undeleted_sent "
    "JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id AND sd.status IN ('syncing', 'synced') "
    "JOIN full_history_enrollment fhe ON fhe.dialog_id = m.dialog_id AND fhe.enabled = 1 "
    "LEFT JOIN message_transcriptions mt ON mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id "
    "LEFT JOIN hydration_jobs hj ON hj.kind = 'transcription' "
    "AND hj.dialog_id = m.dialog_id AND hj.message_id = m.message_id "
    "WHERE m.is_deleted = 0 AND m.media_kind = 'voice' "
    "AND mt.message_id IS NULL AND hj.message_id IS NULL"
)


def repair_transcription_hydration_jobs(
    conn: sqlite3.Connection, *, due_at: int, max_jobs: int
) -> TranscriptionHydrationRepair:
    """Bound the recurring repair of missing voice transcription jobs."""
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
    has_more = (
        conn.execute(
            "SELECT 1 "
            f"{_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL} "
            "ORDER BY m.sent_at DESC, m.dialog_id, m.message_id LIMIT 1",
        ).fetchone()
        is not None
    )
    return TranscriptionHydrationRepair(cursor.rowcount, has_more)


def apply_hydrated_media_fact(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    media_kind: str | None,
    media_payload: str | None,
) -> bool:
    """Apply one media fact only while full-history access remains current.

    The eligibility predicates live in the same UPDATE as the projection
    write, so an access/enrollment transition committed before this statement
    cannot be followed by a stale media write.
    """
    cursor = conn.execute(
        "UPDATE messages SET media_kind = ?, media_payload = ? "
        "WHERE dialog_id = ? AND message_id = ? "
        "AND is_deleted = 0 AND media_kind IN ('contact', 'other') AND media_payload = '{}' "
        "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
        (media_kind, media_payload, dialog_id, message_id, dialog_id),
    )
    return cursor.rowcount > 0


def reconcile_fact_hydration_job(
    conn: sqlite3.Connection,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
) -> None:
    """Reconcile one persisted message and its queue row in caller's tx."""
    queue = HydrationQueueRepository(conn)
    if not queue.is_available():
        return
    job = HydrationJob(
        MEDIA_METADATA_KIND,
        message.dialog_id,
        message.message_id,
        due_at,
        0,
        message.sent_at,
        HydrationPriority.BACKFILL,
    )
    unresolved = message.media_kind in _FACT_HYDRATION_EMPTY_KINDS and message.media_payload == "{}"
    if unresolved and media_fact_hydration_eligible(conn, message.dialog_id, message.message_id):
        queue.enqueue(job)
    elif unresolved:
        queue.remove_active(job)
    else:
        queue.remove(job)

    transcription_job = HydrationJob(
        TRANSCRIPTION_HYDRATION_KIND,
        message.dialog_id,
        message.message_id,
        due_at,
        0,
        message.sent_at,
        priority,
    )
    transcription_row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT 1 FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?",
            (message.dialog_id, message.message_id),
        ).fetchone(),
    )
    if (
        message.media_kind == "voice"
        and transcription_row is None
        and transcription_hydration_eligible(conn, message.dialog_id, message.message_id)
    ):
        queue.enqueue(transcription_job)
    elif message.media_kind == "voice" and transcription_row is None:
        queue.remove_active(transcription_job)
    else:
        queue.remove(transcription_job)


def reconcile_fact_hydration_jobs_for_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    due_at: int,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    """Enqueue all unresolved media for an eligible dialog after revalidation."""
    queue = HydrationQueueRepository(conn)
    if not queue.is_available():
        return
    if not fact_hydration_eligible(conn, dialog_id):
        return
    rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            "SELECT message_id, sent_at FROM messages "
            "WHERE dialog_id = ? AND is_deleted = 0 "
            "AND media_kind IN ('contact', 'other') AND media_payload = '{}'",
            (dialog_id,),
        ).fetchall(),
    )
    for message_id, sent_at in rows:
        queue.enqueue(
            HydrationJob(
                MEDIA_METADATA_KIND,
                dialog_id,
                int(message_id),
                due_at,
                0,
                int(sent_at),
                HydrationPriority.BACKFILL,
            )
        )
    voice_rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            "SELECT m.message_id, m.sent_at FROM messages m "
            "LEFT JOIN message_transcriptions mt ON mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id "
            "WHERE m.dialog_id = ? AND m.is_deleted = 0 AND m.media_kind = 'voice' AND mt.message_id IS NULL",
            (dialog_id,),
        ).fetchall(),
    )
    for message_id, sent_at in voice_rows:
        queue.enqueue(
            HydrationJob(
                TRANSCRIPTION_HYDRATION_KIND,
                dialog_id,
                int(message_id),
                due_at,
                0,
                int(sent_at),
                priority,
            )
        )


def _write_message_rows_and_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    """Replace canonical message and FTS rows for one extraction batch."""
    messages = [item.message for item in extracted]
    conn.executemany(
        _INSERT_MESSAGE_SQL,
        [{**asdict(item.message), "reply_count": item.reply_count} for item in extracted],
    )
    conn.executemany(DELETE_FTS_SQL, ((item.dialog_id, item.message_id) for item in messages))
    conn.executemany(
        INSERT_FTS_SQL,
        ((item.dialog_id, item.message_id, stem_text(item.text)) for item in messages),
    )
    for message in messages:
        reconcile_fact_hydration_job(conn, message, due_at=int(time.time()), priority=priority)


def _overlay_message_transcriptions(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
) -> list[_message_contracts.ExtractedMessage]:
    """Overlay durable transcription facts before canonical row/FTS writes."""
    projected: list[_message_contracts.ExtractedMessage] = []
    for item in extracted:
        dialog_id = item.message.dialog_id
        message_id = item.message.message_id
        row = cast(
            tuple[str, int] | None,
            conn.execute(_SELECT_MESSAGE_TRANSCRIPTION_SQL, (dialog_id, message_id)).fetchone(),
        )
        if row is None:
            projected.append(item)
            continue
        projected.append(replace(item, message=replace(item.message, text=row[0])))
    return projected


def _delete_entity_and_forward_projections(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
) -> None:
    """Clear projections which are replaced by the current extraction batch."""
    id_pairs = [(item.message.dialog_id, item.message.message_id) for item in extracted]
    conn.executemany(_DELETE_ENTITIES_SQL, id_pairs)
    conn.executemany(_DELETE_FORWARD_SQL, id_pairs)


def _replace_reaction_projections(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
) -> None:
    """Replace every reaction aggregate, including an intentionally empty one."""
    for item in extracted:
        replace_reaction_aggregates(
            conn,
            item.message.dialog_id,
            item.message.message_id,
            tuple(ReactionAggregate(emoji=row.emoji, count=row.count) for row in item.reactions),
        )


def _insert_entity_and_forward_projections(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
) -> None:
    """Insert entity and forward projections after their replacement deletes."""
    entities = [entity for item in extracted for entity in item.entities]
    if entities:
        conn.executemany(_INSERT_ENTITY_SQL, [asdict(entity) for entity in entities])
    forwards = [item.forward for item in extracted if item.forward is not None]
    if forwards:
        conn.executemany(_INSERT_FORWARD_SQL, [asdict(forward) for forward in forwards])


def _preserve_transcribed_texts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
) -> list[_message_contracts.ExtractedMessage]:
    preserved_texts: dict[tuple[int, int], str] = {}
    for item in extracted:
        # Retain pre-v38 hydration behavior for unresolved media and voice.
        # Other media captions may be removed on reimport. Durable
        # transcription facts are overlaid separately before persistence.
        if item.message.text is not None and item.message.text.strip():
            continue
        if item.message.media_kind not in _FACT_HYDRATION_EMPTY_KINDS and item.message.media_kind != "voice":
            continue
        row = cast(
            tuple[str | None] | None,
            conn.execute(
                "SELECT text FROM messages WHERE dialog_id = ? AND message_id = ?",
                (item.message.dialog_id, item.message.message_id),
            ).fetchone(),
        )
        if row is not None and row[0]:
            preserved_texts[(item.message.dialog_id, item.message.message_id)] = row[0]
    if not preserved_texts:
        return list(extracted)
    return [
        replace(
            item,
            message=replace(
                item.message,
                text=preserved_texts.get((item.message.dialog_id, item.message.message_id), item.message.text),
            ),
        )
        for item in extracted
    ]
