"""Canonical SQLite persistence for extracted Telegram messages."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, fields, replace
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
