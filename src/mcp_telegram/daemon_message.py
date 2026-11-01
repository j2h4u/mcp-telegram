"""Message serialization helpers for daemon API responses."""

import dataclasses
import json
import sqlite3
from collections.abc import Sequence
from typing import cast

from .formatter import format_reaction_counts
from .message_content import project_message_text
from .models import ContentKind, ReadMessage
from .reactions.contracts import ReactionFreshness
from .telegram_fact_queries import enrich_reaction_events, reaction_event_projection_by_message, read_at_map
from .telegram_message_projection import MessageLike as _MessageLike
from .telegram_message_projection import message_to_dict

__all__ = [
    "_MessageLike",
    "cached_reaction_freshness",
    "fetch_reaction_counts",
    "fetch_text_links",
    "message_to_dict",
    "project_cached_message_facts",
    "project_cached_message_facts_by_dialog",
    "project_read_message_content",
]


def cached_reaction_freshness(message_count: int) -> ReactionFreshness:
    """Return a local-only freshness marker for a projected message page."""
    return ReactionFreshness(
        requested_count=message_count,
        fresh_count=0,
        stale_count=0,
        refreshed_count=0,
        status="cached_only",
    )


def fetch_text_links(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: list[int],
) -> dict[int, list[tuple[int, int, str]]]:
    """Return persisted Telegram hidden links for one message page."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    try:
        rows = cast(
            list[tuple[int | str, int | str, int | str, object]],
            conn.execute(
                f"SELECT message_id, offset, length, value FROM message_entities "
                f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
                "AND type = 'text_url' AND value IS NOT NULL ORDER BY message_id, offset",
                [dialog_id, *message_ids],
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return {}
    result: dict[int, list[tuple[int, int, str]]] = {}
    for message_id, offset, length, value in rows:
        result.setdefault(int(message_id), []).append((int(offset), int(length), str(value)))
    return result


def fetch_reaction_counts(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: list[int],
) -> dict[int, list[tuple[str, int]]]:
    """Return `{message_id: [(emoji, count), ...]}` for the given page."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = cast(
        list[tuple[int | float | str, object, int | float | str]],
        conn.execute(
            f"SELECT message_id, emoji, count FROM message_reactions "
            f"WHERE dialog_id = ? AND message_id IN ({placeholders}) "
            f"ORDER BY count DESC, emoji",
            [dialog_id, *message_ids],
        ).fetchall(),
    )
    result: dict[int, list[tuple[str, int]]] = {}
    for msg_id, emoji, count in rows:
        result.setdefault(int(msg_id), []).append((str(emoji), int(count)))
    return result


def project_cached_message_facts(
    conn: sqlite3.Connection,
    dialog_id: int,
    messages: Sequence[ReadMessage],
) -> list[ReadMessage]:
    """Attach cached reactions, reaction events, and read dates to messages."""
    if not messages:
        return list(messages)
    message_ids = [message.message_id for message in messages]
    reaction_map = fetch_reaction_counts(conn, dialog_id, message_ids)
    text_link_map = fetch_text_links(conn, dialog_id, message_ids)
    read_dates = read_at_map(conn, dialog_id, message_ids)
    with_reactions = [
        _project_cached_message(
            message,
            text_links=text_link_map.get(message.message_id, []),
            reactions_display=format_reaction_counts(reaction_map.get(message.message_id, [])),
        )
        for message in messages
    ]
    with_events = enrich_reaction_events(conn, dialog_id, with_reactions)
    return [dataclasses.replace(message, read_at=read_dates.get(message.message_id)) for message in with_events]


def _project_cached_message(
    message: ReadMessage,
    *,
    text_links: list[tuple[int, int, str]],
    reactions_display: str,
) -> ReadMessage:
    projected = project_read_message_content(message, text_links=text_links)
    return dataclasses.replace(projected, reactions_display=reactions_display)


def project_read_message_content(
    message: ReadMessage,
    *,
    text_links: Sequence[tuple[int, int, str]] = (),
) -> ReadMessage:
    """Project one read-model message through canonical content semantics."""
    rendered_text = project_message_text(message.text, tuple(text_links))
    content_kind: ContentKind = (
        "message_text" if rendered_text is not None else ("media_description" if message.media_description else "none")
    )
    return dataclasses.replace(
        message,
        text=rendered_text,
        media_description=message.media_description,
        content_kind=content_kind,
        media_kind=message.media_kind,
    )


def project_cached_message_facts_by_dialog(
    conn: sqlite3.Connection,
    messages: Sequence[ReadMessage],
) -> list[ReadMessage]:
    """Attach cached facts to a cross-dialog message list while preserving order."""
    if not messages:
        return []
    keys = json.dumps(list({(message.dialog_id, message.message_id) for message in messages}))
    reactions = _batch_message_facts(conn, keys, "message_reactions", "emoji, count", "ORDER BY count DESC, emoji")
    links = _batch_message_facts(
        conn,
        keys,
        "message_entities",
        "offset, length, value",
        "AND type='text_url' AND value IS NOT NULL ORDER BY t.message_id, offset",
        optional=True,
    )
    dates = _batch_message_facts(conn, keys, "message_read_facts", "read_at", "")
    events, statuses = reaction_event_projection_by_message(conn, keys)
    result = []
    for message in messages:
        key = (message.dialog_id, message.message_id)
        projected = _project_cached_message(
            message,
            text_links=[
                (int(cast(int | str, row[0])), int(cast(int | str, row[1])), str(row[2])) for row in links.get(key, [])
            ],
            reactions_display=format_reaction_counts(
                [(str(row[0]), int(cast(int | str, row[1]))) for row in reactions.get(key, [])]
            ),
        )
        date_rows = dates.get(key, [])
        read_at = None if not date_rows or date_rows[0][0] is None else int(cast(int | str, date_rows[0][0]))
        result.append(
            dataclasses.replace(
                projected,
                reaction_events=events.get(key, ()),
                reaction_events_status=statuses.get(key, "unavailable"),
                read_at=read_at,
            )
        )
    return result


def _batch_message_facts(  # noqa: PLR0913 - query parts describe one bounded fact lookup
    conn: sqlite3.Connection, keys: str, table: str, fields: str, suffix: str, *, optional: bool = False
) -> dict[tuple[int, int], list[tuple[object, ...]]]:
    try:
        rows = cast(
            list[tuple[int, int, *tuple[object, ...]]],
            conn.execute(
                "WITH keys AS MATERIALIZED (SELECT json_extract(value,'$[0]') AS dialog_id, "
                "json_extract(value,'$[1]') AS message_id FROM json_each(?)) "
                f"SELECT t.dialog_id, t.message_id, {fields} FROM keys k CROSS JOIN {table} t "
                "WHERE t.dialog_id=k.dialog_id AND t.message_id=k.message_id "
                f"{suffix}",
                (keys,),
            ).fetchall(),
        )
    except sqlite3.OperationalError as exc:
        if not optional or "no such table" not in str(exc).lower():
            raise
        return {}
    result: dict[tuple[int, int], list[tuple[object, ...]]] = {}
    for dialog_id, message_id, *values in rows:
        result.setdefault((int(dialog_id), int(message_id)), []).append(tuple(values))
    return result
