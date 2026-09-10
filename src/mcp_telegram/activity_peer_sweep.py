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
  - run_working_set_enrollment_slice: restart-safe bounded replacement for
    coordinator-owned execution.
  - _load_dialog_state / _save_dialog_state: per-tier cursor helpers.

Phase 54: linked_chat_id resolution is dialogs-cache-trust; no per-channel
backoff helpers — see activity_peer_resolve.resolve_linked_chat_id.

No scheduling loops live here — those are plans 03 and 04.
"""

import asyncio
import logging
import math
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast

from telethon.tl.types import TypeInputPeer

from .access_lifecycle import set_access_lost
from .activity_peer_resolve import LinkedChatResolution, resolve_input_peer, resolve_linked_chat_id
from .activity_substrate import ActivityClient, call_with_timeout
from .flood import TelegramRpcThrottled, _raise_if_latched
from .hydration_queue import HydrationPriority
from .message_contracts import ExtractedMessage
from .messages.sqlite_bundle import insert_messages_with_fts, message_exists
from .messages.telegram_adapter import extract_dialog_id, extract_message_row
from .own_only import enroll_own_only_sync_dialog
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import AcquisitionKind, RpcAttemptBudgetExhaustedError, acquisition_context
from .telegram_rpc_scheduler import RpcAdmissionClosedError, TelegramRpcSource, rpc_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PeerSweepSearchPacing:
    success_s: float = 0.25


@dataclass(frozen=True, slots=True)
class PeerSweepPacing:
    search: PeerSweepSearchPacing = PeerSweepSearchPacing()


_PACING = PeerSweepPacing()


@dataclass(frozen=True, slots=True)
class WorkingSetResult:
    """Result of refreshing the peer working set."""

    enrolled_count: int
    flood_wait_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class WorkingSetEnrollmentSliceResult:
    """Durable outcome from one bounded working-set enrollment unit."""

    enrolled_count: int = 0
    completed: bool = False
    flood_wait_seconds: int | None = None
    consumed: bool = False


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
    # TelegramRpcThrottled surfaced. The caller owns the durable backoff write.


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
    pages_fetched: int = field(default=0, compare=False)
    rpc_calls: int = field(default=0, compare=False)
    extracted: int = field(default=0, compare=False)
    genuinely_new: int = field(default=0, compare=False)
    genuinely_new_keys: frozenset[tuple[int, int]] = field(default=frozenset(), compare=False)
    completed: bool = field(default=False, compare=False)

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

    client: ActivityClient
    conn: sqlite3.Connection
    dialog_id: int
    offset_id: int
    min_id: int
    limit: int
    timeout_s: float
    hydration_priority: HydrationPriority = HydrationPriority.BACKFILL


_LEGACY_PEER_SWEEP_POSITIONAL_ARGS = 3


class _SweepMessageLike(Protocol):
    id: int
    peer_id: object | None


class _SweepResultLike(Protocol):
    messages: Sequence[_SweepMessageLike] | None


@dataclass(frozen=True, slots=True)
class _SearchOutcome:
    result: _SweepResultLike | None
    rpc_duration_s: float
    early_result: SweepResult | None = None


async def _pace_successful_search_request() -> None:
    """Apply a tiny fixed pause after a successful SearchRequest."""
    await asyncio.sleep(_PACING.search.success_s)


async def _resolve_peer_for_sweep(request: PeerSweepRequest) -> TypeInputPeer | SweepResult | None:
    """Resolve one peer while preserving the governed-RPC error boundary."""
    try:
        return await resolve_input_peer(request.client, request.dialog_id)
    except RpcAttemptBudgetExhaustedError:
        # A slice boundary is a continuation point, not an access failure.
        # Leave the owning tier's cursor/lease untouched so the next slice can
        # retry the same peer with a fresh budget.
        raise
    except RpcAdmissionClosedError:
        raise
    except TelegramRpcThrottled as exc:
        _raise_if_latched(exc)
        logger.warning("sweep_peer_once_resolution_throttled dialog_id=%r", request.dialog_id)
        return _access_skip_result(rpc_calls=1)
    except Exception:
        logger.warning("sweep_peer_once_resolution_error dialog_id=%r", request.dialog_id, exc_info=True)
        return _access_skip_result(rpc_calls=1)


def _extract_sweep_messages(
    request: PeerSweepRequest, batch: Sequence[_SweepMessageLike]
) -> tuple[list[ExtractedMessage], frozenset[tuple[int, int]]]:
    extracted: list[ExtractedMessage] = []
    seen_keys: set[tuple[int, int]] = set()
    for message in batch:
        dialog_id = extract_dialog_id(message)
        if dialog_id is None:
            continue
        extracted_message = extract_message_row(dialog_id, message)
        stored_message = getattr(extracted_message, "message", None)
        key = (
            int(getattr(stored_message, "dialog_id", dialog_id)),
            int(getattr(stored_message, "message_id", message.id)),
        )
        if key in seen_keys:
            continue
        seen_keys.add(key)
        extracted.append(extracted_message)

    genuinely_new_keys = {key for key in seen_keys if not message_exists(request.conn, *key)}
    return extracted, frozenset(genuinely_new_keys)


def _extract_and_persist_sweep_messages(
    request: PeerSweepRequest, batch: Sequence[_SweepMessageLike]
) -> tuple[list[ExtractedMessage], frozenset[tuple[int, int]]] | SweepResult:
    """Return page rows or an explicit non-completion processing error."""
    try:
        extracted, genuinely_new_keys = _extract_sweep_messages(request, batch)
    except Exception:
        logger.warning("sweep_peer_once_extraction_error dialog_id=%r", request.dialog_id, exc_info=True)
        return _access_skip_result(rpc_calls=2, pages_fetched=1)

    try:
        if extracted:
            with request.conn:
                insert_messages_with_fts(
                    request.conn,
                    extracted,
                    priority=request.hydration_priority,
                )
    except Exception:
        logger.warning("sweep_peer_once_persistence_error dialog_id=%r", request.dialog_id, exc_info=True)
        return _access_skip_result(rpc_calls=2, pages_fetched=1)
    return extracted, genuinely_new_keys


def _elapsed_s(started_at: float) -> float:
    return time.monotonic() - started_at


def _coerce_peer_sweep_request(*args: object, **kwargs: object) -> PeerSweepRequest:
    """Normalize legacy call shapes into a single request record."""
    if len(args) == 1 and isinstance(args[0], PeerSweepRequest) and not kwargs:
        return args[0]

    if len(args) == _LEGACY_PEER_SWEEP_POSITIONAL_ARGS:
        client, conn, dialog_id = cast(tuple[ActivityClient, sqlite3.Connection, int], args)
    else:
        client = cast(ActivityClient, kwargs.pop("client"))
        conn = cast(sqlite3.Connection, kwargs.pop("conn"))
        dialog_id = cast(int, kwargs.pop("dialog_id"))

    offset_id = cast(int, kwargs.pop("offset_id"))
    min_id = cast(int, kwargs.pop("min_id"))
    limit = cast(int, kwargs.pop("limit"))
    timeout_s = cast(float, kwargs.pop("timeout_s"))
    hydration_priority = cast(
        HydrationPriority,
        kwargs.pop("hydration_priority", HydrationPriority.BACKFILL),
    )
    if kwargs:
        raise TypeError(f"sweep_peer_once: unexpected keyword arguments {sorted(kwargs)!r}")

    return PeerSweepRequest(
        client=client,
        conn=conn,
        dialog_id=dialog_id,
        offset_id=offset_id,
        min_id=min_id,
        limit=limit,
        timeout_s=timeout_s,
        hydration_priority=hydration_priority,
    )


def _access_skip_result(*, rpc_calls: int = 0, pages_fetched: int = 0) -> SweepResult:
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.ACCESS_SKIP,
        rpc_calls=rpc_calls,
        pages_fetched=pages_fetched,
    )


def _flood_wait_result(seconds: int, *, rpc_calls: int = 0) -> SweepResult:
    return SweepResult(
        fetched_ids=[],
        persisted=0,
        min_id=None,
        max_id=None,
        skip_reason=SkipReason.FLOOD_WAIT,
        flood_wait_seconds=seconds,
        rpc_calls=rpc_calls,
    )


async def _search_self_messages(request: PeerSweepRequest, peer: TypeInputPeer, *, rpc_calls: int) -> _SearchOutcome:
    from telethon.tl.functions.messages import SearchRequest
    from telethon.tl.types import InputMessagesFilterEmpty, InputPeerSelf

    search_started_at = time.monotonic()
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
            timeout_s=request.timeout_s,
        )
    except RpcAdmissionClosedError:
        raise
    except RpcAttemptBudgetExhaustedError:
        # Do not turn a local slice bound into ACCESS_SKIP.  The caller owns
        # durable continuation and will retry this page in the next slice.
        raise
    except TelegramRpcThrottled as exc:
        _raise_if_latched(exc)
        assert exc.retry_after_seconds is not None
        logger.warning(
            "sweep_peer_once_flood dialog_id=%r flood_wait_seconds=%s rpc_duration_s=%.3f",
            request.dialog_id,
            exc.retry_after_seconds,
            _elapsed_s(search_started_at),
        )
        return _SearchOutcome(
            result=None,
            rpc_duration_s=_elapsed_s(search_started_at),
            early_result=_flood_wait_result(exc.retry_after_seconds, rpc_calls=rpc_calls),
        )
    except TimeoutError:
        logger.warning(
            "sweep_peer_once_timeout dialog_id=%r offset_id=%r rpc_duration_s=%.3f",
            request.dialog_id,
            request.offset_id,
            _elapsed_s(search_started_at),
        )
        return _SearchOutcome(
            result=None,
            rpc_duration_s=_elapsed_s(search_started_at),
            early_result=_access_skip_result(rpc_calls=rpc_calls),
        )
    except ACCESS_LOST_ERRORS as exc:
        set_access_lost(request.conn, request.dialog_id, int(time.time()), reason=type(exc).__name__)
        request.conn.commit()
        return _SearchOutcome(
            result=None,
            rpc_duration_s=_elapsed_s(search_started_at),
            early_result=_access_skip_result(rpc_calls=rpc_calls),
        )
    except Exception:
        logger.warning(
            "sweep_peer_once_search_error dialog_id=%r offset_id=%r rpc_duration_s=%.3f",
            request.dialog_id,
            request.offset_id,
            _elapsed_s(search_started_at),
            exc_info=True,
        )
        return _SearchOutcome(
            result=None,
            rpc_duration_s=_elapsed_s(search_started_at),
            early_result=_access_skip_result(rpc_calls=rpc_calls),
        )

    return _SearchOutcome(result=cast(_SweepResultLike, result), rpc_duration_s=_elapsed_s(search_started_at))


# ---------------------------------------------------------------------------
# sweep_peer_once: FloodWait-neutral per-peer self-search primitive
# ---------------------------------------------------------------------------


async def sweep_peer_once(*args: object, **kwargs: object) -> SweepResult:
    """Search for self-authored messages in a single peer and persist them.

    Direction-agnostic: takes explicit offset_id + min_id, reports both
    min_id and max_id of the batch.
      - HotSweep (plan 03) reads max_id (forward/newest-side cursor).
      - ColdBackfill (plan 04) reads min_id (backward cursor).

    Finite throttling returns immediately with skip_reason=FLOOD_WAIT and
    flood_wait_seconds set — it does NOT sleep; the owning scheduler sets the
    per-tier *_next_retry_at. A latched throttle propagates to stop account-wide
    work and never creates per-tier retry state.

    TimeoutError (wedged RPC): treated as ACCESS_SKIP — a transient fault,
    not history-floor completion.
    """
    request = _coerce_peer_sweep_request(*args, **kwargs)

    # Step 1: entity-type-aware peer resolution from session
    rpc_calls = 1  # get_input_entity is a governed Telegram call attempt.
    resolved = await _resolve_peer_for_sweep(request)
    if isinstance(resolved, SweepResult):
        return resolved
    peer = resolved
    if peer is None:
        logger.debug("sweep_peer_once_access_skip dialog_id=%r reason=resolve_none", request.dialog_id)
        return _access_skip_result(rpc_calls=rpc_calls)

    # Step 2: issue per-peer self-search with concrete peer (not InputPeerEmpty)
    rpc_calls += 1  # SearchRequest is a governed Telegram call attempt.
    search = await _search_self_messages(request, peer, rpc_calls=rpc_calls)
    if search.early_result is not None:
        return search.early_result

    rpc_duration_s = search.rpc_duration_s
    await _pace_successful_search_request()
    search_result = cast(_SweepResultLike, search.result)
    batch = list(search_result.messages or [])

    # Step 5: genuinely empty batch from a reachable peer → history floor
    if not batch:
        logger.debug(
            "sweep_peer_once_done dialog_id=%r outcome=%s fetched=%d persisted=%d rpc_duration_s=%.3f pacing_s=%.3f",
            request.dialog_id,
            SkipReason.HISTORY_FLOOR,
            0,
            0,
            rpc_duration_s,
            _PACING.search.success_s,
        )
        return SweepResult(
            fetched_ids=[],
            persisted=0,
            min_id=None,
            max_id=None,
            skip_reason=SkipReason.HISTORY_FLOOR,
            pages_fetched=1,
            rpc_calls=rpc_calls,
            completed=True,
        )

    # Step 3-4: extract and persist via canonical pipeline
    page_rows = _extract_and_persist_sweep_messages(request, batch)
    if isinstance(page_rows, SweepResult):
        return page_rows
    extracted, genuinely_new_keys = page_rows
    genuinely_new = len(genuinely_new_keys)

    persisted = len(extracted)

    msg_ids = [m.id for m in batch]
    logger.debug(
        "sweep_peer_once_done dialog_id=%r outcome=%s fetched=%d persisted=%d rpc_duration_s=%.3f pacing_s=%.3f",
        request.dialog_id,
        SkipReason.NONE,
        len(msg_ids),
        persisted,
        rpc_duration_s,
        _PACING.search.success_s,
    )
    return SweepResult(
        fetched_ids=msg_ids,
        persisted=persisted,
        min_id=min(msg_ids) if msg_ids else None,
        max_id=max(msg_ids) if msg_ids else None,
        skip_reason=SkipReason.NONE,
        pages_fetched=1,
        rpc_calls=rpc_calls,
        extracted=len(extracted),
        genuinely_new=genuinely_new,
        genuinely_new_keys=genuinely_new_keys,
        completed=True,
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
        enroll_own_only_sync_dialog(conn, dialog_id)
        conn.execute(_INSERT_THIN_DIALOG_SQL, (dialog_id, now))


# ---------------------------------------------------------------------------
# Cursor helpers over activity_dialog_state
# ---------------------------------------------------------------------------

_DIALOG_STATE_COLUMNS = frozenset(
    {
        "hot_cursor",
        "hot_last_sync_at",
        "hot_next_retry_at",
        "hot_next_due_at",
        "hot_empty_streak",
        "hot_last_error",
        "cold_offset_id",
        "cold_status",
        "cold_next_retry_at",
        "cold_last_error",
    }
)


_DialogStateRow = tuple[
    int | None,
    int | None,
    int | None,
    int | None,
    int | None,
    str | None,
    int | None,
    str | None,
    int | None,
    int | None,
]


def _load_dialog_state(conn: sqlite3.Connection, dialog_id: int) -> dict[str, int | str | None]:
    """Return the per-tier cursor/retry fields for a peer, or {} if absent."""
    row = cast(
        _DialogStateRow | None,
        conn.execute(
            """
            SELECT hot_cursor, hot_last_sync_at, hot_next_retry_at, hot_last_error,
               hot_next_due_at, hot_empty_streak,
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
        "hot_next_due_at",
        "hot_empty_streak",
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


_ENROLLMENT_PHASE_KEY = "activity_working_set_phase"
_ENROLLMENT_CURSOR_KEY = "activity_working_set_cursor"
_ENROLLMENT_COMPLETED_AT_KEY = "activity_working_set_completed_at"
# Stable storage key for the next continuation attempt.  The persisted spelling
# is retained for databases created before the demand coordinator cutover.
_ENROLLMENT_NEXT_ATTEMPT_AT_KEY = "activity_working_set_retry_at"
_ENROLLMENT_SUPERGROUPS = "supergroups"
_ENROLLMENT_CHANNELS = "channels"


def _load_working_set_enrollment_state(conn: sqlite3.Connection) -> dict[str, str | None]:
    keys = (
        _ENROLLMENT_PHASE_KEY,
        _ENROLLMENT_CURSOR_KEY,
        _ENROLLMENT_COMPLETED_AT_KEY,
        _ENROLLMENT_NEXT_ATTEMPT_AT_KEY,
    )
    placeholders = ", ".join("?" for _ in keys)
    rows = cast(
        list[tuple[str, str | None]],
        conn.execute(
            f"SELECT key, value FROM activity_sync_state WHERE key IN ({placeholders})",
            keys,
        ).fetchall(),
    )
    return dict(rows)


def _validate_working_set_enrollment_timing(now: float, cadence_s: float) -> None:
    if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
        raise ValueError("now must be a finite non-negative timestamp")
    if (
        isinstance(cadence_s, bool)
        or not isinstance(cadence_s, (int, float))
        or not math.isfinite(cadence_s)
        or cadence_s <= 0
    ):
        raise ValueError("cadence_s must be finite and positive")


def _has_working_set_enrollment_candidate(conn: sqlite3.Connection) -> bool:
    candidate = cast(
        tuple[int] | None,
        conn.execute("SELECT 1 FROM dialogs WHERE type IN ('supergroup', 'channel') AND hidden = 0 LIMIT 1").fetchone(),
    )
    return candidate is not None


def working_set_enrollment_release_at(
    conn: sqlite3.Connection,
    *,
    now: float,
    cadence_s: float,
) -> float | None:
    """Return the restart-safe release for the next enrollment unit."""
    _validate_working_set_enrollment_timing(now, cadence_s)
    state = _load_working_set_enrollment_state(conn)
    if state.get(_ENROLLMENT_PHASE_KEY) is not None:
        return float(int(state.get(_ENROLLMENT_NEXT_ATTEMPT_AT_KEY) or 0))
    completed_at = int(state.get(_ENROLLMENT_COMPLETED_AT_KEY) or 0)
    if completed_at == 0 and not _has_working_set_enrollment_candidate(conn):
        return None
    return 0.0 if completed_at == 0 else float(completed_at) + float(cadence_s)


def _start_working_set_enrollment(conn: sqlite3.Connection) -> str:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
            (_ENROLLMENT_PHASE_KEY, _ENROLLMENT_SUPERGROUPS),
        )
        conn.execute(
            "DELETE FROM activity_sync_state WHERE key IN (?, ?)",
            (_ENROLLMENT_CURSOR_KEY, _ENROLLMENT_NEXT_ATTEMPT_AT_KEY),
        )
    return _ENROLLMENT_SUPERGROUPS


def _set_working_set_enrollment_position(
    conn: sqlite3.Connection,
    *,
    phase: str,
    cursor: int | None,
    retry_at: int | None = None,
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
            (_ENROLLMENT_PHASE_KEY, phase),
        )
        if cursor is None:
            conn.execute("DELETE FROM activity_sync_state WHERE key = ?", (_ENROLLMENT_CURSOR_KEY,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
                (_ENROLLMENT_CURSOR_KEY, str(cursor)),
            )
        if retry_at is None:
            conn.execute("DELETE FROM activity_sync_state WHERE key = ?", (_ENROLLMENT_NEXT_ATTEMPT_AT_KEY,))
        else:
            conn.execute(
                "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
                (_ENROLLMENT_NEXT_ATTEMPT_AT_KEY, str(retry_at)),
            )


def _finish_working_set_enrollment(conn: sqlite3.Connection, *, completed_at: int) -> None:
    with conn:
        conn.execute(
            "DELETE FROM activity_sync_state WHERE key IN (?, ?, ?)",
            (_ENROLLMENT_PHASE_KEY, _ENROLLMENT_CURSOR_KEY, _ENROLLMENT_NEXT_ATTEMPT_AT_KEY),
        )
        conn.execute(
            "INSERT OR REPLACE INTO activity_sync_state (key, value) VALUES (?, ?)",
            (_ENROLLMENT_COMPLETED_AT_KEY, str(completed_at)),
        )


def _next_enrollment_dialog(
    conn: sqlite3.Connection,
    *,
    dialog_type: str,
    cursor: int | None,
) -> tuple[int, int | None] | None:
    return cast(
        tuple[int, int | None] | None,
        conn.execute(
            "SELECT dialog_id, last_message_at FROM dialogs "
            "WHERE type = ? AND hidden = 0 AND (? IS NULL OR dialog_id > ?) "
            "ORDER BY dialog_id LIMIT 1",
            (dialog_type, cursor, cursor),
        ).fetchone(),
    )


@dataclass(frozen=True, slots=True)
class _WorkingSetEnrollmentPosition:
    phase: str
    cursor: int | None


@dataclass(frozen=True, slots=True)
class _ChannelEnrollmentRequest:
    client: ActivityClient
    conn: sqlite3.Connection
    source: TelegramRpcSource
    timeout_s: float
    now: int
    cursor: int | None


def _load_or_start_working_set_enrollment(conn: sqlite3.Connection) -> _WorkingSetEnrollmentPosition:
    state = _load_working_set_enrollment_state(conn)
    phase = state.get(_ENROLLMENT_PHASE_KEY)
    if phase is None:
        return _WorkingSetEnrollmentPosition(_start_working_set_enrollment(conn), None)
    cursor_value = state.get(_ENROLLMENT_CURSOR_KEY)
    cursor = int(cursor_value) if cursor_value is not None else None
    return _WorkingSetEnrollmentPosition(phase, cursor)


def _enroll_supergroup_slice(
    conn: sqlite3.Connection,
    position: _WorkingSetEnrollmentPosition,
) -> WorkingSetEnrollmentSliceResult | None:
    row = _next_enrollment_dialog(conn, dialog_type="supergroup", cursor=position.cursor)
    if row is None:
        _set_working_set_enrollment_position(conn, phase=_ENROLLMENT_CHANNELS, cursor=None)
        return None
    dialog_id, last_activity_at = row
    enroll_activity_dialog(conn, dialog_id, "supergroup", last_activity_at=last_activity_at)
    _set_working_set_enrollment_position(
        conn,
        phase=_ENROLLMENT_SUPERGROUPS,
        cursor=dialog_id,
    )
    return WorkingSetEnrollmentSliceResult(enrolled_count=1, consumed=True)


async def _enroll_channel_slice(
    request: _ChannelEnrollmentRequest,
) -> WorkingSetEnrollmentSliceResult:
    row = _next_enrollment_dialog(request.conn, dialog_type="channel", cursor=request.cursor)
    if row is None:
        _finish_working_set_enrollment(request.conn, completed_at=request.now)
        return WorkingSetEnrollmentSliceResult(completed=True, consumed=True)

    channel_id, last_activity_at = row
    try:
        with acquisition_context(AcquisitionKind.DIALOG_TRAVERSAL):
            with rpc_scope(request.source, timeout_seconds=request.timeout_s):
                resolution = await resolve_linked_chat_id(
                    request.client,
                    request.conn,
                    channel_id,
                    timeout_s=request.timeout_s,
                )
    except RpcAttemptBudgetExhaustedError:
        return WorkingSetEnrollmentSliceResult(consumed=True)

    if resolution.flood_wait_seconds is not None:
        _set_working_set_enrollment_position(
            request.conn,
            phase=_ENROLLMENT_CHANNELS,
            cursor=request.cursor,
            retry_at=request.now + resolution.flood_wait_seconds,
        )
        return WorkingSetEnrollmentSliceResult(flood_wait_seconds=resolution.flood_wait_seconds, consumed=True)
    if resolution.linked_chat_id is not None:
        enroll_activity_dialog(
            request.conn,
            resolution.linked_chat_id,
            "linked_chat",
            last_activity_at=last_activity_at,
        )
    _set_working_set_enrollment_position(
        request.conn,
        phase=_ENROLLMENT_CHANNELS,
        cursor=channel_id,
    )
    return WorkingSetEnrollmentSliceResult(enrolled_count=int(resolution.linked_chat_id is not None), consumed=True)


async def run_working_set_enrollment_slice(  # noqa: PLR0913 - explicit bounded slice dependencies
    client: ActivityClient,
    conn: sqlite3.Connection,
    *,
    source: TelegramRpcSource,
    cadence_s: float,
    timeout_s: float,
    now: int | None = None,
) -> WorkingSetEnrollmentSliceResult:
    """Enroll one dialog candidate while durably retaining scan continuation."""
    at = int(time.time() if now is None else now)
    release_at = working_set_enrollment_release_at(conn, now=float(at), cadence_s=cadence_s)
    if release_at is None or release_at > at:
        return WorkingSetEnrollmentSliceResult()

    position = _load_or_start_working_set_enrollment(conn)
    if position.phase == _ENROLLMENT_SUPERGROUPS:
        result = _enroll_supergroup_slice(conn, position)
        if result is not None:
            return result
        position = _WorkingSetEnrollmentPosition(_ENROLLMENT_CHANNELS, None)
    if position.phase != _ENROLLMENT_CHANNELS:
        raise RuntimeError(f"unknown working-set enrollment phase {position.phase!r}")
    return await _enroll_channel_slice(
        _ChannelEnrollmentRequest(
            client=client,
            conn=conn,
            source=source,
            timeout_s=timeout_s,
            now=at,
            cursor=position.cursor,
        )
    )


async def build_working_set(
    client: ActivityClient,
    conn: sqlite3.Connection,
    *,
    timeout_s: float,
) -> WorkingSetResult:
    """Build the per-peer self-search working set and enroll peers.

    Source: dialogs.type='supergroup' (megagroups) and dialogs.type='channel'
    (broadcast channels whose linked discussion group is resolved via
    resolve_linked_chat_id (post-Phase-54: dialogs-cache hot read; falls through
    to GetFullChannelRequest only when linked_chat_resolved_at IS NULL)).
    NOT entities.type='group' — that taxonomy differs (concern 4 fix).

    Returns the enrolled count and whether linked-chat resolution flooded.
    """
    # Step 1: standalone supergroups (directly self-searchable)
    supergroup_rows = cast(
        list[tuple[int, int | None]],
        conn.execute(
            "SELECT dialog_id, last_message_at FROM dialogs WHERE type = 'supergroup' AND hidden = 0"
        ).fetchall(),
    )

    # Step 2: broadcast channels (need linked_chat resolution)
    channel_rows = cast(
        list[tuple[int, int | None]],
        conn.execute("SELECT dialog_id, last_message_at FROM dialogs WHERE type = 'channel' AND hidden = 0").fetchall(),
    )

    working_set: dict[int, int | None] = {}  # peer_id → last_activity_at

    # Enroll supergroups directly
    working_set = dict(supergroup_rows)
    supergroup_ids = {dialog_id for dialog_id, _ in supergroup_rows}

    # Step 3: resolve broadcast channels to their discussion groups
    flood_wait_seconds: int | None = None
    for channel_id, channel_last_message_at in channel_rows:
        res: LinkedChatResolution = await resolve_linked_chat_id(client, conn, channel_id, timeout_s=timeout_s)

        if res.flood_wait_seconds is not None:
            logger.warning(
                "build_working_set_channel_flood channel_id=%r flood_wait_seconds=%d"
                " — halting resolution pass (Telegram throttling from GetFullChannelRequest is"
                " account-global; remaining channels stay due for next sweep cycle)",
                channel_id,
                res.flood_wait_seconds,
            )
            flood_wait_seconds = res.flood_wait_seconds
            break

        if res.linked_chat_id is not None:
            existing = working_set.get(res.linked_chat_id)
            if existing is None:
                working_set[res.linked_chat_id] = channel_last_message_at
        # else: no discussion group → drop channel (D-03)

    # Step 4-5: enroll all peers via shared helper
    for peer_id, last_activity_at in working_set.items():
        source = "supergroup" if peer_id in supergroup_ids else "linked_chat"
        enroll_activity_dialog(
            conn,
            peer_id,
            source,
            last_activity_at=last_activity_at,
        )

    return WorkingSetResult(enrolled_count=len(working_set), flood_wait_seconds=flood_wait_seconds)
