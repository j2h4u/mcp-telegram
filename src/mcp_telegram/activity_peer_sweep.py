"""Shared substrate for per-peer self-search sweeps and working-set enrollment.

This module provides:
  - SkipReason: structured per-call outcome with distinct ACCESS_SKIP vs
    HISTORY_FLOOR vs FLOOD_WAIT reasons (concern 3 fix).
  - SweepResult: dataclass carrying fetched_ids, persisted, min/max_id,
    skip_reason, flood_wait_seconds, and a hit_floor property.
  - sweep_peer_once: FloodWait-neutral per-peer self-search primitive.
  - enroll_activity_dialog: shared enrollment helper (reused by schedulers
    and plan 05 daemon-api wiring).
  - build_working_set: working-set builder enrolling from dialogs.type=
    'supergroup'/'channel' with durable resolver-path FloodWait retry.
  - _load_dialog_state / _save_dialog_state: per-tier cursor helpers.

Phase 54: linked_chat_id resolution is dialogs-cache-trust; no per-channel
backoff helpers — see activity_peer_resolve.resolve_linked_chat_id.

No scheduling loops live here — those are plans 03 and 04.
"""

import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast

from .activity_peer_resolve import LinkedChatResolution, resolve_input_peer, resolve_linked_chat_id
from .activity_sync import INSERT_OWN_ONLY_DIALOG_SQL, _ActivityClient, call_with_timeout, extract_dialog_id
from .sync_worker import ExtractedMessage, extract_message_row, insert_messages_with_fts

logger = logging.getLogger(__name__)

# Thin dialogs row written alongside the own_only synced_dialogs insert so the
# peer becomes visible to list_dialogs / get_my_recent_activity. INSERT OR IGNORE
# is mandatory: it preserves any already-resolved/bootstrap/synced dialogs row and
# never downgrades name/type/needs_refresh. name/type/members/created stay NULL
# until DialogReconciler.run_light_pass (WHERE needs_refresh=1 AND hidden=0) fills
# them on its hourly cycle (Bug #1 lazy fix).
_INSERT_THIN_DIALOG_SQL = (
    "INSERT OR IGNORE INTO dialogs"
    " (dialog_id, needs_refresh, snapshot_at, archived, pinned, hidden,"
    " unread_mentions_count, unread_reactions_count)"
    " VALUES (?, 1, ?, 0, 0, 0, 0, 0)"
)


# ---------------------------------------------------------------------------
# SkipReason: load-bearing enum — ONLY HISTORY_FLOOR authorises cold complete
# ---------------------------------------------------------------------------


class SkipReason(StrEnum):
    NONE = "none"
    ACCESS_SKIP = "access_skip"
    # resolve_input_peer returned None (transient cache/session miss or
    # access-loss), or a TimeoutError was caught. The caller MUST set a
    # per-tier *_next_retry_at and leave the peer re-selectable. A transient
    # resolve failure can NEVER permanently end ColdBackfill.
    HISTORY_FLOOR = "history_floor"
    # A genuinely empty batch was returned by a REACHABLE peer. Tier B may
    # set cold_status='complete' ONLY for this reason.
    FLOOD_WAIT = "flood_wait"
    # FloodWaitError surfaced. The caller owns the durable backoff write.


# ---------------------------------------------------------------------------
# SweepResult
# ---------------------------------------------------------------------------


@dataclass
class SweepResult:
    """Result of a single sweep_peer_once call."""

    fetched_ids: list[int]
    persisted: int
    min_id: int | None  # min of batch — Tier-B downward cursor
    max_id: int | None  # max of batch — Tier-A high-water
    skip_reason: SkipReason = SkipReason.NONE
    flood_wait_seconds: int | None = None

    @property
    def hit_floor(self) -> bool:
        """True ONLY for HISTORY_FLOOR — never for access-skip or flood-wait.

        Tier B must use this property (not skip_reason equality) to guard
        the cold_status='complete' transition so a transient access-loss can
        never masquerade as history-floor completion (concern 3).
        """
        return self.skip_reason is SkipReason.HISTORY_FLOOR


@dataclass
class PeerSweepRequest:
    """Request context for a single per-peer self-search sweep."""

    client: _ActivityClient
    conn: sqlite3.Connection
    dialog_id: int
    offset_id: int
    min_id: int
    limit: int


_LEGACY_PEER_SWEEP_POSITIONAL_ARGS = 3


class _SweepMessageLike(Protocol):
    id: int
    peer_id: object | None


class _SweepResultLike(Protocol):
    messages: Sequence[_SweepMessageLike] | None


def _coerce_peer_sweep_request(*args: object, **kwargs: object) -> PeerSweepRequest:
    """Normalize legacy call shapes into a single request record."""
    if len(args) == 1 and isinstance(args[0], PeerSweepRequest) and not kwargs:
        return args[0]

    if len(args) == _LEGACY_PEER_SWEEP_POSITIONAL_ARGS:
        client, conn, dialog_id = cast(tuple[_ActivityClient, sqlite3.Connection, int], args)
    else:
        client = cast(_ActivityClient, kwargs.pop("client"))
        conn = cast(sqlite3.Connection, kwargs.pop("conn"))
        dialog_id = cast(int, kwargs.pop("dialog_id"))

    offset_id = cast(int, kwargs.pop("offset_id"))
    min_id = cast(int, kwargs.pop("min_id"))
    limit = cast(int, kwargs.pop("limit"))
    if kwargs:
        raise TypeError(f"sweep_peer_once: unexpected keyword arguments {sorted(kwargs)!r}")

    return PeerSweepRequest(
        client=client,
        conn=conn,
        dialog_id=dialog_id,
        offset_id=offset_id,
        min_id=min_id,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# sweep_peer_once: FloodWait-neutral per-peer self-search primitive
# ---------------------------------------------------------------------------


async def sweep_peer_once(*args: object, **kwargs: object) -> SweepResult:
    """Search for self-authored messages in a single peer and persist them.

    Direction-agnostic: takes explicit offset_id + min_id, reports both
    min_id and max_id of the batch.
      - HotSweep (plan 03) reads max_id (forward/newest-side cursor).
      - ColdBackfill (plan 04) reads min_id (backward cursor).

    FloodWait-neutral: on FloodWaitError the function returns immediately with
    skip_reason=FLOOD_WAIT and flood_wait_seconds set — it does NOT sleep.
    The owning scheduler sets the per-tier *_next_retry_at.

    TimeoutError (wedged RPC): treated as ACCESS_SKIP — a transient fault,
    not history-floor completion.
    """
    request = _coerce_peer_sweep_request(*args, **kwargs)
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.messages import SearchRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerSelf

    # Step 1: entity-type-aware peer resolution from session
    peer = await resolve_input_peer(request.client, request.dialog_id)
    if peer is None:
        logger.debug("sweep_peer_once_access_skip dialog_id=%r reason=resolve_none", request.dialog_id)
        return SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )

    # Step 2: issue per-peer self-search with concrete peer (not InputPeerEmpty)
    try:
        result = await call_with_timeout(
            request.client,
            SearchRequest(
                peer=peer,
                q="",
                filter=InputMessagesFilterEmpty(),
                from_id=InputPeerSelf(),
                offset_id=request.offset_id,
                add_offset=0,
                limit=request.limit,
                max_id=0,
                min_id=request.min_id,
                hash=0,
                min_date=None,
                max_date=None,
            ),
        )
    except FloodWaitError as exc:
        logger.warning(
            "sweep_peer_once_flood dialog_id=%r flood_wait_seconds=%d",
            request.dialog_id,
            exc.seconds,
        )
        # FloodWait-NEUTRAL: surface the wait, do not sleep
        return SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.FLOOD_WAIT,
            flood_wait_seconds=int(exc.seconds),
        )
    except TimeoutError:
        logger.warning("sweep_peer_once_timeout dialog_id=%r offset_id=%r", request.dialog_id, request.offset_id)
        # Wedged RPC is transient — ACCESS_SKIP, never HISTORY_FLOOR
        return SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )

    batch = list(cast(_SweepResultLike, result).messages or [])

    # Step 5: genuinely empty batch from a reachable peer → history floor
    if not batch:
        return SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.HISTORY_FLOOR,
        )

    # Step 3-4: extract and persist via canonical pipeline
    extracted: list[ExtractedMessage] = []
    for m in batch:
        did = extract_dialog_id(m)
        if did is None:
            continue
        extracted.append(extract_message_row(did, m))

    persisted = 0
    with request.conn:
        if extracted:
            insert_messages_with_fts(request.conn, extracted)
            persisted = len(extracted)

    msg_ids = [m.id for m in batch]
    return SweepResult(
        fetched_ids=msg_ids,
        persisted=persisted,
        min_id=min(msg_ids) if msg_ids else None,
        max_id=max(msg_ids) if msg_ids else None,
        skip_reason=SkipReason.NONE,
    )


# ---------------------------------------------------------------------------
# Shared enrollment helper — reused by build_working_set AND plan 05
# ---------------------------------------------------------------------------


def enroll_activity_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    source: str,
    *,
    last_activity_at: int | None = None,
) -> None:
    """Upsert a single peer into activity_dialog_state and synced_dialogs.

    ON CONFLICT clause refreshes source/updated_at/last_activity_at.
    It does NOT touch hot_cursor, hot_next_retry_at, cold_offset_id,
    cold_status, or cold_next_retry_at — per-tier cursor/retry state is
    owned solely by the schedulers (concern 5 isolation).

    synced_dialogs enrollment uses INSERT OR IGNORE so an existing
    higher-status row (e.g. 'active'/'synced') is NEVER downgraded.
    """
    now = int(time.time())
    with conn:
        conn.execute(
            """
            INSERT INTO activity_dialog_state
                (dialog_id, source, last_activity_at, cold_status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(dialog_id) DO UPDATE SET
                -- Provenance precedence: a peer enrolled as a direct 'supergroup'
                -- membership must NOT be downgraded to 'linked_chat' by a later
                -- trace-driven enrollment (the same peer can be both a direct
                -- supergroup AND a channel's linked discussion group). 'supergroup'
                -- is sticky; any other existing source is refreshed normally.
                source           = CASE
                                       WHEN activity_dialog_state.source = 'supergroup'
                                       THEN activity_dialog_state.source
                                       ELSE excluded.source
                                   END,
                updated_at       = excluded.updated_at,
                last_activity_at = COALESCE(excluded.last_activity_at,
                                            activity_dialog_state.last_activity_at)
            """,
            (dialog_id, source, last_activity_at, now, now),
        )
        conn.execute(INSERT_OWN_ONLY_DIALOG_SQL, (dialog_id,))
        conn.execute(_INSERT_THIN_DIALOG_SQL, (dialog_id, now))


# ---------------------------------------------------------------------------
# Cursor helpers over activity_dialog_state
# ---------------------------------------------------------------------------

_DIALOG_STATE_COLUMNS = frozenset(
    {
        "hot_cursor",
        "hot_last_sync_at",
        "hot_next_retry_at",
        "hot_last_error",
        "cold_offset_id",
        "cold_status",
        "cold_next_retry_at",
        "cold_last_error",
    }
)


_DialogStateRow = tuple[int | None, int | None, int | None, str | None, int | None, str | None, int | None, str | None]


def _load_dialog_state(conn: sqlite3.Connection, dialog_id: int) -> dict[str, int | None | str]:
    """Return the per-tier cursor/retry fields for a peer, or {} if absent."""
    row = cast(
        _DialogStateRow | None,
        conn.execute(
        """
        SELECT hot_cursor, hot_last_sync_at, hot_next_retry_at, hot_last_error,
               cold_offset_id, cold_status, cold_next_retry_at, cold_last_error
        FROM activity_dialog_state
        WHERE dialog_id = ?
        """,
        (dialog_id,),
        ).fetchone(),
    )
    if row is None:
        return {}
    keys = [
        "hot_cursor",
        "hot_last_sync_at",
        "hot_next_retry_at",
        "hot_last_error",
        "cold_offset_id",
        "cold_status",
        "cold_next_retry_at",
        "cold_last_error",
    ]
    return dict(zip(keys, row, strict=True))


def _save_dialog_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    **fields: object,
) -> None:
    """Update whitelisted per-tier cursor/retry fields for a peer.

    Only fields listed in _DIALOG_STATE_COLUMNS are accepted; unknown
    field names raise ValueError to prevent silent schema drift.
    """
    unknown = set(fields) - _DIALOG_STATE_COLUMNS
    if unknown:
        raise ValueError(f"_save_dialog_state: unknown fields {unknown!r}")
    if not fields:
        return
    set_clause = ", ".join(f"{col} = ?" for col in fields)
    values = list(fields.values())
    values.append(int(time.time()))
    values.append(dialog_id)
    with conn:
        conn.execute(
            f"UPDATE activity_dialog_state SET {set_clause}, updated_at = ? WHERE dialog_id = ?",
            values,
        )


# ---------------------------------------------------------------------------
# Working-set builder
# ---------------------------------------------------------------------------


async def build_working_set(client: _ActivityClient, conn: sqlite3.Connection) -> int:
    """Build the per-peer self-search working set and enroll peers.

    Source: dialogs.type='supergroup' (megagroups) and dialogs.type='channel'
    (broadcast channels whose linked discussion group is resolved via
    resolve_linked_chat_id (post-Phase-54: dialogs-cache hot read; falls through
    to GetFullChannelRequest only when linked_chat_resolved_at IS NULL)).
    NOT entities.type='group' — that taxonomy differs (concern 4 fix).

    Returns the count of peers enrolled in the working set.
    """
    # Step 1: standalone supergroups (directly self-searchable)
    supergroup_rows = cast(list[tuple[int, int | None]], conn.execute(
        "SELECT dialog_id, last_message_at FROM dialogs WHERE type = 'supergroup' AND hidden = 0"
    ).fetchall())

    # Step 2: broadcast channels (need linked_chat resolution)
    channel_rows = cast(list[tuple[int, int | None]], conn.execute(
        "SELECT dialog_id, last_message_at FROM dialogs WHERE type = 'channel' AND hidden = 0"
    ).fetchall())

    working_set: dict[int, int | None] = {}  # peer_id → last_activity_at

    # Enroll supergroups directly
    working_set = dict(supergroup_rows)
    supergroup_ids = {dialog_id for dialog_id, _ in supergroup_rows}

    # Step 3: resolve broadcast channels to their discussion groups
    for channel_id, channel_last_message_at in channel_rows:
        res: LinkedChatResolution = await resolve_linked_chat_id(client, conn, channel_id)

        if res.flood_wait_seconds is not None:
            logger.warning(
                "build_working_set_channel_flood channel_id=%r flood_wait_seconds=%d"
                " — halting resolution pass (FloodWait from GetFullChannelRequest is"
                " account-global; remaining channels stay due for next sweep cycle)",
                channel_id,
                res.flood_wait_seconds,
            )
            break

        if res.linked_chat_id is not None:
            existing = working_set.get(res.linked_chat_id)
            if existing is None:
                working_set[res.linked_chat_id] = channel_last_message_at
        # else: no discussion group → drop channel (D-03)

    # Step 4-5: enroll all peers via shared helper
    for peer_id, last_activity_at in working_set.items():
        source = "supergroup" if peer_id in supergroup_ids else "linked_chat"
        enroll_activity_dialog(conn, peer_id, source, last_activity_at=last_activity_at)

    return len(working_set)
