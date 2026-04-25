"""Global own-message archive worker.

Populates own-message rows (out=1) in the unified messages table
via messages.Search(InputPeerEmpty, from_id=InputPeerSelf).
Runs as a named daemon background task alongside run_access_probe_loop.
"""
from __future__ import annotations

import asyncio
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

from .sync_worker import (
    ExtractedMessage,
    extract_message_row,
    insert_messages_with_fts,
)

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_S = 3600.0
_BACKFILL_BATCH_LIMIT = 100
_BACKFILL_INTER_BATCH_PAUSE_S = 0.5
# Upper bound on a single SearchRequest await. Prevents a wedged MTProto
# socket after startup FloodWait from hanging the incremental loop
# indefinitely (D-02 expert panel).
_SEARCH_RPC_TIMEOUT_S: float = 120.0

# Mirrors sync_worker.UPSERT_ENTITY_SQL (verified line 239).
# NOTE: `type` and `updated_at` are NOT NULL with no DEFAULT — both MUST be supplied.
UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities "
    "(id, type, name, username, name_normalized, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)

# Activity-sync dialog enrollment: 'own_only' is the lowest non-empty
# coverage status (D-2). INSERT OR IGNORE preserves higher-status rows
# already enrolled by FullSyncWorker (syncing/synced) or probe loops
# (fragment/access_lost). Status only escalates.
INSERT_OWN_ONLY_DIALOG_SQL = (
    "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) "
    "VALUES (?, 'own_only')"
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


async def _call_with_timeout(client: Any, request: Any) -> Any:
    """Invoke a Telethon RPC with a hard timeout and abandon on overrun.

    asyncio.wait_for awaits the wrapped task to actually finish after
    cancellation. Telethon's MTProto futures sometimes never resolve
    (post-startup flood wait corridor), causing wait_for to hang
    indefinitely past the requested deadline.

    asyncio.wait + explicit cancel returns immediately on timeout;
    the abandoned task is left to complete (or be GC'd) in the background
    rather than blocking our control flow.

    Raises TimeoutError on overrun. Re-raises FloodWaitError and other
    exceptions surfaced by the RPC call.
    """
    task = asyncio.create_task(client(request))
    done, _pending = await asyncio.wait({task}, timeout=_SEARCH_RPC_TIMEOUT_S)
    if not done:
        task.cancel()
        raise TimeoutError(f"RPC exceeded {_SEARCH_RPC_TIMEOUT_S}s deadline")
    return task.result()


async def _run_backfill(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
) -> None:
    state = _load_state(conn)
    if state.get("backfill_complete") == "1":
        logger.debug("activity_sync_backfill_skip reason=already_complete")
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
            result = await _call_with_timeout(
                client,
                SearchRequest(
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
                ),
            )
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
        except TimeoutError:
            logger.warning(
                "activity_sync_backfill_rpc_timeout offset_id=%d total_fetched=%d",
                checkpoint,
                total_fetched,
            )
            return  # outer run_activity_sync_loop re-enters after interval

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
        extracted: list[ExtractedMessage] = []
        for m in batch:
            # Step 1: resolve dialog_id from the Telethon message's peer.
            #   _extract_dialog_id returns None if the peer cannot be
            #   mapped (malformed / unexpected shape). Skip such rows —
            #   the canonical pipeline requires a concrete int dialog_id.
            dialog_id = _extract_dialog_id(m)
            if dialog_id is None:
                continue
            # Step 2: feed the resolved (dialog_id, msg) to the canonical
            #   extractor. extract_message_row builds a full StoredMessage
            #   plus reactions/entities/forward side-tables.
            extracted.append(extract_message_row(dialog_id, m))

        with conn:
            if extracted:
                insert_messages_with_fts(conn, extracted)
                dialog_ids = {em.message.dialog_id for em in extracted}
                conn.executemany(
                    INSERT_OWN_ONLY_DIALOG_SQL,
                    [(did,) for did in dialog_ids],
                )

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

    # Anchor by timestamp, not per-chat message_id. Global SearchRequest with
    # InputPeerEmpty returns messages from many dialogs — each with its own
    # message_id sequence. Using min_id=MAX(message_id) across chats causes
    # newer messages in dialogs with lower per-chat IDs to be silently skipped.
    # min_date is a wall-clock filter applied uniformly across all dialogs.
    last_sync_at = int(state.get("last_sync_at") or 0)
    if last_sync_at == 0:
        return

    # 60-second buffer guards against messages at the exact boundary being
    # missed when the previous sync finished mid-second.
    min_date = max(0, last_sync_at - 60)
    logger.info(
        "activity_sync_incremental_start min_date=%d window_s=%d",
        min_date,
        int(time.time()) - min_date,
    )

    inserted = 0
    batch_num = 0
    offset_id = 0  # start from newest
    while not shutdown_event.is_set():
        try:
            result = await _call_with_timeout(
                client,
                SearchRequest(
                    peer=InputPeerEmpty(),
                    q="",
                    filter=InputMessagesFilterEmpty(),
                    min_date=min_date,
                    max_date=None,
                    offset_id=offset_id,
                    add_offset=0,
                    limit=_BACKFILL_BATCH_LIMIT,
                    max_id=0,
                    min_id=0,
                    hash=0,
                    from_id=InputPeerSelf(),
                ),
            )
        except FloodWaitError as exc:
            logger.warning("activity_sync_incremental_floodwait seconds=%d", exc.seconds)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=float(exc.seconds))
                return  # shutdown fired during flood wait — exit cleanly
            except TimeoutError:
                continue
        except TimeoutError:
            logger.warning(
                "activity_sync_rpc_timeout offset_id=%d inserted=%d",
                offset_id,
                inserted,
            )
            _set_state(conn, "last_sync_at", str(int(time.time())))
            break

        batch = list(getattr(result, "messages", []) or [])
        if not batch:
            break

        extracted: list[ExtractedMessage] = []
        for m in batch:
            dialog_id = _extract_dialog_id(m)
            if dialog_id is None:
                continue
            extracted.append(extract_message_row(dialog_id, m))

        with conn:
            if extracted:
                insert_messages_with_fts(conn, extracted)
                dialog_ids = {em.message.dialog_id for em in extracted}
                conn.executemany(
                    INSERT_OWN_ONLY_DIALOG_SQL,
                    [(did,) for did in dialog_ids],
                )

        _upsert_entities_from_search(conn, result)
        inserted += len(batch)
        batch_num += 1
        offset_id = min(m.id for m in batch if getattr(m, "id", None) is not None)

        _set_state(conn, "last_sync_at", str(int(time.time())))
        logger.info(
            "activity_sync_incremental_batch batch=%d fetched=%d extracted=%d "
            "total_inserted=%d next_offset_id=%d",
            batch_num, len(batch), len(extracted), inserted, offset_id,
        )

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=_BACKFILL_INTER_BATCH_PAUSE_S)
            return
        except TimeoutError:
            pass

    _set_state(conn, "last_sync_at", str(int(time.time())))
    logger.info(
        "activity_sync_incremental_done batches=%d inserted=%d", batch_num, inserted
    )


async def run_activity_sync_loop(
    client: Any,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    interval: float = _DEFAULT_INTERVAL_S,
) -> None:
    """Background task: keep own-message rows (out=1) in messages up-to-date.

    One pass = (backfill if incomplete) + (incremental if backfill complete).
    Sleeps `interval` between passes, interruptible via shutdown_event.
    """
    while not shutdown_event.is_set():
        logger.info("activity_sync_loop_start")
        try:
            await _run_backfill(client, conn, shutdown_event)
            await _run_incremental(client, conn, shutdown_event)
        except Exception:
            logger.warning("activity_sync_error", exc_info=True)
        logger.info("activity_sync_loop_sleeping interval=%.0fs", interval)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
