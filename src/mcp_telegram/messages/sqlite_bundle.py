"""Transaction-neutral SQLite persistence for canonical message bundles."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import cast

from .. import message_contracts as _message_contracts
from ..fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from ..hydration_queue import HydrationPriority, HydrationQueueRepository
from ..media_fact import decode_media_fact, is_transcribable_telegram_media
from ..reactions.contracts import ReactionAggregate
from ..reactions.persistence import replace_reaction_aggregates
from .sqlite_hydration_jobs import _FACT_HYDRATION_EMPTY_KINDS, _is_canonical_media_pair, reconcile_fact_hydration_job


def _insert_sql(table: str, dataclass_type: type) -> str:
    column_names = tuple(field.name for field in fields(dataclass_type))
    return f"INSERT OR REPLACE INTO {table} ({', '.join(column_names)}) VALUES ({', '.join(':' + name for name in column_names)})"


_STORED_MESSAGE_FIELDS = tuple(field.name for field in fields(_message_contracts.StoredMessage))
_INSERT_MESSAGE_SQL = f"INSERT OR REPLACE INTO messages ({', '.join(_STORED_MESSAGE_FIELDS)}, reply_count, is_deleted) VALUES ({', '.join(':' + name for name in _STORED_MESSAGE_FIELDS)}, :reply_count, 0)"
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
    found: bool
    text: str | None


@dataclass(frozen=True, slots=True)
class MessageOutLookup:
    found: bool
    outgoing: bool


def message_exists(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether the canonical message key is already persisted."""
    return conn.execute(_SELECT_MESSAGE_EXISTS_SQL, (dialog_id, message_id)).fetchone() is not None


def read_message_text(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> MessageTextLookup:
    """Read one message text without opening or committing a transaction."""
    row = cast(tuple[str | None] | None, conn.execute(_SELECT_MESSAGE_TEXT_SQL, (dialog_id, message_id)).fetchone())
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
    """Version and persist a changed message in the caller's transaction."""
    if old_text == extracted.message.text:
        return None
    dialog_id, message_id = extracted.message.dialog_id, extracted.message.message_id
    version_row = cast(tuple[int], conn.execute(_NEXT_VERSION_SQL, (dialog_id, message_id)).fetchone())
    next_version = int(version_row[0])
    conn.execute(_INSERT_VERSION_SQL, (dialog_id, message_id, next_version, old_text, edit_date))
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
    conn.execute(_INSERT_VERSION_SQL, (dialog_id, message_id, next_version, old_text, transcribed_at))
    conn.execute(_UPDATE_MESSAGE_TEXT_SQL, (transcribed_text, dialog_id, message_id))
    conn.execute(DELETE_FTS_SQL, (dialog_id, message_id))
    conn.execute(INSERT_FTS_SQL, (dialog_id, message_id, stem_text(transcribed_text)))
    return next_version


def mark_message_deleted(conn: sqlite3.Connection, dialog_id: int, message_id: int, deleted_at: int) -> bool:
    """Tombstone one message and report whether this call changed its state."""
    cursor = conn.execute(_MARK_DELETED_SQL, (deleted_at, dialog_id, message_id))
    if cursor.rowcount > 0:
        HydrationQueueRepository(conn).remove_for_message(dialog_id, message_id)
    return cursor.rowcount > 0


def list_undeleted_message_ids(conn: sqlite3.Connection, dialog_id: int, sent_before: int) -> tuple[int, ...]:
    """List undeleted message IDs sent strictly before the caller's cutoff."""
    rows = cast(Sequence[tuple[int]], conn.execute(_SELECT_UNDELETED_MESSAGES_SQL, (dialog_id, sent_before)).fetchall())
    return tuple(int(message_id) for (message_id,) in rows)


def insert_messages_with_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
) -> None:
    """Persist message bundles in the caller-owned transaction."""
    projected = _overlay_message_transcriptions(conn, _preserve_transcribed_texts(conn, extracted))
    _write_message_rows_and_fts(conn, projected, priority=priority)
    _delete_entity_and_forward_projections(conn, projected)
    _replace_reaction_projections(conn, projected)
    _insert_entity_and_forward_projections(conn, projected)


def _write_message_rows_and_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    messages = [item.message for item in extracted]
    conn.executemany(
        _INSERT_MESSAGE_SQL, [{**asdict(item.message), "reply_count": item.reply_count} for item in extracted]
    )
    conn.executemany(DELETE_FTS_SQL, ((item.dialog_id, item.message_id) for item in messages))
    conn.executemany(INSERT_FTS_SQL, ((item.dialog_id, item.message_id, stem_text(item.text)) for item in messages))
    for message in messages:
        reconcile_fact_hydration_job(conn, message, due_at=int(time.time()), priority=priority)


def _overlay_message_transcriptions(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> list[_message_contracts.ExtractedMessage]:
    projected: list[_message_contracts.ExtractedMessage] = []
    for item in extracted:
        dialog_id, message_id = item.message.dialog_id, item.message.message_id
        row = cast(
            tuple[str, int] | None, conn.execute(_SELECT_MESSAGE_TRANSCRIPTION_SQL, (dialog_id, message_id)).fetchone()
        )
        if row is None:
            projected.append(item)
            continue
        fact = decode_media_fact(item.message.media_kind, item.message.media_payload)
        if _is_canonical_media_pair(
            item.message.media_kind, item.message.media_payload, fact=fact
        ) and is_transcribable_telegram_media(fact):
            projected.append(replace(item, message=replace(item.message, text=row[0])))
            continue
        if _is_canonical_media_pair(item.message.media_kind, item.message.media_payload, fact=fact):
            conn.execute(
                "DELETE FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?", (dialog_id, message_id)
            )
        projected.append(item)
    return projected


def _delete_entity_and_forward_projections(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> None:
    id_pairs = [(item.message.dialog_id, item.message.message_id) for item in extracted]
    conn.executemany(_DELETE_ENTITIES_SQL, id_pairs)
    conn.executemany(_DELETE_FORWARD_SQL, id_pairs)


def _replace_reaction_projections(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> None:
    for item in extracted:
        replace_reaction_aggregates(
            conn,
            item.message.dialog_id,
            item.message.message_id,
            tuple(ReactionAggregate(emoji=row.emoji, count=row.count) for row in item.reactions),
        )


def _insert_entity_and_forward_projections(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> None:
    entities = [entity for item in extracted for entity in item.entities]
    if entities:
        conn.executemany(_INSERT_ENTITY_SQL, [asdict(entity) for entity in entities])
    forwards = [item.forward for item in extracted if item.forward is not None]
    if forwards:
        conn.executemany(_INSERT_FORWARD_SQL, [asdict(forward) for forward in forwards])


def _preserve_transcribed_texts(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> list[_message_contracts.ExtractedMessage]:
    preserved_texts: dict[tuple[int, int], str] = {}
    for item in extracted:
        if item.message.text is not None and item.message.text.strip():
            continue
        if item.message.media_kind not in _FACT_HYDRATION_EMPTY_KINDS and item.message.media_kind != "voice":
            continue
        row = cast(
            tuple[str | None] | None,
            conn.execute(_SELECT_MESSAGE_TEXT_SQL, (item.message.dialog_id, item.message.message_id)).fetchone(),
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
