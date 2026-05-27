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
  - _channel_resolution_due / _record_channel_resolution_flood /
    _clear_channel_resolution: durable resolver-path retry helpers over
    activity_channel_resolution (schema v23).

No scheduling loops live here — those are plans 03 and 04.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .activity_peer_resolve import LinkedChatResolution, resolve_input_peer, resolve_linked_chat_id
from .activity_sync import INSERT_OWN_ONLY_DIALOG_SQL, call_with_timeout, extract_dialog_id
from .sync_worker import ExtractedMessage, extract_message_row, insert_messages_with_fts

logger = logging.getLogger(__name__)

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
    min_id: int | None        # min of batch — Tier-B downward cursor
    max_id: int | None        # max of batch — Tier-A high-water
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


# ---------------------------------------------------------------------------
# sweep_peer_once: FloodWait-neutral per-peer self-search primitive
# ---------------------------------------------------------------------------

async def sweep_peer_once(
    client: Any,
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    offset_id: int,
    min_id: int,
    limit: int,
) -> SweepResult:
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
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.messages import SearchRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerSelf

    # Step 1: entity-type-aware peer resolution from session
    peer = await resolve_input_peer(client, dialog_id)
    if peer is None:
        logger.debug(
            "sweep_peer_once_access_skip dialog_id=%r reason=resolve_none", dialog_id
        )
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
            client,
            SearchRequest(
                peer=peer,
                q="",
                filter=InputMessagesFilterEmpty(),
                from_id=InputPeerSelf(),
                offset_id=offset_id,
                add_offset=0,
                limit=limit,
                max_id=0,
                min_id=min_id,
                hash=0,
                min_date=None,
                max_date=None,
            ),
        )
    except FloodWaitError as exc:
        logger.warning(
            "sweep_peer_once_flood dialog_id=%r flood_wait_seconds=%d",
            dialog_id, exc.seconds,
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
        logger.warning(
            "sweep_peer_once_timeout dialog_id=%r offset_id=%r", dialog_id, offset_id
        )
        # Wedged RPC is transient — ACCESS_SKIP, never HISTORY_FLOOR
        return SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.ACCESS_SKIP,
        )

    batch = list(getattr(result, "messages", []) or [])

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
    with conn:
        if extracted:
            insert_messages_with_fts(conn, extracted)
            persisted = len(extracted)

    msg_ids = [m.id for m in batch if getattr(m, "id", None) is not None]
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
                source           = excluded.source,
                updated_at       = excluded.updated_at,
                last_activity_at = COALESCE(excluded.last_activity_at,
                                            activity_dialog_state.last_activity_at)
            """,
            (dialog_id, source, last_activity_at, now, now),
        )
        conn.execute(INSERT_OWN_ONLY_DIALOG_SQL, (dialog_id,))


# ---------------------------------------------------------------------------
# Cursor helpers over activity_dialog_state
# ---------------------------------------------------------------------------

_DIALOG_STATE_COLUMNS = frozenset({
    "hot_cursor",
    "hot_last_sync_at",
    "hot_next_retry_at",
    "hot_last_error",
    "cold_offset_id",
    "cold_status",
    "cold_next_retry_at",
    "cold_last_error",
})


def _load_dialog_state(conn: sqlite3.Connection, dialog_id: int) -> dict:
    """Return the per-tier cursor/retry fields for a peer, or {} if absent."""
    row = conn.execute(
        """
        SELECT hot_cursor, hot_last_sync_at, hot_next_retry_at, hot_last_error,
               cold_offset_id, cold_status, cold_next_retry_at, cold_last_error
        FROM activity_dialog_state
        WHERE dialog_id = ?
        """,
        (dialog_id,),
    ).fetchone()
    if row is None:
        return {}
    keys = [
        "hot_cursor", "hot_last_sync_at", "hot_next_retry_at", "hot_last_error",
        "cold_offset_id", "cold_status", "cold_next_retry_at", "cold_last_error",
    ]
    return dict(zip(keys, row))


def _save_dialog_state(
    conn: sqlite3.Connection,
    dialog_id: int,
    **fields: Any,
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
# Durable channel-resolution retry helpers (activity_channel_resolution)
# ---------------------------------------------------------------------------

def _channel_resolution_due(
    conn: sqlite3.Connection, channel_id: int, *, now: int
) -> bool:
    """Return True when the channel is due for a resolution attempt.

    Returns False (skip) iff activity_channel_resolution.next_retry_at is
    non-NULL and greater than now — the durable backoff is being honored.
    Returns True when the row is absent or next_retry_at is NULL/past.
    """
    row = conn.execute(
        "SELECT next_retry_at FROM activity_channel_resolution WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    if row is None:
        return True  # no record → due
    next_retry_at = row[0]
    if next_retry_at is None:
        return True  # cleared → due
    return int(next_retry_at) <= now  # past → due; future → skip


def _record_channel_resolution_flood(
    conn: sqlite3.Connection,
    channel_id: int,
    *,
    next_retry_at: int,
    last_error: str,
) -> None:
    """Persist durable resolver-path FloodWait backoff for a broadcast channel.

    On FloodWaitError, build_working_set calls this to write
    next_retry_at = now + flood_wait_seconds into activity_channel_resolution
    so the backoff survives builder passes and daemon restarts.
    """
    now = int(time.time())
    with conn:
        conn.execute(
            """
            INSERT INTO activity_channel_resolution
                (channel_id, next_retry_at, last_error, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                next_retry_at = excluded.next_retry_at,
                last_error    = excluded.last_error,
                updated_at    = excluded.updated_at
            """,
            (channel_id, next_retry_at, last_error, now),
        )


def _clear_channel_resolution(
    conn: sqlite3.Connection, channel_id: int, *, now: int
) -> None:
    """Clear the resolver-path backoff after a successful (or clean-None) resolution.

    Sets next_retry_at = NULL so the channel is immediately due again on the
    next builder pass.  last_error is also cleared.
    """
    with conn:
        conn.execute(
            """
            INSERT INTO activity_channel_resolution
                (channel_id, next_retry_at, last_error, updated_at)
            VALUES (?, NULL, NULL, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                next_retry_at = NULL,
                last_error    = NULL,
                updated_at    = excluded.updated_at
            """,
            (channel_id, now),
        )


# ---------------------------------------------------------------------------
# Working-set builder
# ---------------------------------------------------------------------------

async def build_working_set(client: Any, conn: sqlite3.Connection) -> int:
    """Build the per-peer self-search working set and enroll peers.

    Source: dialogs.type='supergroup' (megagroups) and dialogs.type='channel'
    (broadcast channels whose linked discussion group is resolved via
    resolve_linked_chat_id).  NOT entities.type='group' — that taxonomy
    differs (concern 4 fix).

    Returns the count of peers enrolled in the working set.
    """
    now = int(time.time())

    # Step 1: standalone supergroups (directly self-searchable)
    supergroup_rows = conn.execute(
        "SELECT dialog_id, last_message_at FROM dialogs WHERE type = 'supergroup' AND hidden = 0"
    ).fetchall()

    # Step 2: broadcast channels (need linked_chat resolution)
    channel_rows = conn.execute(
        "SELECT dialog_id, last_message_at FROM dialogs WHERE type = 'channel' AND hidden = 0"
    ).fetchall()

    working_set: dict[int, int | None] = {}  # peer_id → last_activity_at

    # Enroll supergroups directly
    for dialog_id, last_message_at in supergroup_rows:
        working_set[dialog_id] = last_message_at

    # Step 3: resolve broadcast channels to their discussion groups
    for channel_id, channel_last_message_at in channel_rows:
        # Durable backoff: skip if still in flood-wait period
        if not _channel_resolution_due(conn, channel_id, now=now):
            logger.debug(
                "build_working_set_channel_skip_backoff channel_id=%r", channel_id
            )
            continue

        res: LinkedChatResolution = await resolve_linked_chat_id(client, conn, channel_id)

        if res.flood_wait_seconds is not None:
            # FloodWait from GetFullChannel is ACCOUNT-GLOBAL, not per-channel:
            # once Telegram issues it, every further request in this pass is sent
            # *during* the wait window, which is exactly what escalates rate-limiting
            # toward an account ban. Persist durable backoff for this channel, then
            # STOP the pass (break, not continue). The remaining unresolved channels
            # stay due and drain over subsequent passes (enroll cadence ~30 min), by
            # which point the short global wait has long cleared. Resolved channels
            # are cache-first (entity_details TTL) so they never re-hit the API.
            next_retry_at = now + res.flood_wait_seconds
            _record_channel_resolution_flood(
                conn,
                channel_id,
                next_retry_at=next_retry_at,
                last_error=f"FloodWaitError({res.flood_wait_seconds}s)",
            )
            logger.warning(
                "build_working_set_channel_flood channel_id=%r flood_wait_seconds=%d"
                " next_retry_at=%d — halting resolution pass (account-global wait)",
                channel_id, res.flood_wait_seconds, next_retry_at,
            )
            break

        # Clean resolution (linked or clean-None) — clear any prior backoff
        _clear_channel_resolution(conn, channel_id, now=now)

        if res.linked_chat_id is not None:
            # Use channel's last_message_at as fallback if linked chat has
            # no direct dialogs row (review MEDIUM)
            existing = working_set.get(res.linked_chat_id)
            if existing is None:
                # No prior entry — use channel's last_message_at as fallback
                working_set[res.linked_chat_id] = channel_last_message_at
            # else: already in set (dedup — possibly a direct supergroup row)

        # else: no discussion group → drop channel (D-03)

    # Step 4-5: enroll all peers via shared helper
    for peer_id, last_activity_at in working_set.items():
        source = "supergroup" if peer_id in {r[0] for r in supergroup_rows} else "linked_chat"
        enroll_activity_dialog(conn, peer_id, source, last_activity_at=last_activity_at)

    return len(working_set)
