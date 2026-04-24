"""Global own-message archive worker.

Populates activity_comments table via messages.Search(InputPeerEmpty, from_id=InputPeerSelf).
Runs as a named daemon background task alongside run_access_probe_loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any

from telethon.errors import FloodWaitError
from telethon.tl.functions.messages import SearchRequest
from telethon.tl.types import (
    Channel,
    Chat,
    InputMessagesFilterEmpty,
    InputPeerEmpty,
    InputPeerSelf,
    User,
)

from .sync_worker import extract_reactions_rows

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 3600.0
_BACKFILL_BATCH_LIMIT = 100
_BACKFILL_INTER_BATCH_PAUSE_S = 0.5

INSERT_ACTIVITY_SQL = (
    "INSERT OR REPLACE INTO activity_comments "
    "(dialog_id, message_id, sent_at, text, reactions, reply_count, last_synced_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?)"
)

# Mirrors sync_worker.UPSERT_ENTITY_SQL (verified line 239).
# NOTE: `type` and `updated_at` are NOT NULL with no DEFAULT — both MUST be supplied.
UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities "
    "(id, type, name, username, name_normalized, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)


def _load_state(conn: sqlite3.Connection) -> dict[str, str | None]:
    rows = conn.execute(
        "SELECT key, value FROM activity_sync_state"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    with conn:
        conn.execute(
            "UPDATE activity_sync_state SET value=? WHERE key=?",
            (value, key),
        )


def _extract_dialog_id(msg: Any) -> int | None:
    peer = getattr(msg, "peer_id", None)
    if peer is None:
        return None
    # telethon.utils.get_peer_id handles User/Chat/Channel variants
    from telethon.utils import get_peer_id
    try:
        return get_peer_id(peer)
    except Exception:
        logger.warning("activity_sync_peer_id_unresolvable", exc_info=True)
        return None


def _message_to_row(msg: Any) -> tuple | None:
    dialog_id = _extract_dialog_id(msg)
    if dialog_id is None:
        return None
    sent_at = int(msg.date.timestamp()) if getattr(msg, "date", None) else None
    if sent_at is None:
        return None
    reaction_rows = extract_reactions_rows(dialog_id, msg.id, getattr(msg, "reactions", None))
    reactions_json: str | None = None
    if reaction_rows:
        reactions_json = json.dumps({r.emoji: r.count for r in reaction_rows})
    reply_count = getattr(getattr(msg, "replies", None), "replies", 0) or 0
    return (
        dialog_id,
        msg.id,
        sent_at,
        getattr(msg, "message", None),
        reactions_json,
        int(reply_count),
        int(time.time()),
    )


def _normalize(text: str | None) -> str | None:
    """Match the name_normalized convention used elsewhere (lower + strip)."""
    if not text:
        return None
    return text.strip().lower() or None


def _classify_entity(obj: Any) -> str | None:
    """Infer entities.type from Telethon object.

    Matches the taxonomy used elsewhere: 'user', 'bot', 'channel', 'group'.
    """
    if isinstance(obj, User):
        return "bot" if getattr(obj, "bot", False) else "user"
    if isinstance(obj, Channel):
        # megagroup=True → group; else channel (broadcast)
        return "group" if getattr(obj, "megagroup", False) else "channel"
    if isinstance(obj, Chat):
        return "group"
    return None


def _upsert_entities_from_search(conn: sqlite3.Connection, result: Any) -> None:
    """Upsert users/chats from SearchRequest response into entities table.

    Uses the FULL column set (id, type, name, username, name_normalized, updated_at).
    `type` and `updated_at` are NOT NULL with no DEFAULT — both MUST be supplied
    on every row or the INSERT will fail.
    """
    from telethon.utils import get_peer_id

    now = int(time.time())
    rows: list[tuple[int, str, str | None, str | None, str | None, int]] = []

    for u in getattr(result, "users", []) or []:
        etype = _classify_entity(u)
        if etype is None:
            continue
        name = " ".join(
            p for p in (getattr(u, "first_name", None), getattr(u, "last_name", None)) if p
        ) or (getattr(u, "username", None) or None)
        username = getattr(u, "username", None)
        rows.append((int(u.id), etype, name, username, _normalize(name), now))

    for c in getattr(result, "chats", []) or []:
        etype = _classify_entity(c)
        if etype is None:
            continue
        try:
            pid = get_peer_id(c)  # yields -100XXXXX for Channel
        except Exception:
            continue
        name = getattr(c, "title", None) or None
        username = getattr(c, "username", None)
        rows.append((int(pid), etype, name, username, _normalize(name), now))

    if not rows:
        return
    with conn:
        conn.executemany(UPSERT_ENTITY_SQL, rows)


def _fmt_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


async def _run_backfill(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    state = _load_state(conn)
    if state.get("backfill_complete") == "1":
        return

    checkpoint = int(state.get("backfill_offset_id") or 0)

    # Mark that backfill has started so scan_status can distinguish
    # "never touched" from "running but not yet done".
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO activity_sync_state (key, value) VALUES ('backfill_started_at', ?)",
            (str(int(time.time())),),
        )

    logger.info("activity_sync_backfill_start offset_id=%d", checkpoint)

    total_fetched = 0
    total_known: int | None = None  # filled from result.count on first batch
    batch_num = 0
    loop_start = time.monotonic()

    while not shutdown_event.is_set():
        try:
            result = await client(SearchRequest(
                peer=InputPeerEmpty(),
                q="",
                filter=InputMessagesFilterEmpty(),
                min_date=None,
                max_date=None,
                offset_id=checkpoint,
                add_offset=0,
                limit=_BACKFILL_BATCH_LIMIT,
                max_id=0,
                min_id=0,
                hash=0,
                from_id=InputPeerSelf(),
            ))
        except FloodWaitError as exc:
            logger.warning(
                "activity_sync_floodwait seconds=%d total_fetched=%d",
                exc.seconds, total_fetched,
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=float(exc.seconds))
                return
            except TimeoutError:
                continue

        if total_known is None:
            total_known = getattr(result, "count", None)
            if total_known is not None:
                logger.info("activity_sync_backfill_total total=%d", total_known)

        batch = list(getattr(result, "messages", []) or [])
        if not batch:
            _set_state(conn, "backfill_complete", "1")
            _set_state(conn, "last_sync_at", str(int(time.time())))
            logger.info(
                "activity_sync_backfill_complete total_fetched=%d", total_fetched,
            )
            return

        batch_num += 1
        rows = [r for r in (_message_to_row(m) for m in batch) if r is not None]
        with conn:
            if rows:
                conn.executemany(INSERT_ACTIVITY_SQL, rows)

        _upsert_entities_from_search(conn, result)

        total_fetched += len(batch)
        new_checkpoint = min(m.id for m in batch if getattr(m, "id", None) is not None)
        _set_state(conn, "backfill_offset_id", str(new_checkpoint))
        checkpoint = new_checkpoint

        elapsed = time.monotonic() - loop_start
        rate = total_fetched / elapsed if elapsed > 0 else 0.0
        if total_known is not None:
            remaining = total_known - total_fetched
            eta_s = int(remaining / rate) if rate > 0 else None
            eta_str = _fmt_duration(eta_s) if eta_s is not None else "?"
            logger.info(
                "activity_sync_backfill_batch batch=%d fetched=%d total=%d/%d"
                " rate=%.0f/s eta=%s offset_id=%d",
                batch_num, len(batch), total_fetched, total_known,
                rate, eta_str, checkpoint,
            )
        else:
            logger.info(
                "activity_sync_backfill_batch batch=%d fetched=%d total=%d"
                " rate=%.0f/s offset_id=%d",
                batch_num, len(batch), total_fetched, rate, checkpoint,
            )

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=_BACKFILL_INTER_BATCH_PAUSE_S)
            return
        except TimeoutError:
            pass


async def _run_incremental(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    state = _load_state(conn)
    if state.get("backfill_complete") != "1":
        return

    row = conn.execute("SELECT MAX(sent_at) FROM activity_comments").fetchone()
    max_sent_at = int(row[0]) if row and row[0] is not None else 0

    # W5: If activity_comments is empty (backfill just flipped to complete but no
    # own messages exist for this account), skip the iteration entirely.
    if max_sent_at == 0:
        return

    inserted = 0
    try:
        async for msg in client.iter_messages(entity=None, from_user="me"):
            if shutdown_event.is_set():
                break
            sent_at = int(msg.date.timestamp()) if getattr(msg, "date", None) else 0
            if sent_at <= max_sent_at:
                break
            r = _message_to_row(msg)
            if r is None:
                continue
            with conn:
                conn.execute(INSERT_ACTIVITY_SQL, r)
            inserted += 1
    except FloodWaitError as exc:
        logger.warning("activity_sync_incremental_floodwait seconds=%d", exc.seconds)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=float(exc.seconds))
        except TimeoutError:
            pass

    _set_state(conn, "last_sync_at", str(int(time.time())))
    if inserted:
        logger.info("activity_sync_incremental_inserted count=%d", inserted)


async def run_activity_sync_loop(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_INTERVAL_S,
) -> None:
    """Background task: keep activity_comments up-to-date.

    One pass = (backfill if incomplete) + (incremental if backfill complete).
    Sleeps `interval` between passes, interruptible via shutdown_event.
    """
    while not shutdown_event.is_set():
        try:
            await _run_backfill(client, conn, shutdown_event)
            await _run_incremental(client, conn, shutdown_event)
        except Exception:
            logger.warning("activity_sync_error", exc_info=True)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
