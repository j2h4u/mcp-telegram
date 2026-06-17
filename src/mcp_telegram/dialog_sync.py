"""Dialog snapshot synchronisation — bootstrap sweep + (Phase 43) reconciliation.

Phase 41: DialogsBootstrapWorker performs a single iter_dialogs() sweep that
populates the v17 `dialogs` snapshot table. The sweep is resumable via a
checkpoint cursor stored in `daemon_state` (v18 KV table). Each dialog's
UPSERT and the cursor write happen in a single transaction so a kill at any
moment leaves a consistent state — the next start either skips a completed
sweep or resumes from the last successful dialog.

Connection ownership
--------------------
The worker opens its OWN dedicated SQLite connection via _open_sync_db(db_path)
in __init__ and closes it in run()'s finally block. It does NOT share the
daemon's main connection — concurrent background tasks (FullSyncWorker,
DeltaSyncWorker, EventHandlerManager, access probe, activity_sync, read-position
init, total-message backfill) write through their own paths, and SQLite WAL +
busy_timeout=10000 (configured by _open_sync_db) handles cross-connection
serialization safely. This isolation was added per Phase 41 review HIGH finding.

Phase 42 inter-phase contract (CRITICAL)
----------------------------------------
The recency guard `WHERE dialogs.snapshot_at < excluded.snapshot_at` evaluates
to NULL (false) when the existing row has snapshot_at = NULL — the UPDATE
silently SKIPS those rows. Phase 42 event handlers MUST always write non-NULL
snapshot_at on every insert/update path. The dialogs DDL declares snapshot_at
as INTEGER NOT NULL, but downstream code must not bypass that with explicit
NULL writes. See _UPSERT_DIALOG_SQL comment block.

BOOTSTRAP requirements coverage
-------------------------------
- BOOTSTRAP-01: iter_dialogs() sweep populates `dialogs`.
- BOOTSTRAP-03: FloodWait → interruptible sleep, no crash (D-13).
- BOOTSTRAP-04: cursor checkpoint enables mid-sweep resume.
- BOOTSTRAP-06: INSERT ... ON CONFLICT ... DO UPDATE ... WHERE ... < excluded.snapshot_at —
  bootstrap never clobbers fresher event-handler writes (D-12).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Protocol, TypedDict, TypeVar, cast

from telethon.errors import (  # type: ignore[import-untyped]
    ChannelBannedError,
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerIdInvalidError,
    RPCError,
    UserBannedInChannelError,
    UserKickedError,
)
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import GetForumTopicsRequest  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
    TypeInputPeer,
)

from .flood import flood_seconds, sleep_through_flood
from .sync_db import _open_sync_db

logger = logging.getLogger(__name__)
T = TypeVar("T")


class _EntityLike(Protocol):
    id: int
    title: str | None
    first_name: str | None
    last_name: str | None
    username: str | None
    access_hash: int | None
    bot: bool
    broadcast: bool
    participants_count: int | None
    date: datetime | None
    forum: bool


class _DraftLike(Protocol):
    message: str | None


class _MessageLike(Protocol):
    date: datetime | None


class _DialogLike(Protocol):
    id: int
    entity: _EntityLike
    message: _MessageLike | None
    pinned: bool
    folder_id: int | None
    unread_mentions_count: int | None
    unread_reactions_count: int | None
    draft: _DraftLike | None
    date: datetime | None


class _ForumTopicLike(Protocol):
    id: int
    title: str | None
    is_general: bool
    icon_emoji_id: int | None
    date: datetime | None


class _ForumTopicsResultLike(Protocol):
    topics: list[_ForumTopicLike]


class _BootstrapRow(TypedDict):
    dialog_id: int
    name: str | None
    type: str
    archived: int
    pinned: int
    members: int | None
    created: int | None
    last_message_at: int | None
    snapshot_at: int
    unread_mentions_count: int
    unread_reactions_count: int
    draft_text: str | None


class _EntityFields(TypedDict):
    name: str | None
    type: str
    members: int | None
    created: int | None


class _DialogSyncClient(Protocol):
    def iter_dialogs(self, **kwargs: object) -> AsyncIterator[_DialogLike]: ...

    async def get_entity(self, peer: object) -> _EntityLike: ...

    async def __call__(self, request: object) -> _ForumTopicsResultLike: ...


def _attr[T](obj: object, name: str, default: T) -> T:
    return cast(T, getattr(obj, name, default))

# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_GET_STATE_SQL = "SELECT value FROM daemon_state WHERE key = ?"
_SET_STATE_SQL = "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)"
_DELETE_STATE_SQL = "DELETE FROM daemon_state WHERE key = ?"

# D-12: UPSERT carries `WHERE dialogs.snapshot_at < excluded.snapshot_at` so
# bootstrap data only overwrites rows where its `snapshot_at` is newer —
# event-handler-written rows (Phase 42) with a fresher timestamp are never
# clobbered. `hidden` and `needs_refresh` (D-11) are deliberately excluded
# from the UPDATE clause.
#
# CRITICAL inter-phase contract for Phase 42:
#   `dialogs.snapshot_at < excluded.snapshot_at` evaluates to NULL (false)
#   when the existing row has snapshot_at = NULL — the UPDATE silently
#   SKIPS those rows. Phase 42 event handlers MUST always write non-NULL
#   snapshot_at on every insert/update. The DDL declares snapshot_at as
#   INTEGER NOT NULL, but downstream code must not bypass that with
#   explicit NULL writes.
_UPSERT_DIALOG_SQL = """
INSERT INTO dialogs (
    dialog_id, name, type, archived, pinned, members, created,
    last_message_at, snapshot_at, hidden, needs_refresh,
    unread_mentions_count, unread_reactions_count, draft_text
) VALUES (
    :dialog_id, :name, :type, :archived, :pinned, :members, :created,
    :last_message_at, :snapshot_at, 0, 0,
    :unread_mentions_count, :unread_reactions_count, :draft_text
)
ON CONFLICT(dialog_id) DO UPDATE SET
    name = excluded.name,
    type = excluded.type,
    archived = excluded.archived,
    pinned = excluded.pinned,
    members = excluded.members,
    created = excluded.created,
    last_message_at = excluded.last_message_at,
    snapshot_at = excluded.snapshot_at,
    unread_mentions_count = excluded.unread_mentions_count,
    unread_reactions_count = excluded.unread_reactions_count,
    draft_text = excluded.draft_text
WHERE dialogs.snapshot_at < excluded.snapshot_at
"""

# daemon_state keys (D-02, D-03)
_KEY_STATUS = "bootstrap_sweep_status"
_KEY_OFFSET_DATE = "bootstrap_sweep_offset_date"
_KEY_OFFSET_ID = "bootstrap_sweep_offset_id"
_KEY_OFFSET_PEER = "bootstrap_sweep_offset_peer"
_CURSOR_KEYS = (_KEY_OFFSET_DATE, _KEY_OFFSET_ID, _KEY_OFFSET_PEER)

_STATUS_IN_PROGRESS = "in_progress"
_STATUS_COMPLETE = "complete"

# Progress-reporting cadence (every Nth dialog updates startup_detail)
_PROGRESS_REPORT_EVERY = 50

# ---------------------------------------------------------------------------
# Access-loss handling (RECON-04). Canonical home for both the error
# tuple and the atomic transition helper. sync_worker.py and
# delta_sync.py import these symbols from here.
# ---------------------------------------------------------------------------

_ACCESS_LOST_ERRORS = (
    ChannelPrivateError,
    ChatForbiddenError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserKickedError,
    ChannelBannedError,
)

_SET_ACCESS_LOST_SQL = "UPDATE synced_dialogs SET status = 'access_lost', access_lost_at = ? WHERE dialog_id = ?"
_SET_DIALOGS_HIDDEN_SQL = "UPDATE dialogs SET hidden = 1, snapshot_at = ? WHERE dialog_id = ?"


def _set_access_lost(conn: sqlite3.Connection, dialog_id: int, now: int) -> None:
    """Atomic access-loss transition (RECON-04).

    Writes synced_dialogs.status='access_lost' and dialogs.hidden=1 in a
    single transaction. UPDATEs against a missing row are no-ops; safe
    to call even when only one of the two tables has the dialog_id.
    """
    with conn:
        conn.execute(_SET_ACCESS_LOST_SQL, (now, dialog_id))
        conn.execute(_SET_DIALOGS_HIDDEN_SQL, (now, dialog_id))


# ---------------------------------------------------------------------------
# Reconciliation SQL (Phase 43)
# ---------------------------------------------------------------------------

_SELECT_DIRTY_DIALOGS_SQL = "SELECT dialog_id FROM dialogs WHERE needs_refresh = 1 AND hidden = 0"

_UPSERT_TOPIC_FROM_RECON_SQL = """
INSERT INTO topic_metadata
    (dialog_id, topic_id, title, top_message_id,
     is_general, is_deleted, updated_at,
     icon_emoji_id, pinned, hidden, snapshot_at, date)
VALUES
    (:dialog_id, :topic_id, :title, NULL,
     :is_general, 0, :updated_at,
     :icon_emoji_id, 0, 0, :snapshot_at, :date)
ON CONFLICT(dialog_id, topic_id) DO UPDATE SET
    title          = COALESCE(excluded.title, topic_metadata.title),
    icon_emoji_id  = COALESCE(excluded.icon_emoji_id, topic_metadata.icon_emoji_id),
    is_general     = excluded.is_general,
    updated_at     = excluded.updated_at,
    snapshot_at    = excluded.snapshot_at,
    date           = COALESCE(excluded.date, topic_metadata.date)
WHERE topic_metadata.snapshot_at IS NULL
   OR topic_metadata.snapshot_at < excluded.snapshot_at
"""
_UPDATE_DIALOG_ENTITY_SQL = (
    "UPDATE dialogs SET name=?, type=?, members=?, created=?, needs_refresh=0, snapshot_at=? WHERE dialog_id=?"
)
_HIDE_DIALOG_SQL = "UPDATE dialogs SET hidden=1, snapshot_at=? WHERE dialog_id=? AND hidden=0"
_SELECT_VISIBLE_DIALOG_IDS_SQL = "SELECT dialog_id FROM dialogs WHERE hidden = 0"

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = cast(tuple[str | None] | None, conn.execute(_GET_STATE_SQL, (key,)).fetchone())
    return row[0] if row else None


def _set_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(_SET_STATE_SQL, (key, value))


def _clear_cursor(conn: sqlite3.Connection) -> None:
    """Delete all cursor rows (used after a corrupt-state recovery)."""
    for k in _CURSOR_KEYS:
        conn.execute(_DELETE_STATE_SQL, (k,))


# ---------------------------------------------------------------------------
# offset_peer encode/decode (Telethon InputPeer round-trip)
# ---------------------------------------------------------------------------


def _encode_offset_peer(entity: _EntityLike) -> str | None:
    """Serialize a Telethon entity to a JSON cursor record.

    Returns None for unknown entity types — caller writes NULL for offset_peer
    (no fake channel-with-id-0 cursors). access_hash may be None on
    privacy-restricted entities — guard with `or 0`.
    """
    if isinstance(entity, types.User):
        return json.dumps({"type": "user", "id": entity.id, "access_hash": entity.access_hash or 0})
    if isinstance(entity, types.Chat):
        return json.dumps({"type": "chat", "id": entity.id, "access_hash": 0})
    if isinstance(entity, types.Channel):
        return json.dumps({"type": "channel", "id": entity.id, "access_hash": entity.access_hash or 0})
    # Unknown entity type — log and refuse to fabricate a cursor (review LOW).
    logger.warning(
        "bootstrap_sweep unknown entity type=%s — offset_peer not encoded",
        type(entity).__name__,
    )
    return None


def _decode_offset_peer(json_str: str) -> object:
    """Reconstruct a Telethon InputPeer from the JSON cursor record.

    Raises ValueError on malformed JSON or missing keys — caller catches and
    triggers cursor reset (corrupt-state recovery).
    """
    d = cast(dict[str, object], json.loads(json_str))
    t = d["type"]
    peer_id = int(cast(int | str, d["id"]))
    ah = int(cast(int | str, d.get("access_hash", 0) or 0))
    if t == "user":
        return InputPeerUser(peer_id, ah)
    if t == "chat":
        return InputPeerChat(peer_id)
    if t == "channel":
        return InputPeerChannel(peer_id, ah)
    raise ValueError(f"unknown offset_peer type: {t!r}")


# ---------------------------------------------------------------------------
# Per-dialog row extraction
# ---------------------------------------------------------------------------


def _extract_entity_fields(entity: _EntityLike) -> _EntityFields:
    """Return {name, type, members, created} from a bare entity (User/Chat/Channel).

    Single source of truth for entity-type dispatch (RECON-02 + 43-REVIEWS.md
    "Make _extract_entity_fields refactor mandatory"). Called by:
      - _extract_dialog_row (full pass — has a Dialog wrapper, passes dialog.entity)
      - DialogReconciliationWorker.run_light_pass (no Dialog wrapper — get_entity result)
    """
    if isinstance(entity, types.User):
        dialog_type = "bot" if entity.bot else "user"
        members = None
        created = None
    elif isinstance(entity, types.Chat):
        dialog_type = "group"
        members = _attr(entity, "participants_count", None)
        created = None
    elif isinstance(entity, types.Channel):
        dialog_type = "channel" if entity.broadcast else "supergroup"
        members = _attr(entity, "participants_count", None)
        date = entity.date
        created = int(date.timestamp()) if date else None
    else:
        dialog_type = "unknown"
        members = None
        created = None
    return {
        "name": _extract_name(entity),
        "type": dialog_type,
        "members": members,
        "created": created,
    }


def _extract_name(entity: _EntityLike) -> str | None:
    """Build a display name from a Telethon entity (User/Chat/Channel)."""
    title = _attr(entity, "title", None)
    if title:
        return title
    first = _attr(entity, "first_name", None) or ""
    last = _attr(entity, "last_name", None) or ""
    name = f"{first} {last}".strip()
    return name or None


def _extract_dialog_row(dialog: _DialogLike, snapshot_at: int) -> _BootstrapRow:
    """Build the dict bound to _UPSERT_DIALOG_SQL for one Dialog object.

    All values come from the Dialog object and dialog.entity directly — no
    extra RPCs:
    - D-08: members/created from dialog.entity (Channel/Chat); NULL for User.
    - D-09: unread_mentions/reactions from dialog directly.
    - D-10: draft_text = dialog.draft.message[:80] (DIFF-03 truncation).
    - D-11: needs_refresh = 0 for all bootstrap rows (handled in INSERT clause).
    """
    entity = dialog.entity
    fields = _extract_entity_fields(entity)
    name = fields["name"]
    dialog_type = fields["type"]
    members = fields["members"]
    created = fields["created"]

    last_msg = dialog.message
    last_message_at: int | None = None
    if last_msg is not None:
        last_message_date = last_msg.date
        if last_message_date is not None:
            last_message_at = int(last_message_date.timestamp())

    # D-09: unread_mentions / unread_reactions from Dialog object directly.
    unread_mentions = int(dialog.unread_mentions_count or 0)
    unread_reactions = int(dialog.unread_reactions_count or 0)

    # D-10: draft_text = dialog.draft.message[:80] if present, else NULL.
    draft = dialog.draft
    draft_text: str | None = None
    if draft is not None:
        msg = draft.message
        if msg:
            draft_text = msg[:80]  # DIFF-03

    return {
        "dialog_id": int(dialog.id),
        "name": name,
        "type": dialog_type,
        # `archived` is True iff dialog.folder_id is not None
        "archived": int(dialog.folder_id is not None),
        "pinned": int(bool(dialog.pinned)),
        "members": members,
        "created": created,
        "last_message_at": last_message_at,
        "snapshot_at": snapshot_at,
        "unread_mentions_count": unread_mentions,
        "unread_reactions_count": unread_reactions,
        "draft_text": draft_text,
    }


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class DialogsBootstrapWorker:
    """Single-pass iter_dialogs() sweep that populates the `dialogs` table.

    Resumable via a cursor checkpoint in `daemon_state`. Idempotent — once the
    completion flag is written, subsequent runs short-circuit without calling
    iter_dialogs(). FloodWait causes an interruptible sleep; the daemon's
    shutdown_event wakes it before the full wait elapses.

    Connection ownership: takes `db_path` and opens its own dedicated SQLite
    connection via `_open_sync_db(db_path)` in __init__. Closes it in run()'s
    finally block. This isolation eliminates write contention with other
    background tasks that share the daemon's main connection. WAL mode +
    busy_timeout=10s (configured by _open_sync_db) makes cross-connection
    serialization safe.

    Constructor takes only what the worker owns — the daemon supplies an
    optional `startup_detail_setter` lambda so the worker can update
    api_server.startup_detail without depending on DaemonAPIServer directly.
    """

    def __init__(
        self,
        client: object,
        db_path: Path,
        shutdown_event: asyncio.Event,
        *,
        startup_detail_setter: Callable[[str], None] | None = None,
    ) -> None:
        self._client = cast(_DialogSyncClient, client)
        # Open a dedicated connection — NOT shared with the daemon's main conn.
        # See module docstring "Connection ownership". Same pattern as
        # _backfill_in_thread() at daemon.py:449-456.
        self._conn = _open_sync_db(db_path)
        self._shutdown_event = shutdown_event
        self._startup_detail_setter = startup_detail_setter

    def _set_detail(self, msg: str) -> None:
        """Forward to startup_detail_setter if provided (None-safe)."""
        if self._startup_detail_setter is not None:
            self._startup_detail_setter(msg)

    def _reconstruct_cursor(self) -> tuple[datetime | None, int, object | None]:
        """Read offset_date / offset_id / offset_peer from daemon_state.

        Returns (offset_date, offset_id, offset_peer) — any may be None/0
        meaning "no cursor for that field, start from the beginning".

        On corrupt state (malformed isoformat or JSON), logs a WARNING,
        clears all cursor keys, and returns the fresh-start tuple. This
        prevents a corrupt daemon_state row from bricking daemon startup
        forever (review MEDIUM finding).
        """
        offset_date_str = _get_state(self._conn, _KEY_OFFSET_DATE)
        offset_id_str = _get_state(self._conn, _KEY_OFFSET_ID)
        offset_peer_str = _get_state(self._conn, _KEY_OFFSET_PEER)

        try:
            offset_date = datetime.fromisoformat(offset_date_str) if offset_date_str else None
            offset_id = int(offset_id_str) if offset_id_str else 0
            offset_peer = _decode_offset_peer(offset_peer_str) if offset_peer_str else None
            return offset_date, offset_id, offset_peer
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning(
                "bootstrap_sweep cursor corrupt (%s) — clearing cursor and restarting from scratch",
                exc,
            )
            with self._conn:
                _clear_cursor(self._conn)
            return None, 0, None

    async def run(self) -> int:
        """Run (or skip) the bootstrap sweep. Returns count of dialogs processed.

        Returns 0 if the sweep is already complete or if it exits early on
        FloodWait/RPCError/shutdown. Caller does not need to inspect the
        return value — daemon_state holds the persistent state.

        The dedicated connection is closed in the finally block.
        """
        try:
            status = _get_state(self._conn, _KEY_STATUS)
            if status == _STATUS_COMPLETE:
                logger.info("bootstrap_sweep already complete — skipping")
                return 0

            offset_date, offset_id, offset_peer = self._reconstruct_cursor()

            # Mark in_progress at the very start so a kill before the first dialog
            # still leaves a recognisable resume signal.
            with self._conn:
                _set_state(self._conn, _KEY_STATUS, _STATUS_IN_PROGRESS)

            count = 0
            try:
                async for dialog in self._client.iter_dialogs(
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_peer=offset_peer if offset_peer is not None else types.InputPeerEmpty(),
                ):
                    if self._shutdown_event.is_set():
                        logger.info(
                            "bootstrap_sweep shutdown signal received — exiting (count=%d)",
                            count,
                        )
                        return count

                    snapshot_at = int(time.time())
                    row = _extract_dialog_row(dialog, snapshot_at)

                    # Atomic: UPSERT + cursor checkpoint in a single transaction.
                    # BOOTSTRAP-04 requires the cursor to advance only with the
                    # row, never separately, so a kill never leaves an
                    # inconsistent state.
                    with self._conn:
                        self._conn.execute(_UPSERT_DIALOG_SQL, row)
                        dialog_date = dialog.date
                        _set_state(
                            self._conn,
                            _KEY_OFFSET_DATE,
                            dialog_date.isoformat() if dialog_date is not None else None,
                        )
                        _set_state(self._conn, _KEY_OFFSET_ID, str(int(dialog.id)))
                        encoded_peer = _encode_offset_peer(dialog.entity)
                        _set_state(self._conn, _KEY_OFFSET_PEER, encoded_peer)
                        _set_state(self._conn, _KEY_STATUS, _STATUS_IN_PROGRESS)

                    count += 1
                    if count % _PROGRESS_REPORT_EVERY == 0:
                        self._set_detail(f"bootstrap sweep: {count} dialogs processed")

            except FloodWaitError as exc:
                wait_s = flood_seconds(exc)
                logger.warning(
                    "bootstrap_sweep flood_wait=%ds processed_so_far=%d — sleeping",
                    wait_s,
                    count,
                )
                self._set_detail(f"bootstrap sweep: flood_wait {wait_s}s (processed {count})")
                await sleep_through_flood(self._shutdown_event, wait_s)
                # Return without writing 'complete'. iter_dialogs() is an async
                # generator and is not restartable mid-stream after FloodWait — the
                # next daemon start re-enters this method, picks up the cursor,
                # and calls iter_dialogs() afresh with the saved offset triple.
                return count
            except RPCError as exc:
                logger.warning(
                    "bootstrap_sweep rpc_error=%s processed_so_far=%d — aborting sweep",
                    exc,
                    count,
                )
                # Surface to operator via healthcheck startup_detail (review MEDIUM).
                # The daemon will retry on every restart while bootstrap_sweep_status
                # remains 'in_progress'; the operator must see the stall via
                # /health rather than scanning logs.
                self._set_detail("bootstrap sweep stalled (RPCError)")
                return count

            # Loop drained naturally — sweep is complete.
            with self._conn:
                _set_state(self._conn, _KEY_STATUS, _STATUS_COMPLETE)

            self._set_detail(f"bootstrap sweep: complete ({count} dialogs)")
            logger.info("bootstrap_sweep complete count=%d", count)
            return count
        finally:
            try:
                self._conn.close()
            except Exception:
                logger.debug("bootstrap_sweep conn close error", exc_info=True)


# ---------------------------------------------------------------------------
# Reconciliation Worker (Phase 43)
# ---------------------------------------------------------------------------


class DialogReconciliationWorker:
    """Hourly light pass + daily full pass to keep `dialogs` snapshot fresh.

    Light pass: refreshes entity-derived fields for rows with needs_refresh=1.
    Full pass:  iter_dialogs() sweep + soft-deletes dialogs no longer returned.

    Connection ownership (deliberate divergence from DialogsBootstrapWorker):
      DialogsBootstrapWorker opens a DEDICATED sqlite3 connection (its own
      db_path arg) because the bootstrap sweep holds the connection across
      a long-lived async generator. DialogReconciliationWorker takes the
      daemon's MAIN `conn` directly because:
        (1) Each UPSERT in run_full_pass uses its own `with self._conn:`
            block — no transaction spans an await.
        (2) The RECON-04 helper `_set_access_lost` already operates on the
            same main `conn` from sync_worker.py and delta_sync.py — keeping
            reconciliation on that connection avoids cross-connection
            coordination for the atomic synced_dialogs+dialogs transition.
        (3) The light pass writes are short-lived and low-volume (a few
            hundred rows at most per hourly cycle).
      See 43-RESEARCH.md "Connection Ownership" and 43-REVIEWS.md
      "Connection ownership note".

    FloodWait semantics (RECON-05):
      - Light pass: sleep, then advance to next dialog. Does NOT retry the
        same dialog. The needs_refresh=1 flag remains set on the dialog that
        triggered the FloodWait, so the NEXT hourly cycle picks it up.
      - Full pass: sleep, then return. Does NOT resume the iter_dialogs
        stream (Telethon's iter_dialogs is a generator and cannot be resumed
        mid-stream). The next daily cycle re-runs the full pass from
        scratch. last_full_pass is NOT updated when the pass is interrupted
        this way — see run_reconciliation_loop's success-only update logic.
    """

    def __init__(
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._client = cast(_DialogSyncClient, client)
        self._conn = conn
        self._shutdown_event = shutdown_event

    async def run_light_pass(self) -> int:
        """RECON-02: refresh dialogs flagged with needs_refresh=1.

        Returns count of dialogs successfully refreshed.

        FloodWait behavior: on FloodWaitError we sleep (interruptible by
        shutdown_event), then ADVANCE TO THE NEXT DIALOG. We do NOT retry
        the same dialog — its needs_refresh=1 flag remains set, so the next
        hourly cycle picks it up. Returning early on shutdown preserves the
        partial count.

        Telethon session-cache dependency: client.get_entity(dialog_id)
        requires Telethon's session to have access_hash cached for channels
        and supergroups. After a session reset, channels lose this cache
        until iter_dialogs() repopulates it (typically via the daily full
        pass or the bootstrap sweep). When this happens, get_entity raises
        PeerIdInvalidError — we log distinctly so the issue is observable
        and leave needs_refresh=1 for retry once the cache is warm again.
        """
        rows = cast(list[tuple[int]], self._conn.execute(_SELECT_DIRTY_DIALOGS_SQL).fetchall())
        count = 0
        for (dialog_id,) in rows:
            if self._shutdown_event.is_set():
                logger.info(
                    "recon_light_pass_complete count=%d (shutdown)",
                    count,
                )
                return count
            try:
                entity = await self._client.get_entity(dialog_id)
                fields = _extract_entity_fields(entity)
                snapshot_at = int(time.time())
                with self._conn:
                    self._conn.execute(
                        _UPDATE_DIALOG_ENTITY_SQL,
                        (
                            fields["name"],
                            fields["type"],
                            fields["members"],
                            fields["created"],
                            snapshot_at,
                            dialog_id,
                        ),
                    )
                count += 1
                if entity.forum:
                    topic_count = await self._refresh_forum_topics(dialog_id, entity)
                    logger.debug(
                        "recon_light_pass_forum_topics dialog_id=%d count=%d",
                        dialog_id,
                        topic_count,
                    )
            except FloodWaitError as exc:
                wait_s = flood_seconds(exc)
                logger.warning(
                    "recon_light_flood_wait dialog_id=%d wait=%ds",
                    dialog_id,
                    wait_s,
                )
                if await sleep_through_flood(self._shutdown_event, wait_s):
                    logger.info(
                        "recon_light_pass_complete count=%d (shutdown_during_flood_wait)",
                        count,
                    )
                    return count  # shutdown during flood wait
                # Slept full duration; advance to NEXT dialog (per FloodWait
                # semantics in class docstring). Do NOT retry the same dialog —
                # its needs_refresh=1 will be picked up by the next hourly cycle.
            except _ACCESS_LOST_ERRORS as exc:
                logger.warning(
                    "recon_light_access_lost dialog_id=%d — %s",
                    dialog_id,
                    type(exc).__name__,
                )
                _set_access_lost(self._conn, dialog_id, int(time.time()))
                # do not increment count — refresh did not succeed
            except PeerIdInvalidError:
                # Telethon session does not have access_hash cached for this
                # peer (typical for channels/supergroups after a session
                # reset). Leave needs_refresh=1 — the next iter_dialogs
                # sweep (full pass or bootstrap) will repopulate the cache.
                logger.warning(
                    "recon_light_pass_peer_invalid dialog_id=%s (session cache miss; will retry next cycle)",
                    dialog_id,
                )
            except RPCError as exc:
                logger.warning(
                    "recon_light_rpc_error dialog_id=%d error=%s",
                    dialog_id,
                    exc,
                )
                # leave needs_refresh=1 for next cycle
        logger.info("recon_light_pass_complete count=%d", count)
        return count

    async def run_full_pass(self) -> tuple[int, bool]:
        """RECON-03: full iter_dialogs() sweep with soft-delete of missing rows.

        Returns (count, completed) where count is the number of dialogs UPSERTed
        and completed is True only when the sweep finished normally (soft-delete
        phase ran). Dialogs visible before the sweep but not returned by
        iter_dialogs() get hidden=1 when completed=True.

        FloodWait behavior: iter_dialogs is a generator — it cannot be
        resumed mid-stream. On FloodWaitError we sleep (interruptible by
        shutdown_event) and return (count, False). Soft-deletes are NOT
        applied (we cannot tell which dialogs are truly missing vs simply
        not yet streamed). The caller (run_reconciliation_loop) only advances
        last_full_pass when completed=True, so the next hourly tick retries
        the full pass instead of waiting a full day.
        """
        pre_pass_ids = {row[0] for row in cast(list[tuple[int]], self._conn.execute(_SELECT_VISIBLE_DIALOG_IDS_SQL).fetchall())}
        seen_ids: set[int] = set()
        count = 0
        try:
            async for dialog in self._client.iter_dialogs():
                if self._shutdown_event.is_set():
                    return count, False
                snapshot_at = int(time.time())  # fresh per dialog — avoids stale recency guard
                row = _extract_dialog_row(dialog, snapshot_at)
                with self._conn:
                    self._conn.execute(_UPSERT_DIALOG_SQL, row)
                seen_ids.add(int(dialog.id))
                count += 1
                if dialog.entity.forum:
                    topic_count = await self._refresh_forum_topics(int(dialog.id), dialog.entity)
                    logger.debug(
                        "recon_full_pass_forum_topics dialog_id=%d count=%d",
                        int(dialog.id),
                        topic_count,
                    )
        except FloodWaitError as exc:
            wait_s = flood_seconds(exc)
            logger.warning(
                "recon_full_flood_wait wait=%ds processed=%d",
                wait_s,
                count,
            )
            await sleep_through_flood(self._shutdown_event, wait_s)
            return count, False  # cannot resume mid-stream; next cycle retries

        # Soft-delete dialogs visible pre-pass but not returned by iter_dialogs.
        now = int(time.time())
        missing = pre_pass_ids - seen_ids
        for dialog_id in missing:
            with self._conn:
                self._conn.execute(_HIDE_DIALOG_SQL, (now, dialog_id))
        logger.info(
            "recon_full_pass_complete count=%d hidden=%d",
            count,
            len(missing),
        )
        return count, True

    async def _refresh_forum_topics(
        self,
        dialog_id: int,
        entity: _EntityLike,
    ) -> int:
        """Fetch topics for a forum supergroup and upsert into topic_metadata.

        Called from run_light_pass after entity is already fetched. Handles
        FloodWaitError by sleeping (interruptible by shutdown_event) and returning 0.
        Non-forum entities must not be passed — callers must guard with
        getattr(entity, 'forum', False).

        Returns count of topics written.
        """
        try:
            result = await self._client(
                GetForumTopicsRequest(
                    peer=cast(TypeInputPeer, entity),
                    offset_date=None,
                    offset_id=0,
                    offset_topic=0,
                    limit=100,
                )
            )
        except FloodWaitError as exc:
            wait_s = flood_seconds(exc)
            logger.warning(
                "recon_forum_topics_flood_wait dialog_id=%d wait=%ds",
                dialog_id,
                wait_s,
            )
            await sleep_through_flood(self._shutdown_event, wait_s)
            return 0
        except (RPCError, TypeError) as exc:
            logger.warning(
                "recon_forum_topics_fetch_failed dialog_id=%d error=%s",
                dialog_id,
                exc,
            )
            return 0

        topics = result.topics or []
        # NOTE: hard cap of 100 topics per GetForumTopicsRequest (Telegram limit).
        # Forums with >100 topics will silently drop topics beyond the first 100.
        # This matches the pre-existing _list_topics limit=100 behaviour.
        now = int(time.time())
        rows = [
            {
                "dialog_id": dialog_id,
                "topic_id": int(t.id),
                "title": t.title or "",
                "is_general": int(t.is_general or (int(t.id) == 1)),
                "icon_emoji_id": t.icon_emoji_id,
                "updated_at": now,
                "snapshot_at": now,
                "date": int(t.date.timestamp()) if t.date is not None else None,
            }
            for t in topics
        ]
        # Batch all upserts in a single transaction for atomicity and performance.
        with self._conn:
            for row in rows:
                self._conn.execute(_UPSERT_TOPIC_FROM_RECON_SQL, row)
        count = len(rows)
        logger.info("recon_forum_topics_complete dialog_id=%d count=%d", dialog_id, count)
        return count


async def run_reconciliation_loop(
    client: object,
    conn: sqlite3.Connection,
    shutdown_event: asyncio.Event,
    *,
    hourly_interval: float = 3600.0,
    daily_interval: float = 86400.0,
) -> None:
    """Background loop: light pass every hourly_interval, full pass every daily_interval.

    First iteration always runs a full pass (last_full_pass starts at None).
    Shutdown-responsive: returns from inside asyncio.wait_for as soon as
    shutdown_event fires.

    last_full_pass is updated ONLY when run_full_pass() returns without
    raising. If the per-pass try/except catches an exception, last_full_pass
    stays at its prior value — the next hourly tick will retry the full
    pass instead of waiting a full day. This addresses 43-REVIEWS.md
    "Update last_full_pass only on success" (Codex MEDIUM).

    UAT support: Plan 03's daemon caller may pass a smaller hourly_interval
    sourced from RECON_HOURLY_SECONDS env var so an operator can observe a
    needs_refresh=1 -> 0 transition without waiting an hour.
    """
    # None (not 0.0) forces the full pass on the first iteration: time.monotonic()
    # is seconds-since-boot, so on a freshly-booted host monotonic() < daily_interval
    # and `now - 0.0 >= daily_interval` would be False — the daily pass would never
    # run until the host had been up for a full day.
    last_full_pass: float | None = None
    while not shutdown_event.is_set():
        now = time.monotonic()
        worker = DialogReconciliationWorker(client, conn, shutdown_event)
        try:
            await worker.run_light_pass()
        except Exception:
            logger.warning("recon_light_pass_error", exc_info=True)
        if last_full_pass is None or now - last_full_pass >= daily_interval:
            try:
                _count, completed = await worker.run_full_pass()
                # Advance last_full_pass only when the sweep completed
                # normally (soft-delete phase ran). FloodWait or shutdown
                # mid-stream returns completed=False, leaving last_full_pass
                # unchanged so the next hourly tick retries the full pass.
                if completed:
                    last_full_pass = time.monotonic()
            except Exception:
                logger.warning("recon_full_pass_error", exc_info=True)
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=hourly_interval)
            return  # shutdown
        except TimeoutError:
            pass
