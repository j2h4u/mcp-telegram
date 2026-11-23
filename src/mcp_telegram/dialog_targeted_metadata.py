"""Merge one explicitly requested dialog without publishing directory coverage."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import cast

from telethon import utils  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]

from .dialog_directory import _eligibility_category, _staged_dialog_projection, apply_realtime_eligibility
from .dialog_directory_tl import RawDialogFact
from .dialog_fact_observations import claim_dialog_fact
from .read_state import apply_read_cursor
from .unread_state import apply_unread_facts


def capture_dialog_revision(conn: sqlite3.Connection, dialog_id: int) -> int | None:
    row = cast(
        tuple[int] | None, conn.execute("SELECT revision FROM dialogs WHERE dialog_id=?", (dialog_id,)).fetchone()
    )
    return None if row is None else int(row[0])


def publish_targeted_categories(
    conn: sqlite3.Connection, entities: tuple[object, ...], observed_at: int, order: int
) -> None:
    for entity in entities:
        category = _eligibility_category(entity)
        if category is None:
            continue
        dialog_id = int(utils.get_peer_id(entity))
        if claim_dialog_fact(conn, dialog_id, "category", order):
            apply_realtime_eligibility(conn, dialog_id, observed_at=observed_at, category=category)


def _top_message_date(dialog_id: int, top_message: int, messages: tuple[object, ...]) -> datetime | None:
    return next(
        (
            message.date
            for message in messages
            if isinstance(message, (types.Message, types.MessageService))
            and message.id == top_message
            and isinstance(getattr(message, "date", None), datetime)
            and getattr(message, "peer_id", None) is not None
            and utils.get_peer_id(message.peer_id) == dialog_id
        ),
        None,
    )


def _publish_read_cursors(conn: sqlite3.Connection, dialog_id: int, inbox: int | None, outbox: int | None) -> None:
    for kind, value in (("inbox", inbox), ("outbox", outbox)):
        if value is not None:
            apply_read_cursor(conn, dialog_id, "inbox" if kind == "inbox" else "outbox", value)
            column = "read_inbox_max_id" if kind == "inbox" else "read_outbox_max_id"
            conn.execute(
                f"UPDATE dialogs SET {column}=MAX(COALESCE({column},0),?) WHERE dialog_id=?", (value, dialog_id)
            )


def publish_targeted_dialog(  # noqa: PLR0913 - independent observation fences
    conn: sqlite3.Connection,
    dialog_id: int,
    dialog: object,
    messages: tuple[object, ...],
    *,
    observed_at: int,
    order: int,
    baseline_revision: int | None,
) -> None:
    if not isinstance(dialog, types.Dialog):
        return
    top_date = _top_message_date(dialog_id, dialog.top_message, messages)
    fact = RawDialogFact(dialog_id, None, dialog, top_date)
    observed = _staged_dialog_projection(fact, observed_at, None)
    if baseline_revision is None:
        conn.execute(
            "INSERT INTO dialogs(dialog_id,archived,pinned,last_message_at,snapshot_at,unread_mentions_count,"
            "unread_reactions_count,read_inbox_max_id,read_outbox_max_id) "
            "SELECT ?,?,?,?,?,?,?,?,? WHERE NOT EXISTS (SELECT 1 FROM dialogs WHERE dialog_id=?) "
            "AND NOT EXISTS (SELECT 1 FROM dialog_fact_observations WHERE dialog_id=? AND observation_order>?)",
            (
                dialog_id,
                observed.archived,
                observed.pinned,
                observed.last_message_at,
                observed_at,
                observed.unread_mentions_count,
                observed.unread_reactions_count,
                observed.read_inbox_max_id,
                observed.read_outbox_max_id,
                dialog_id,
                dialog_id,
                order,
            ),
        )
    else:
        conn.execute(
            "UPDATE dialogs SET pinned=?,last_message_at=COALESCE(?,last_message_at),snapshot_at=?,"
            "unread_mentions_count=?,unread_reactions_count=? WHERE dialog_id=? AND revision=?",
            (
                observed.pinned,
                observed.last_message_at,
                observed_at,
                observed.unread_mentions_count,
                observed.unread_reactions_count,
                dialog_id,
                baseline_revision,
            ),
        )
    apply_realtime_eligibility(
        conn,
        dialog_id,
        observed_at=observed_at,
        archived=observed.archived,
        mute_until=observed.mute_until,
        observation_order=order,
    )
    apply_unread_facts(
        conn,
        dialog_id,
        observed_at=observed_at,
        unread_count=observed.unread_count,
        unread_mark=None if observed.unread_mark is None else bool(observed.unread_mark),
        observation_order=order,
    )
    conn.execute(
        "UPDATE dialog_directory_facts SET unread=(SELECT CASE WHEN unread_count>0 OR unread_mentions_count>0 "
        "OR unread_mark=1 THEN 1 WHEN unread_count IS NOT NULL AND unread_mark IS NOT NULL THEN 0 ELSE NULL END "
        "FROM dialogs WHERE dialog_id=?) WHERE dialog_id=? AND EXISTS (SELECT 1 FROM dialogs WHERE dialog_id=?)",
        (dialog_id, dialog_id, dialog_id),
    )
    _publish_read_cursors(conn, dialog_id, observed.read_inbox_max_id, observed.read_outbox_max_id)
