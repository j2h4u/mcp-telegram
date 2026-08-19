"""Canonical SQLite persistence for extracted Telegram messages."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import cast

from .. import message_contracts as _message_contracts
from ..fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from ..reactions.contracts import ReactionAggregate
from ..reactions.persistence import replace_reaction_aggregates

_UNSUPPORTED_MEDIA_DESCRIPTION = "[неподдерживаемый тип]"


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
_NEXT_VERSION_SQL = "SELECT COALESCE(MAX(version), 0) + 1 FROM message_versions WHERE dialog_id = ? AND message_id = ?"
_INSERT_VERSION_SQL = (
    "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date) VALUES (?, ?, ?, ?, ?)"
)
_UPDATE_MESSAGE_TEXT_SQL = "UPDATE messages SET text = ? WHERE dialog_id = ? AND message_id = ?"
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


def read_message_text(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> MessageTextLookup:
    """Read one message text without opening or committing a transaction."""
    row = cast(
        tuple[str | None] | None,
        conn.execute(_SELECT_MESSAGE_TEXT_SQL, (dialog_id, message_id)).fetchone(),
    )
    return MessageTextLookup(found=row is not None, text=None if row is None else row[0])


def persist_edited_message(
    conn: sqlite3.Connection,
    extracted: _message_contracts.ExtractedMessage,
    *,
    old_text: str | None,
    edit_date: int,
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
    insert_messages_with_fts(conn, [extracted])
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


def mark_message_deleted(conn: sqlite3.Connection, dialog_id: int, message_id: int, deleted_at: int) -> bool:
    """Tombstone one message and report whether this call changed its state."""
    cursor = conn.execute(_MARK_DELETED_SQL, (deleted_at, dialog_id, message_id))
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
) -> None:
    """Persist message bundles in the caller-owned transaction.

    Replaces FTS and child projections so edits are idempotent. It deliberately
    does not open or commit a transaction; callers compose it with their own
    state changes.
    """
    preserved = _preserve_transcribed_texts(conn, extracted)
    _write_message_rows_and_fts(conn, preserved)
    _delete_entity_and_forward_projections(conn, preserved)
    _replace_reaction_projections(conn, preserved)
    _insert_entity_and_forward_projections(conn, preserved)


def _write_message_rows_and_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
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
        if item.message.text or item.message.media_description != _UNSUPPORTED_MEDIA_DESCRIPTION:
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
