"""Message serialization helpers for daemon API responses."""

import dataclasses
import sqlite3
from collections.abc import Sequence
from typing import cast

from .formatter import format_reaction_counts
from .message_content import MessageSnapshot, project_message_content
from .models import ReadMessage
from .reactions.contracts import ReactionFreshness
from .telegram_fact_queries import enrich_reaction_events, read_at_map
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
    except sqlite3.OperationalError:
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
    content = project_message_content(
        MessageSnapshot(
            text=message.text,
            media_description=message.media_description,
            media_kind=message.media_kind,
            text_links=tuple(text_links),
        )
    )
    return dataclasses.replace(
        message,
        text=content.text,
        media_description=content.media_description,
        content_kind=content.kind,
        media_kind=content.media_kind,
    )


def project_cached_message_facts_by_dialog(
    conn: sqlite3.Connection,
    messages: Sequence[ReadMessage],
) -> list[ReadMessage]:
    """Attach cached facts to a cross-dialog message list while preserving order."""
    grouped: dict[int, list[tuple[int, ReadMessage]]] = {}
    for index, message in enumerate(messages):
        grouped.setdefault(message.dialog_id, []).append((index, message))

    enriched: dict[int, ReadMessage] = {}
    for dialog_id, indexed_messages in grouped.items():
        dialog_messages = [message for _, message in indexed_messages]
        facts = project_cached_message_facts(conn, dialog_id, dialog_messages)
        for (index, _), message in zip(indexed_messages, facts, strict=True):
            enriched[index] = message
    return [enriched[index] for index in range(len(messages))]
