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

BOOTSTRAP requirements coverage
-------------------------------
- BOOTSTRAP-01: iter_dialogs() sweep populates `dialogs`.
- BOOTSTRAP-03: TelegramRpcThrottled → interruptible sleep, no crash (D-13).
- BOOTSTRAP-04: cursor checkpoint enables mid-sweep resume.
- BOOTSTRAP-06: INSERT ... ON CONFLICT ... DO UPDATE ... uses a NULL-aware
  body snapshot guard and per-fact unread observation guards, so bootstrap
  never clobbers fresher event-handler writes (D-12).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sqlite3
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Protocol, TypeVar, cast

from telethon.errors import PeerIdInvalidError, RPCError  # type: ignore[import-untyped]
from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.types import (  # type: ignore[import-untyped]
    InputPeerChannel,
    InputPeerChat,
    InputPeerUser,
)

from .access_lifecycle import set_access_lost
from .dialog_classification import EntityKind, classify_dialog_type
from .flood import TelegramRpcThrottled, sleep_through_flood
from .maintenance_logging import log_maintenance_cycle
from .read_state import apply_read_cursor
from .sync_db import _open_sync_db
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    UnclassifiedTelegramDemandError,
    acquisition_context,
    current_demand_token,
    demand_context,
)
from .telegram_rpc_consumers import DemandKind, demand_freshness_seconds
from .telegram_rpc_scheduler import (
    RpcAdmissionExpiredError,
    RpcAdmissionSaturatedError,
    TelegramRpcAdmissionDeferred,
    TelegramRpcSource,
    current_rpc_scope,
    rpc_attempt_budget,
    rpc_scope,
)
from .topics.contracts import TopicSourceUnavailableError, is_topic_capable
from .topics.refresh import TopicRefresher

logger = logging.getLogger(__name__)
T = TypeVar("T")
_LAST_FULL_RECONCILIATION_KEY = "dialog_reconciliation_last_full_at"


def _read_last_full_reconciliation_at(conn: sqlite3.Connection) -> float | None:
    row = cast(
        tuple[object] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key=?", (_LAST_FULL_RECONCILIATION_KEY,)).fetchone(),
    )
    if row is None:
        return None
    try:
        value = float(cast(str | bytes | int | float, row[0]))
    except TypeError, ValueError:
        logger.warning("invalid persisted dialog reconciliation timestamp")
        return None
    if not math.isfinite(value) or value < 0:
        logger.warning("invalid persisted dialog reconciliation timestamp")
        return None
    return value


@contextmanager
def _dialog_demand_scope(kind: DemandKind, acquisition_kind: AcquisitionKind) -> Iterator[None]:
    """Install a precise root for direct calls while preserving an adapter root."""
    try:
        current_demand_token()
    except UnclassifiedTelegramDemandError:
        with demand_context(kind):
            with acquisition_context(acquisition_kind):
                yield
    else:
        with acquisition_context(acquisition_kind):
            yield


def _dialog_sync_rpc_scope[**P, R](
    kind: DemandKind,
    acquisition_kind: AcquisitionKind,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Give dialog snapshots and reconciliation precise demand identity."""

    def decorate(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(func)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            with _dialog_demand_scope(kind, acquisition_kind):
                with rpc_scope(TelegramRpcSource.DIALOG_SYNC):
                    return await func(*args, **kwargs)

        return wrapped

    return decorate


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


class _DraftLike(Protocol):
    message: str | None


def _extract_draft_text(draft: object) -> str | None:
    if draft is None:
        return None

    for field in ("message", "text", "message_text"):
        value = getattr(draft, field, None)
        if isinstance(value, str):
            return value[:80] if value else None
    return None


class _MessageLike(Protocol):
    id: int
    date: datetime | None


class _ReadCursorDialogLike(Protocol):
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None


class _DialogLike(Protocol):
    id: int
    dialog: _ReadCursorDialogLike | None
    entity: _EntityLike
    message: _MessageLike | None
    pinned: bool
    folder_id: int | None
    read_inbox_max_id: int | None
    read_outbox_max_id: int | None
    unread_mentions_count: int | None
    unread_reactions_count: int | None
    unread_count: int | None
    unread_mark: bool | None
    draft: _DraftLike | None
    date: datetime | None


class _ForumTopicLike(Protocol):
    id: int
    title: str | None
    icon_emoji_id: int | None
    date: datetime | None


class _ForumTopicsResultLike(Protocol):
    topics: list[_ForumTopicLike]


_BootstrapRow = dict[str, object]
_EntityFields = dict[str, object]


@dataclass(frozen=True, slots=True)
class _BootstrapAttemptResult:
    count: int
    continue_sweep: bool = False
    completed: bool = False


@dataclass(slots=True)
class _BootstrapAttempt:
    count: int


class _DialogSyncClient(Protocol):
    def iter_dialogs(self, **_kwargs: object) -> AsyncIterator[_DialogLike]: ...

    async def get_entity(self, _peer: object) -> _EntityLike: ...

    async def __call__(self, _request: object) -> _ForumTopicsResultLike: ...


def _attr[T](obj: object, name: str, default: T) -> T:
    return cast(T, getattr(obj, name, default))


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_GET_STATE_SQL = "SELECT value FROM daemon_state WHERE key = ?"
_SET_STATE_SQL = "INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)"
_DELETE_STATE_SQL = "DELETE FROM daemon_state WHERE key = ?"

# D-12: bootstrap/reconciliation body fields only overwrite rows whose
# snapshot is older (or unknown), while unread facts use their own observed
# timestamps. This allows a raw event observed at t102 to survive a Dialog
# snapshot captured at t101 that commits later. `hidden` and `needs_refresh`
# remain deliberately excluded from the update clause.
_UPSERT_DIALOG_SQL = """
INSERT INTO dialogs (
    dialog_id, name, type, archived, pinned, members, created,
    last_message_at, snapshot_at, hidden, needs_refresh,
    unread_mentions_count, unread_reactions_count, draft_text,
    unread_count, unread_mark, unread_count_observed_at, unread_mark_observed_at
) VALUES (
    :dialog_id, :name, :type, :archived, :pinned, :members, :created,
    :last_message_at, :snapshot_at, 0, 0,
    :unread_mentions_count, :unread_reactions_count, :draft_text,
    :unread_count, :unread_mark, :unread_count_observed_at, :unread_mark_observed_at
)
ON CONFLICT(dialog_id) DO UPDATE SET
    name = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                THEN excluded.name ELSE dialogs.name END,
    type = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                THEN excluded.type ELSE dialogs.type END,
    archived = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                    THEN excluded.archived ELSE dialogs.archived END,
    pinned = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                  THEN excluded.pinned ELSE dialogs.pinned END,
    members = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                   THEN excluded.members ELSE dialogs.members END,
    created = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                   THEN excluded.created ELSE dialogs.created END,
    last_message_at = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                           THEN excluded.last_message_at ELSE dialogs.last_message_at END,
    snapshot_at = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                       THEN excluded.snapshot_at ELSE dialogs.snapshot_at END,
    unread_mentions_count = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                                 THEN excluded.unread_mentions_count ELSE dialogs.unread_mentions_count END,
    unread_reactions_count = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                                  THEN excluded.unread_reactions_count ELSE dialogs.unread_reactions_count END,
    draft_text = CASE WHEN dialogs.snapshot_at IS NULL OR dialogs.snapshot_at < excluded.snapshot_at
                      THEN excluded.draft_text ELSE dialogs.draft_text END,
    unread_count = CASE
        WHEN excluded.unread_count IS NOT NULL
         AND excluded.unread_count_observed_at IS NOT NULL
         AND (dialogs.unread_count_observed_at IS NULL
              OR excluded.unread_count_observed_at > dialogs.unread_count_observed_at)
        THEN excluded.unread_count ELSE dialogs.unread_count END,
    unread_count_observed_at = CASE
        WHEN excluded.unread_count IS NOT NULL
         AND excluded.unread_count_observed_at IS NOT NULL
         AND (dialogs.unread_count_observed_at IS NULL
              OR excluded.unread_count_observed_at > dialogs.unread_count_observed_at)
        THEN excluded.unread_count_observed_at ELSE dialogs.unread_count_observed_at END,
    unread_mark = CASE
        WHEN excluded.unread_mark IS NOT NULL
         AND excluded.unread_mark_observed_at IS NOT NULL
         AND (dialogs.unread_mark_observed_at IS NULL
              OR excluded.unread_mark_observed_at > dialogs.unread_mark_observed_at)
        THEN excluded.unread_mark ELSE dialogs.unread_mark END,
    unread_mark_observed_at = CASE
        WHEN excluded.unread_mark IS NOT NULL
         AND excluded.unread_mark_observed_at IS NOT NULL
         AND (dialogs.unread_mark_observed_at IS NULL
              OR excluded.unread_mark_observed_at > dialogs.unread_mark_observed_at)
        THEN excluded.unread_mark_observed_at ELSE dialogs.unread_mark_observed_at END
"""

# daemon_state keys (D-02, D-03)
_KEY_STATUS = "bootstrap_sweep_status"
_KEY_OFFSET_DATE = "bootstrap_sweep_offset_date"
_KEY_OFFSET_ID = "bootstrap_sweep_offset_id"
_KEY_OFFSET_PEER = "bootstrap_sweep_offset_peer"
_CURSOR_KEYS = (_KEY_OFFSET_DATE, _KEY_OFFSET_ID, _KEY_OFFSET_PEER)

_STATUS_IN_PROGRESS = "in_progress"
_STATUS_COMPLETE = "complete"

# Honest state for the latest full dialog enumeration.  The count is the
# number of Dialog wrappers observed, not a percentage or message estimate.
_KEY_UNREAD_SWEEP_ATTEMPTED_AT = "dialog_unread_sweep_attempted_at"
_KEY_UNREAD_SWEEP_COMPLETED_AT = "dialog_unread_sweep_completed_at"
_KEY_UNREAD_SWEEP_STATUS = "dialog_unread_sweep_status"
_KEY_UNREAD_SWEEP_OBSERVED_COUNT = "dialog_unread_sweep_observed_count"
_KEY_UNREAD_SWEEP_LAST_VISIBLE_COUNT = "dialog_unread_sweep_last_visible_count"

# Progress-reporting cadence (every Nth dialog updates startup_detail)
_PROGRESS_REPORT_EVERY = 50

# ---------------------------------------------------------------------------
# Access-loss handling (RECON-04). Telegram error classification lives in
# telegram_access; local atomic status transition lives in sync_db.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reconciliation SQL (Phase 43)
# ---------------------------------------------------------------------------

_SELECT_DIRTY_DIALOGS_SQL = "SELECT dialog_id FROM dialogs WHERE needs_refresh = 1 AND hidden = 0"
_SELECT_DIRTY_DIALOG_EXISTS_SQL = "SELECT 1 FROM dialogs WHERE needs_refresh = 1 AND hidden = 0 LIMIT 1"

_UPDATE_DIALOG_ENTITY_SQL = (
    "UPDATE dialogs SET name=?, type=?, members=?, created=?, needs_refresh=0, snapshot_at=? WHERE dialog_id=?"
)
_HIDE_DIALOG_SQL = "UPDATE dialogs SET hidden=1, snapshot_at=? WHERE dialog_id=? AND hidden=0"
_SELECT_VISIBLE_DIALOG_IDS_SQL = "SELECT dialog_id FROM dialogs WHERE hidden = 0"

_SELECT_FULL_RECONCILIATION_STATE_SQL = """
SELECT generation, status, offset_date, offset_id, offset_peer, started_at, observed_count
FROM dialog_full_reconciliation_state
WHERE singleton = 1
"""

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


def _begin_unread_sweep(conn: sqlite3.Connection, *, visible_count: int) -> None:
    """Record an honest partial attempt before opening iter_dialogs()."""
    prior = _get_state(conn, _KEY_UNREAD_SWEEP_LAST_VISIBLE_COUNT)
    last_visible = int(prior) if prior is not None and prior.isdecimal() else visible_count
    now = int(time.time())
    _set_state(conn, _KEY_UNREAD_SWEEP_ATTEMPTED_AT, str(now))
    _set_state(conn, _KEY_UNREAD_SWEEP_STATUS, "partial")
    _set_state(conn, _KEY_UNREAD_SWEEP_OBSERVED_COUNT, "0")
    _set_state(conn, _KEY_UNREAD_SWEEP_LAST_VISIBLE_COUNT, str(last_visible))


def _finish_unread_sweep(
    conn: sqlite3.Connection,
    *,
    status: str,
    observed_count: int,
    completed: bool,
    visible_count: int | None = None,
) -> None:
    if status not in {"complete", "partial", "source_unavailable"}:
        raise ValueError(f"invalid unread sweep status: {status!r}")
    _set_state(conn, _KEY_UNREAD_SWEEP_STATUS, status)
    _set_state(conn, _KEY_UNREAD_SWEEP_OBSERVED_COUNT, str(observed_count))
    if completed:
        _set_state(conn, _KEY_UNREAD_SWEEP_COMPLETED_AT, str(int(time.time())))
        if visible_count is not None:
            _set_state(conn, _KEY_UNREAD_SWEEP_LAST_VISIBLE_COUNT, str(visible_count))


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
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.USER).value
        members = None
        created = None
    elif isinstance(entity, types.Chat):
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.CHAT).value
        members = entity.participants_count
        created = None
    elif isinstance(entity, types.Channel):
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.CHANNEL).value
        members = entity.participants_count
        date = entity.date
        created = int(date.timestamp()) if date else None
    else:
        dialog_type = classify_dialog_type(entity, entity_kind=EntityKind.UNKNOWN).value
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


def _extract_unread_facts(dialog: _DialogLike, snapshot_at: int) -> dict[str, object]:
    """Extract Telegram unread facts and their snapshot observation times."""
    unread_count_raw = getattr(dialog, "unread_count", None)
    unread_count = int(unread_count_raw) if isinstance(unread_count_raw, int) else None
    raw_dialog: object | None = getattr(dialog, "dialog", None)
    unread_mark_raw = getattr(raw_dialog, "unread_mark", None) if raw_dialog is not None else None
    if not isinstance(unread_mark_raw, bool):
        direct_mark = getattr(dialog, "unread_mark", None)
        unread_mark_raw = direct_mark if isinstance(direct_mark, bool) else None
    return {
        "unread_count": unread_count,
        "unread_mark": unread_mark_raw,
        "unread_count_observed_at": snapshot_at if unread_count is not None else None,
        "unread_mark_observed_at": snapshot_at if unread_mark_raw is not None else None,
    }


def _extract_dialog_row(dialog: _DialogLike, snapshot_at: int) -> _BootstrapRow:
    """Build the dict bound to _UPSERT_DIALOG_SQL for one Dialog object.

    All values come from the Dialog object and dialog.entity directly — no
    extra RPCs:
    - D-08: members/created from dialog.entity (Channel/Chat); NULL for User.
    - D-09: unread_mentions/reactions from dialog directly.
    - D-10: draft_text = normalized dialog.draft text (DIFF-03 truncation).
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

    # Telegram's unread_count is authoritative and nullable.  ``iter_dialogs``
    # returns a custom wrapper with this field directly; raw TL Dialogs expose
    # unread_mark only on the nested ``dialog`` object.
    unread_facts = _extract_unread_facts(dialog, snapshot_at)

    # D-10: draft_text = best available draft text truncated to 80 chars.
    draft_text = _extract_draft_text(dialog.draft)  # DIFF-03

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
        **unread_facts,
        "draft_text": draft_text,
    }


def _dialog_read_cursor(dialog: _DialogLike, field: str) -> int | None:
    """Read a cursor from either a raw TL Dialog or Telethon custom Dialog.

    ``GetPeerDialogsRequest`` returns TL ``Dialog`` objects with direct
    ``read_*`` fields. ``iter_dialogs()`` returns custom Dialog wrappers where
    the same TL object is exposed as ``.dialog``. Reconciliation consumes
    ``iter_dialogs()``, while startup read-position code may use raw TL
    dialogs, so this helper keeps that shape knowledge in one place.
    """
    direct: object = getattr(dialog, field, None)
    if direct is not None:
        return cast(int, direct)
    wrapped: object | None = getattr(dialog, "dialog", None)
    if wrapped is None:
        return None
    value: object = getattr(wrapped, field, None)
    return cast(int | None, value)


def _apply_dialog_read_cursors(conn: sqlite3.Connection, dialog: _DialogLike) -> bool:
    """Refresh local DM read cursors from an already-fetched Telegram Dialog.

    ``iter_dialogs()`` returns Telegram's current ``read_inbox_max_id`` and
    ``read_outbox_max_id`` on each Dialog.  Reconciliation already consumes
    that stream to maintain the local dialog snapshot, so applying the cursors
    here adds no Telegram RPCs and therefore does not change throttling risk.

    ``None`` means Telegram did not provide a cursor for that side; preserve
    the existing DB value rather than inventing precision.  Non-``None`` values
    are written through :func:`apply_read_cursor`, which keeps the existing
    monotonic invariant and never regresses a cursor.

    Returns True when at least one side wrote to an existing ``synced_dialogs``
    row.  The caller owns the transaction boundary.
    """
    dialog_id = int(dialog.id)
    wrote_any = False
    inbox_max = _dialog_read_cursor(dialog, "read_inbox_max_id")
    outbox_max = _dialog_read_cursor(dialog, "read_outbox_max_id")
    if inbox_max is not None and apply_read_cursor(conn, dialog_id, "inbox", inbox_max) > 0:
        wrote_any = True
    if outbox_max is not None and apply_read_cursor(conn, dialog_id, "outbox", outbox_max) > 0:
        wrote_any = True
    return wrote_any


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


class DialogsBootstrapWorker:
    """Single-pass iter_dialogs() sweep that populates the `dialogs` table.

    Resumable via a cursor checkpoint in `daemon_state`. Idempotent — once the
    completion flag is written, subsequent runs short-circuit without calling
    iter_dialogs(). TelegramRpcThrottled causes an interruptible sleep; the daemon's
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
        self._last_retry_error: BaseException | None = None

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

    async def _consume_bootstrap_attempt(self, attempt: _BootstrapAttempt) -> bool:
        """Consume one iterator pass and report whether it drained normally."""
        offset_date, offset_id, offset_peer = self._reconstruct_cursor()
        async for dialog in self._client.iter_dialogs(
            offset_date=offset_date,
            offset_id=offset_id,
            offset_peer=offset_peer if offset_peer is not None else types.InputPeerEmpty(),
        ):
            if self._shutdown_event.is_set():
                logger.info("bootstrap_sweep shutdown signal received — exiting (count=%d)", attempt.count)
                return False

            snapshot_at = int(time.time())
            self._checkpoint_dialog(dialog, _extract_dialog_row(dialog, snapshot_at))
            attempt.count += 1
            if attempt.count % _PROGRESS_REPORT_EVERY == 0:
                self._set_detail(f"bootstrap sweep: {attempt.count} dialogs processed")
        return True

    async def _handle_bootstrap_throttling(self, exc: TelegramRpcThrottled, count: int) -> int:
        if exc.retry_after_seconds is None or current_rpc_scope().attempt_budget is not None:
            return count
        wait_s = exc.retry_after_seconds
        logger.warning(
            "bootstrap_sweep flood_wait=%ds processed_so_far=%d — sleeping",
            wait_s,
            count,
        )
        self._set_detail(f"bootstrap sweep: flood_wait {wait_s}s (processed {count})")
        await sleep_through_flood(self._shutdown_event, wait_s)
        return count

    async def _handle_bootstrap_admission_deferred(
        self, exc: TelegramRpcAdmissionDeferred, count: int
    ) -> _BootstrapAttemptResult:
        wait_s = exc.retry_after_seconds or 1
        logger.info(
            "bootstrap_sweep admission_deferred retry_after=%s processed_so_far=%d — preserving cursor",
            wait_s,
            count,
        )
        self._set_detail(f"bootstrap sweep: admission deferred {wait_s}s (processed {count})")
        if current_rpc_scope().attempt_budget is not None:
            return _BootstrapAttemptResult(count)
        return _BootstrapAttemptResult(
            count,
            continue_sweep=not await sleep_through_flood(self._shutdown_event, wait_s),
        )

    def _checkpoint_dialog(self, dialog: _DialogLike, row: _BootstrapRow) -> None:
        """Persist one dialog and its resume cursor atomically."""
        with self._conn:
            self._conn.execute(_UPSERT_DIALOG_SQL, row)
            _apply_dialog_read_cursors(self._conn, dialog)
            dialog_date = dialog.date
            _set_state(
                self._conn,
                _KEY_OFFSET_DATE,
                dialog_date.isoformat() if dialog_date is not None else None,
            )
            _set_state(self._conn, _KEY_OFFSET_ID, str(int(dialog.id)))
            _set_state(self._conn, _KEY_OFFSET_PEER, _encode_offset_peer(dialog.entity))
            _set_state(self._conn, _KEY_STATUS, _STATUS_IN_PROGRESS)

    async def _run_bootstrap_attempt(self, count: int) -> _BootstrapAttemptResult:
        """Run one iterator pass, returning whether local admission should retry."""
        attempt = _BootstrapAttempt(count)
        try:
            completed = await self._consume_bootstrap_attempt(attempt)
        except TelegramRpcAdmissionDeferred as exc:
            self._last_retry_error = exc
            return await self._handle_bootstrap_admission_deferred(exc, attempt.count)
        except TelegramRpcThrottled as exc:
            self._last_retry_error = exc
            await self._handle_bootstrap_throttling(exc, attempt.count)
            return _BootstrapAttemptResult(attempt.count)
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            self._last_retry_error = exc
            logger.info(
                "bootstrap_sweep admission_deferred error_type=%s processed_so_far=%d — preserving cursor",
                type(exc).__name__,
                attempt.count,
            )
            return _BootstrapAttemptResult(attempt.count)
        except RPCError as exc:
            self._last_retry_error = exc
            logger.warning(
                "bootstrap_sweep rpc_error=%s processed_so_far=%d — aborting sweep",
                exc,
                attempt.count,
            )
            self._set_detail("bootstrap sweep stalled (RPCError)")
            return _BootstrapAttemptResult(attempt.count)
        return _BootstrapAttemptResult(attempt.count, completed=completed)

    @_dialog_sync_rpc_scope(DemandKind.DIALOG_BOOTSTRAP, AcquisitionKind.DIALOG_TRAVERSAL)
    async def run(self) -> int:
        """Run (or skip) the bootstrap sweep. Returns count of dialogs processed.

        Returns 0 if the sweep is already complete or if it exits early on
        TelegramRpcThrottled/RPCError/shutdown. A local admission deferral is
        retried in-process after a bounded, shutdown-aware wait; the durable
        cursor is re-read before each fresh iterator. Caller does not need to
        inspect the return value — daemon_state holds the persistent state.

        The dedicated connection is closed in the finally block.
        """
        try:
            status = _get_state(self._conn, _KEY_STATUS)
            if status == _STATUS_COMPLETE:
                logger.info("bootstrap_sweep already complete — skipping")
                return 0

            # Mark in_progress at the very start so a kill before the first dialog
            # still leaves a recognisable resume signal.
            with self._conn:
                _set_state(self._conn, _KEY_STATUS, _STATUS_IN_PROGRESS)

            count = 0
            while True:
                result = await self._run_bootstrap_attempt(count)
                count = result.count
                if result.continue_sweep:
                    self._last_retry_error = None
                    continue
                if not result.completed:
                    return count
                break

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


class DialogBootstrapDemandAdapter:
    """Resumable bootstrap adapter over the daemon-state cursor."""

    demand_kind = DemandKind.DIALOG_BOOTSTRAP

    def __init__(
        self,
        client: object,
        conn: sqlite3.Connection,
        db_path: Path,
        shutdown_event: asyncio.Event,
        *,
        startup_detail_setter: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._conn = conn
        self._db_path = db_path
        self._shutdown_event = shutdown_event
        self._startup_detail_setter = startup_detail_setter

    def status(self, now: float) -> DemandStatus | None:
        """Read bootstrap completion without opening an iterator or writing."""
        del now
        if _get_state(self._conn, _KEY_STATUS) == _STATUS_COMPLETE:
            return None
        return DemandStatus(release_at=0.0)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Resume one bounded traversal slice from its committed cursor."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.status(time.time()) is None:
            return
        worker = DialogsBootstrapWorker(
            self._client,
            self._db_path,
            self._shutdown_event,
            startup_detail_setter=self._startup_detail_setter,
        )
        with demand_context(DemandKind.DIALOG_BOOTSTRAP):
            with rpc_attempt_budget(budget):
                try:
                    await worker.run()
                    if worker._last_retry_error is not None:
                        raise worker._last_retry_error
                except RpcAttemptBudgetExhaustedError:
                    return


def _full_pass_access_status(count: int) -> str:
    return "source_unavailable" if count == 0 else "partial"


@dataclass(frozen=True, slots=True)
class _FullReconciliationState:
    generation: int
    offset_date: datetime | None
    offset_id: int
    offset_peer: object | None
    observed_count: int


@dataclass(frozen=True, slots=True)
class _FullPassSliceResult:
    completed: bool
    partial_status: str = "partial"


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
        (1) Each UPSERT in the full traversal uses its own `with self._conn:`
            block — no transaction spans an await.
        (2) The access_lifecycle operation already operates on the same main
        `conn` from sync_worker.py and delta_sync.py — keeping
            reconciliation on that connection avoids cross-connection
            coordination for the atomic synced_dialogs+dialogs transition.
        (3) The light pass writes are short-lived and low-volume (a few
            hundred rows at most per hourly cycle).
      See 43-RESEARCH.md "Connection Ownership" and 43-REVIEWS.md
      "Connection ownership note".

    FloodWait semantics (RECON-05):
      - Light pass: sleep, then advance to next dialog. Does NOT retry the
        same dialog. The needs_refresh=1 flag remains set on the dialog that
        triggered throttling, so the NEXT hourly cycle picks it up.
      - Full pass: checkpoint each consumed dialog, then sleep and return.
        A later slice or legacy cycle reconstructs the Telegram cursor and
        resumes the same durable generation. last_full_pass advances only
        after the iterator drains and the unchanged unseen baseline is hidden.
    """

    def __init__(
        self,
        client: object,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        topic_refresher: TopicRefresher | None = None,
    ) -> None:
        self._client = cast(_DialogSyncClient, client)
        self._conn = conn
        self._shutdown_event = shutdown_event
        self._topic_refresher = topic_refresher

    async def _handle_light_throttling(self, exc: TelegramRpcThrottled, dialog_id: int) -> bool:
        if exc.retry_after_seconds is None:
            return False
        wait_s = exc.retry_after_seconds
        logger.warning("recon_light_flood_wait dialog_id=%d wait=%ds", dialog_id, wait_s)
        return await sleep_through_flood(self._shutdown_event, wait_s)

    async def _refresh_light_dialog(self, dialog_id: int, *, refresh_topics: bool = True) -> bool | None:
        """Refresh one dirty dialog; None means shutdown interrupted a wait."""
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
            if refresh_topics and self._topic_refresher is not None and is_topic_capable(entity):
                topic_count = await self._refresh_forum_topics(dialog_id, entity)
                logger.debug(
                    "recon_light_pass_forum_topics dialog_id=%d count=%d",
                    dialog_id,
                    topic_count,
                )
            return True
        except TelegramRpcThrottled as exc:
            if await self._handle_light_throttling(exc, dialog_id):
                return None
        except (RpcAdmissionSaturatedError, RpcAdmissionExpiredError) as exc:
            logger.info(
                "recon_light admission_deferred dialog_id=%d error_type=%s — preserving refresh flag",
                dialog_id,
                type(exc).__name__,
            )
        except ACCESS_LOST_ERRORS as exc:
            set_access_lost(self._conn, dialog_id, int(time.time()), reason=type(exc).__name__)
            self._conn.commit()
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
        return False

    @_dialog_sync_rpc_scope(DemandKind.DIALOG_LIGHT_RECONCILIATION, AcquisitionKind.ENTITY_LOOKUP)
    async def run_light_pass(self, *, refresh_topics: bool = True) -> int:
        """RECON-02: refresh dialogs flagged with needs_refresh=1.

        Returns count of dialogs successfully refreshed.

        Throttling behavior: on TelegramRpcThrottled we sleep (interruptible by
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
            refreshed = await self._refresh_light_dialog(dialog_id, refresh_topics=refresh_topics)
            if refreshed is None:
                return count
            count += int(refreshed)
        log_maintenance_cycle(logger, count > 0, "recon_light_pass_complete count=%d", count)
        return count

    def _mark_full_pass_partial(self, count: int, *, status: str = "partial") -> None:
        with self._conn:
            _finish_unread_sweep(
                self._conn,
                status=status,
                observed_count=count,
                completed=False,
            )

    def _full_observed_count(self, generation: int, *, fallback: int) -> int:
        row = cast(
            tuple[int] | None,
            self._conn.execute(
                "SELECT observed_count FROM dialog_full_reconciliation_state WHERE singleton=1 AND generation=?",
                (generation,),
            ).fetchone(),
        )
        return row[0] if row is not None else fallback

    def _reset_corrupt_full_generation(self, generation: int) -> None:
        with self._conn:
            self._conn.execute(
                "UPDATE dialog_full_reconciliation_state "
                "SET status='idle', offset_date=NULL, offset_id=0, offset_peer=NULL, "
                "started_at=NULL, observed_count=0 WHERE singleton=1 AND generation=?",
                (generation,),
            )
            self._conn.execute(
                "DELETE FROM dialog_full_reconciliation_baseline WHERE generation=?",
                (generation,),
            )

    def _load_or_begin_full_generation(self) -> _FullReconciliationState:
        """Atomically resume a generation or snapshot a new visible baseline."""
        with self._conn:
            row = cast(
                tuple[int, str, str | None, int, str | None, int | None, int] | None,
                self._conn.execute(_SELECT_FULL_RECONCILIATION_STATE_SQL).fetchone(),
            )
            if row is None:
                raise RuntimeError("dialog full reconciliation state row is missing")
            generation, status, offset_date_raw, offset_id, offset_peer_raw, _started_at, observed_count = row
            if status == "idle":
                generation += 1
                started_at = int(time.time())
                self._conn.execute("DELETE FROM dialog_full_reconciliation_baseline")
                self._conn.execute(
                    "INSERT INTO dialog_full_reconciliation_baseline "
                    "(generation, dialog_id, baseline_revision, seen) "
                    "SELECT ?, dialog_id, revision, 0 FROM dialogs WHERE hidden=0",
                    (generation,),
                )
                visible_count = cast(
                    int,
                    self._conn.execute(
                        "SELECT COUNT(*) FROM dialog_full_reconciliation_baseline WHERE generation=?",
                        (generation,),
                    ).fetchone()[0],
                )
                self._conn.execute(
                    "UPDATE dialog_full_reconciliation_state "
                    "SET generation=?, status='in_progress', offset_date=NULL, offset_id=0, "
                    "offset_peer=NULL, started_at=?, observed_count=0 WHERE singleton=1",
                    (generation, started_at),
                )
                _begin_unread_sweep(self._conn, visible_count=visible_count)
                return _FullReconciliationState(generation, None, 0, None, 0)

        try:
            offset_date = datetime.fromisoformat(offset_date_raw) if offset_date_raw else None
            offset_peer = _decode_offset_peer(offset_peer_raw) if offset_peer_raw else None
        except ValueError, TypeError, json.JSONDecodeError:
            logger.warning("recon_full cursor corrupt — starting a new generation", exc_info=True)
            self._reset_corrupt_full_generation(generation)
            return self._load_or_begin_full_generation()
        return _FullReconciliationState(generation, offset_date, offset_id, offset_peer, observed_count)

    def _checkpoint_full_dialog(self, state: _FullReconciliationState, dialog: _DialogLike) -> bool:
        """Apply a dialog, seen membership, and resume cursor in one transaction."""
        snapshot_at = int(time.time())
        with self._conn:
            current = cast(
                tuple[int, str] | None,
                self._conn.execute(
                    "SELECT generation, status FROM dialog_full_reconciliation_state WHERE singleton=1"
                ).fetchone(),
            )
            if current != (state.generation, "in_progress"):
                return False
            self._conn.execute(_UPSERT_DIALOG_SQL, _extract_dialog_row(dialog, snapshot_at))
            _apply_dialog_read_cursors(self._conn, dialog)
            dialog_id = int(dialog.id)
            seen_update = self._conn.execute(
                "UPDATE dialog_full_reconciliation_baseline SET seen=1 WHERE generation=? AND dialog_id=? AND seen=0",
                (state.generation, dialog_id),
            )
            first_observation = seen_update.rowcount == 1
            if not first_observation:
                inserted = self._conn.execute(
                    "INSERT OR IGNORE INTO dialog_full_reconciliation_baseline "
                    "(generation, dialog_id, baseline_revision, seen) "
                    "SELECT ?, dialog_id, revision, 1 FROM dialogs WHERE dialog_id=?",
                    (state.generation, dialog_id),
                )
                first_observation = inserted.rowcount == 1
            dialog_date = dialog.date if isinstance(dialog.date, datetime) else None
            message = dialog.message
            message_id = message.id if message is not None and isinstance(message.id, int) else 0
            self._conn.execute(
                "UPDATE dialog_full_reconciliation_state "
                "SET offset_date=?, offset_id=?, offset_peer=?, observed_count=observed_count+? "
                "WHERE singleton=1 AND generation=? AND status='in_progress'",
                (
                    dialog_date.isoformat() if dialog_date is not None else None,
                    message_id,
                    _encode_offset_peer(dialog.entity),
                    int(first_observation),
                    state.generation,
                ),
            )
        return True

    def _complete_full_generation(self, generation: int) -> tuple[int, int] | None:
        """Hide only unchanged unseen baseline rows after a generation match."""
        now = int(time.time())
        with self._conn:
            row = cast(
                tuple[int, str, int] | None,
                self._conn.execute(
                    "SELECT generation, status, observed_count FROM dialog_full_reconciliation_state WHERE singleton=1"
                ).fetchone(),
            )
            if row is None or row[0] != generation or row[1] != "in_progress":
                return None
            observed_count = row[2]
            cursor = self._conn.execute(
                """
                UPDATE dialogs
                   SET hidden=1, snapshot_at=:now
                 WHERE hidden=0
                   AND EXISTS (
                       SELECT 1
                         FROM dialog_full_reconciliation_baseline AS baseline
                        WHERE baseline.generation=:generation
                          AND baseline.dialog_id=dialogs.dialog_id
                          AND baseline.seen=0
                          AND baseline.baseline_revision=dialogs.revision
                   )
                """,
                {"generation": generation, "now": now},
            )
            hidden = cursor.rowcount
            _finish_unread_sweep(
                self._conn,
                status="complete",
                observed_count=observed_count,
                completed=True,
                visible_count=observed_count,
            )
            _set_state(self._conn, _LAST_FULL_RECONCILIATION_KEY, str(float(now)))
            self._conn.execute(
                "UPDATE dialog_full_reconciliation_state "
                "SET status='idle', offset_date=NULL, offset_id=0, offset_peer=NULL, "
                "started_at=NULL, observed_count=0 WHERE singleton=1 AND generation=?",
                (generation,),
            )
            self._conn.execute(
                "DELETE FROM dialog_full_reconciliation_baseline WHERE generation=?",
                (generation,),
            )
        return observed_count, hidden

    async def _wait_full_pass_throttle(
        self,
        retry_after_seconds: int | None,
        *,
        wait_on_throttle: bool,
    ) -> None:
        if wait_on_throttle and retry_after_seconds is not None:
            await sleep_through_flood(self._shutdown_event, retry_after_seconds)

    async def _handle_full_pass_exception(
        self,
        state: _FullReconciliationState,
        exc: Exception,
        *,
        wait_on_throttle: bool,
    ) -> _FullPassSliceResult:
        if isinstance(exc, RpcAttemptBudgetExhaustedError):
            return _FullPassSliceResult(False)
        if not wait_on_throttle:
            raise exc
        if isinstance(exc, TelegramRpcAdmissionDeferred):
            logger.info(
                "recon_full admission_deferred retry_after=%s generation=%d",
                exc.retry_after_seconds,
                state.generation,
            )
            await self._wait_full_pass_throttle(
                exc.retry_after_seconds,
                wait_on_throttle=wait_on_throttle,
            )
            return _FullPassSliceResult(False)
        if isinstance(exc, (RpcAdmissionSaturatedError, RpcAdmissionExpiredError)):
            logger.info("recon_full admission_deferred error_type=%s", type(exc).__name__)
            return _FullPassSliceResult(False)
        if isinstance(exc, TelegramRpcThrottled):
            logger.warning("recon_full_flood_wait wait=%s", exc.retry_after_seconds)
            await self._wait_full_pass_throttle(
                exc.retry_after_seconds,
                wait_on_throttle=wait_on_throttle,
            )
            return _FullPassSliceResult(False)
        count = self._full_observed_count(state.generation, fallback=state.observed_count)
        partial_status = _full_pass_access_status(count)
        logger.warning("recon_full_%s error=%s", partial_status, type(exc).__name__)
        return _FullPassSliceResult(False, partial_status)

    def _record_full_pass_error(
        self,
        state: _FullReconciliationState,
        exc: Exception,
    ) -> None:
        self._mark_full_pass_partial(
            self._full_observed_count(state.generation, fallback=state.observed_count),
        )
        logger.error(
            "recon_full_pass_unexpected_error generation=%d",
            state.generation,
            exc_info=exc,
        )
        raise exc

    async def _iterate_full_pass(
        self,
        state: _FullReconciliationState,
        *,
        refresh_topics: bool,
    ) -> _FullPassSliceResult:
        async for dialog in self._client.iter_dialogs(
            offset_date=state.offset_date,
            offset_id=state.offset_id,
            offset_peer=state.offset_peer if state.offset_peer is not None else types.InputPeerEmpty(),
        ):
            if self._shutdown_event.is_set() or not self._checkpoint_full_dialog(state, dialog):
                return _FullPassSliceResult(False)
            if refresh_topics and self._topic_refresher is not None and is_topic_capable(dialog.entity):
                await self._refresh_forum_topics(int(dialog.id), dialog.entity)
        return _FullPassSliceResult(True)

    async def _consume_full_pass(
        self,
        state: _FullReconciliationState,
        *,
        refresh_topics: bool,
        wait_on_throttle: bool,
    ) -> _FullPassSliceResult:
        try:
            return await self._iterate_full_pass(
                state,
                refresh_topics=refresh_topics,
            )
        except (
            RpcAttemptBudgetExhaustedError,
            TelegramRpcAdmissionDeferred,
            RpcAdmissionSaturatedError,
            RpcAdmissionExpiredError,
            TelegramRpcThrottled,
        ) as exc:
            return await self._handle_full_pass_exception(
                state,
                exc,
                wait_on_throttle=wait_on_throttle,
            )
        except ACCESS_LOST_ERRORS as exc:
            return await self._handle_full_pass_exception(
                state,
                exc,
                wait_on_throttle=wait_on_throttle,
            )
        except Exception as exc:
            self._record_full_pass_error(state, exc)
            raise

    async def _run_full_pass_slice(self, *, refresh_topics: bool, wait_on_throttle: bool) -> tuple[int, bool]:
        state = self._load_or_begin_full_generation()
        result = await self._consume_full_pass(
            state,
            refresh_topics=refresh_topics,
            wait_on_throttle=wait_on_throttle,
        )
        if not result.completed:
            count = self._full_observed_count(state.generation, fallback=state.observed_count)
            self._mark_full_pass_partial(count, status=result.partial_status)
            return count, False

        completion = self._complete_full_generation(state.generation)
        if completion is None:
            return state.observed_count, False
        count, hidden = completion
        log_maintenance_cycle(
            logger,
            hidden > 0,
            "recon_full_pass_complete count=%d hidden=%d generation=%d",
            count,
            hidden,
            state.generation,
        )
        return count, True

    @_dialog_sync_rpc_scope(DemandKind.DIALOG_LIGHT_RECONCILIATION, AcquisitionKind.TOPIC_SNAPSHOT)
    async def _refresh_forum_topics(
        self,
        dialog_id: int,
        entity: _EntityLike,
    ) -> int:
        """Refresh a topic-capable dialog's canonical topic_metadata snapshot.

        Called from run_light_pass after entity is already fetched. Handles
        TelegramRpcThrottled by sleeping (interruptible by shutdown_event) and returning 0.
        The injected application service identifies both forum supergroups and
        private bot dialogs with ``bot_forum_view``.

        Returns count of topics written.
        """
        if self._topic_refresher is None:
            return 0
        try:
            count = await self._topic_refresher.refresh(dialog_id, entity)
        except TelegramRpcThrottled as exc:
            if exc.retry_after_seconds is None:
                return 0
            wait_s = exc.retry_after_seconds
            logger.warning(
                "recon_forum_topics_flood_wait dialog_id=%d wait=%ds",
                dialog_id,
                wait_s,
            )
            await sleep_through_flood(self._shutdown_event, wait_s)
            return 0
        except TopicSourceUnavailableError as exc:
            logger.warning(
                "recon_forum_topics_fetch_failed dialog_id=%d error=%s",
                dialog_id,
                exc,
            )
            return 0

        logger.debug("recon_topics_complete dialog_id=%d count=%d", dialog_id, count)
        return count


class DialogLightReconciliationDemandAdapter:
    """Bounded dirty-dialog adapter over ``dialogs.needs_refresh``."""

    demand_kind = DemandKind.DIALOG_LIGHT_RECONCILIATION

    def __init__(self, worker: DialogReconciliationWorker) -> None:
        self._worker = worker

    def status(self, now: float) -> DemandStatus | None:
        """Report dirty visible dialogs without mutating their flags."""
        del now
        row = cast(tuple[int] | None, self._worker._conn.execute(_SELECT_DIRTY_DIALOG_EXISTS_SQL).fetchone())
        if row is None:
            return None
        return DemandStatus(release_at=0.0)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Refresh dirty entity rows until the attempt budget yields."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        if self.status(time.time()) is None:
            return
        with demand_context(DemandKind.DIALOG_LIGHT_RECONCILIATION):
            with rpc_attempt_budget(budget):
                try:
                    await self._worker.run_light_pass(refresh_topics=False)
                except RpcAttemptBudgetExhaustedError:
                    return


class DialogFullReconciliationDemandAdapter:
    """Execute bounded, generation-safe slices of the daily dialog traversal."""

    demand_kind = DemandKind.DIALOG_FULL_RECONCILIATION

    def __init__(self, worker: DialogReconciliationWorker, *, interval_seconds: float | None = None) -> None:
        resolved_interval = demand_freshness_seconds(self.demand_kind) if interval_seconds is None else interval_seconds
        if resolved_interval <= 0:
            raise ValueError("interval_seconds must be positive")
        self._worker = worker
        self._interval_seconds = resolved_interval

    def status(self, now: float) -> DemandStatus:
        """Report the persisted daily release boundary without writes."""
        del now
        completed_at = _read_last_full_reconciliation_at(self._worker._conn)
        release_at = 0.0 if completed_at is None else completed_at + self._interval_seconds
        freshness_deadline = None if completed_at is None else release_at
        state = cast(
            tuple[str] | None,
            self._worker._conn.execute(
                "SELECT status FROM dialog_full_reconciliation_state WHERE singleton=1"
            ).fetchone(),
        )
        if state == ("in_progress",):
            return DemandStatus(release_at=0.0, freshness_deadline=freshness_deadline)
        return DemandStatus(release_at=release_at, freshness_deadline=freshness_deadline)

    async def run_slice(self, budget: RpcAttemptBudget) -> None:
        """Resume from the committed cursor until the actual-attempt budget yields."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        now = time.time()
        if not self.status(now).is_ready(now):
            return
        with demand_context(DemandKind.DIALOG_FULL_RECONCILIATION):
            with rpc_attempt_budget(budget):
                with rpc_scope(
                    TelegramRpcSource.DIALOG_SYNC,
                    acquisition_kind=AcquisitionKind.DIALOG_TRAVERSAL,
                ):
                    await self._worker._run_full_pass_slice(refresh_topics=False, wait_on_throttle=False)


_EXPORTED_SYMBOLS = (
    DialogBootstrapDemandAdapter,
    DialogFullReconciliationDemandAdapter,
    DialogLightReconciliationDemandAdapter,
    DialogReconciliationWorker,
    DialogsBootstrapWorker,
    DialogsBootstrapWorker.run,
)
