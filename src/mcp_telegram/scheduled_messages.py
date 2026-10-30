"""Storage and read-only reconciliation for Telegram scheduled messages.

Scheduled message IDs belong to a queue-local sequence.  This module keeps
their mirror separate from sent history while exposing the same message-shaped
fields and explicit lifecycle state for the query layer to project.  It never
invokes a mutating Telegram method.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, cast

from telethon.errors import RPCError  # type: ignore[import-untyped]
from telethon.tl.types import Message as TelegramMessage  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .access_lifecycle import set_access_lost
from .flood import TelegramRpcThrottled, _raise_if_latched
from .fts import stem_text
from .message_contracts import ExtractedMessage
from .messages.telegram_adapter import extract_message_row
from .observation_order import allocate_observation_order, delivered_observation_order
from .scheduled_write_fence import ScheduledMutationFence
from .sqlite_writer import SQLiteWriter
from .sync_transactions import require_write_transaction, write_transaction
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_demand import (
    AcquisitionKind,
    DemandStatus,
    DurableDemandAdapter,
    RpcAttemptBudget,
    RpcAttemptBudgetExhaustedError,
    durable_demand_context,
)
from .telegram_demand_progress import SliceOutcome, SliceOutcomeKind, WorkKey, WorkRevision
from .telegram_gateway import ScheduledHistoryClient, fetch_scheduled_history_snapshot
from .telegram_rpc_consumers import DemandKind
from .telegram_rpc_scheduler import (
    TelegramRpcAdmissionDeferred,
    TelegramRpcMethodRecoveryPending,
    TelegramRpcSource,
    rpc_attempt_budget,
    rpc_scope,
)

logger = logging.getLogger(__name__)


_SCHEDULED_SYNC_KEY = "account"
_DELETE_SCHEDULED_FTS_SQL = "DELETE FROM scheduled_messages_fts WHERE dialog_id=? AND message_id=?"
_INSERT_SCHEDULED_FTS_SQL = "INSERT INTO scheduled_messages_fts(dialog_id, message_id, stemmed_text) VALUES (?, ?, ?)"


@dataclass(frozen=True, slots=True)
class ScheduledReconciliationPolicy:
    activity_rpc_timeout_seconds: float
    max_dialogs_per_slice: int = 8


def _as_int(value: object) -> int:
    return int(cast(int | str, value))


def _mark_scheduled_dialog_dirty(
    conn: sqlite3.Connection,
    dialog_id: int,
    now: int,
    *,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> None:
    """Advance the event fence and request evidence only while coverage is unknown."""
    if mutation_fence is not None:
        coverage = cast(
            tuple[str] | None,
            conn.execute(
                "SELECT coverage_state FROM scheduled_reconciliation_state WHERE dialog_id=?", (dialog_id,)
            ).fetchone(),
        )
        if coverage is None or coverage[0] in ("inaccessible", "unsupported"):
            _invalidate_scheduled_target(mutation_fence, account_id, dialog_id)
    conn.execute(
        """
        INSERT INTO scheduled_reconciliation_state (
            dialog_id, repair_due_at, discovery_due_at, dirty_since,
            dirty_generation, updated_at, coverage_state, coverage_reason
        ) VALUES (?, NULL, ?, ?, 1, ?, 'unknown', 'baseline_unknown')
        ON CONFLICT(dialog_id) DO UPDATE SET
            discovery_due_at = CASE WHEN coverage_state IN ('unknown','inaccessible','unsupported')
                THEN COALESCE(discovery_due_at, excluded.discovery_due_at) ELSE discovery_due_at END,
            coverage_state = CASE WHEN coverage_state IN ('inaccessible','unsupported')
                THEN 'unknown' ELSE coverage_state END,
            coverage_reason = CASE WHEN coverage_state IN ('inaccessible','unsupported')
                THEN 'scheduled_event_after_terminal_coverage' ELSE coverage_reason END,
            dirty_since = COALESCE(dirty_since, excluded.dirty_since),
            dirty_generation = dirty_generation + 1,
            updated_at = excluded.updated_at
        """,
        (dialog_id, now, now, now),
    )


def scheduled_dialog_has_due_work(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Whether accepted scheduled evidence still needs bounded acquisition."""
    row = cast(
        tuple[object] | None,
        conn.execute(
            "SELECT 1 FROM scheduled_reconciliation_state WHERE dialog_id=? "
            "AND (repair_due_at IS NOT NULL OR discovery_due_at IS NOT NULL)",
            (dialog_id,),
        ).fetchone(),
    )
    return row is not None


class _ScheduledSnapshotMessage(Protocol):
    id: int


def _unix_timestamp(value: object | None) -> int | None:
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, int):
        return value
    return None


def scheduled_dialog_id(peer: object | None) -> int | None:
    """Return a canonical dialog id from a raw Telegram Peer."""
    if peer is None:
        return None
    try:
        return int(cast(int, get_peer_id(peer)))
    except TypeError, ValueError:
        # Keep raw-update tests and older Telethon-compatible doubles useful.
        channel_id = cast(object | None, getattr(peer, "channel_id", None))
        if channel_id is not None:
            return -1000000000000 - _as_int(channel_id)
        chat_id = cast(object | None, getattr(peer, "chat_id", None))
        if chat_id is not None:
            return -_as_int(chat_id)
        user_id = cast(object | None, getattr(peer, "user_id", None))
        if user_id is not None:
            return _as_int(user_id)
        return None


def scheduled_message_dialog_id(message: object) -> int | None:
    """Extract the destination dialog id from a scheduled Message."""
    return scheduled_dialog_id(getattr(message, "peer_id", None))


def _composition_flag(source: object, name: str) -> bool | None:
    value = getattr(source, name, None)
    if isinstance(source, TelegramMessage):
        return bool(value)
    return value if type(value) is bool else None


def _invalidate_scheduled_target(
    mutation_fence: ScheduledMutationFence | None,
    account_id: int | None,
    dialog_id: int,
    message_id: int | None = None,
) -> None:
    if mutation_fence is None:
        return
    if account_id is None:
        raise RuntimeError("scheduled mutation fence requires the verified account")
    if message_id is None:
        mutation_fence.invalidate_dialog(account_id, dialog_id)
    else:
        mutation_fence.invalidate_message(account_id, dialog_id, message_id)


def _scheduled_params(dialog_id: int, extracted: ExtractedMessage, source: object, now: int) -> dict[str, object]:
    message = extracted.message
    scheduled_at = _unix_timestamp(getattr(extracted, "scheduled_at", None))
    if scheduled_at is None:
        # Telegram exposes the schedule date as Message.date.  The extraction
        # helper already normalises it to sent_at for ordinary Message objects.
        scheduled_at = message.sent_at or None
    return {
        "dialog_id": dialog_id,
        "message_id": message.message_id,
        "scheduled_at": scheduled_at,
        "text": message.text,
        "sender_id": message.sender_id,
        "sender_first_name": message.sender_first_name,
        "media_kind": message.media_kind,
        "media_payload": message.media_payload,
        "formatting_entities": message.formatting_entities,
        "service_action": message.service_action,
        "reply_to_msg_id": message.reply_to_msg_id,
        "forum_topic_id": message.forum_topic_id,
        "edit_date": message.edit_date,
        "grouped_id": message.grouped_id,
        "reply_to_peer_id": message.reply_to_peer_id,
        "out": message.out,
        "is_service": message.is_service,
        "post_author": message.post_author,
        "schedule_repeat_period": getattr(source, "schedule_repeat_period", None),
        "has_rich_message": _composition_flag(source, "rich_message"),
        "invert_media": _composition_flag(source, "invert_media"),
        "video_processing_pending": _composition_flag(source, "video_processing_pending"),
        "updated_at": now,
    }


_UPSERT_SCHEDULED_SQL = """
INSERT INTO scheduled_messages (
    dialog_id, message_id, scheduled_at, text, sender_id, sender_first_name,
    media_kind, media_payload, formatting_entities, service_action, reply_to_msg_id,
    forum_topic_id, edit_date, grouped_id,
    reply_to_peer_id, out, is_service, post_author, schedule_repeat_period,
    message_state, visibility, unpublished, unseen, publication_hint_message_id,
    published_message_id, publication_verified_at, published_at, deleted_at,
    first_seen_at, updated_at, has_rich_message, invert_media, video_processing_pending
) VALUES (
    :dialog_id, :message_id, :scheduled_at, :text, :sender_id, :sender_first_name,
    :media_kind, :media_payload, :formatting_entities, :service_action, :reply_to_msg_id,
    :forum_topic_id, :edit_date, :grouped_id,
    :reply_to_peer_id, :out, :is_service, :post_author, :schedule_repeat_period,
    'scheduled', 'author_only', 1, 1, NULL, NULL, NULL, NULL, NULL,
    :updated_at, :updated_at, :has_rich_message, :invert_media, :video_processing_pending
)
ON CONFLICT(dialog_id, message_id) DO UPDATE SET
    scheduled_at = excluded.scheduled_at,
    text = excluded.text,
    sender_id = excluded.sender_id,
    sender_first_name = excluded.sender_first_name,
    media_kind = excluded.media_kind,
    media_payload = excluded.media_payload,
    formatting_entities = excluded.formatting_entities,
    service_action = excluded.service_action,
    reply_to_msg_id = excluded.reply_to_msg_id,
    forum_topic_id = excluded.forum_topic_id,
    edit_date = excluded.edit_date,
    grouped_id = excluded.grouped_id,
    reply_to_peer_id = excluded.reply_to_peer_id,
    out = excluded.out,
    is_service = excluded.is_service,
    post_author = excluded.post_author,
    schedule_repeat_period = excluded.schedule_repeat_period,
    message_state = 'scheduled',
    visibility = 'author_only',
    unpublished = 1,
    unseen = 1,
    publication_hint_message_id = NULL,
    published_message_id = NULL,
    publication_verified_at = NULL,
    published_at = NULL,
    deleted_at = NULL,
    updated_at = excluded.updated_at,
    has_rich_message = excluded.has_rich_message,
    invert_media = excluded.invert_media,
    video_processing_pending = excluded.video_processing_pending
WHERE scheduled_messages.message_state != 'published'
"""


def _scheduled_observation_current(
    conn: sqlite3.Connection, dialog_id: int, message_id: int, observation_order: int
) -> bool:
    row = cast(
        tuple[int, int, str | None],
        conn.execute(
            "SELECT COALESCE((SELECT observation_order FROM scheduled_messages "
            "WHERE dialog_id=? AND message_id=?),0), "
            "COALESCE((SELECT snapshot_observation_order FROM scheduled_reconciliation_state WHERE dialog_id=?),0), "
            "(SELECT message_state FROM scheduled_messages WHERE dialog_id=? AND message_id=?)",
            (dialog_id, message_id, dialog_id, dialog_id, message_id),
        ).fetchone(),
    )
    return row[2] != "published" and observation_order > max(row[0], row[1])


def upsert_scheduled_message(  # noqa: PLR0913 - shared queue projection with retained acquisition order
    conn: sqlite3.Connection,
    dialog_id: int,
    message: object,
    *,
    now: int | None = None,
    mark_dirty: bool = True,
    observation_order: int | None = None,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> None:
    """Insert or replace one scheduled snapshot without touching sent history."""
    extracted = extract_message_row(dialog_id, message)
    require_write_transaction(conn)
    timestamp = int(time.time()) if now is None else int(now)
    scheduled_at = _unix_timestamp(getattr(extracted, "scheduled_at", None)) or extracted.message.sent_at
    if scheduled_at is None:
        return
    order = delivered_observation_order(conn) if observation_order is None else observation_order
    if not _scheduled_observation_current(conn, dialog_id, extracted.message.message_id, order):
        return
    _invalidate_scheduled_target(mutation_fence, account_id, dialog_id, extracted.message.message_id)
    cursor = conn.execute(_UPSERT_SCHEDULED_SQL, _scheduled_params(dialog_id, extracted, message, timestamp))
    if not cursor.rowcount:
        return
    conn.execute(
        "UPDATE scheduled_messages SET observation_order=? WHERE dialog_id=? AND message_id=?",
        (order, dialog_id, extracted.message.message_id),
    )
    conn.execute(_DELETE_SCHEDULED_FTS_SQL, (dialog_id, extracted.message.message_id))
    conn.execute(
        _INSERT_SCHEDULED_FTS_SQL,
        (dialog_id, extracted.message.message_id, stem_text(extracted.message.text)),
    )
    if mark_dirty:
        _mark_scheduled_dialog_dirty(conn, dialog_id, timestamp, mutation_fence=mutation_fence, account_id=account_id)


_INSERT_SCHEDULED_TOMBSTONE_SQL = """
INSERT OR IGNORE INTO scheduled_messages (
    dialog_id, message_id, message_state, visibility, unpublished, unseen,
    first_seen_at, updated_at, deleted_at, publication_hint_message_id
) VALUES (?, ?, ?, 'author_only', 1, 1, ?, ?, ?, ?)
"""


def mark_scheduled_messages_removed(  # noqa: PLR0913 - removal hints share the supplied update order
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
    sent_message_ids: Sequence[int] | None = None,
    *,
    now: int | None = None,
    observation_order: int | None = None,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> None:
    """Retain queue-removal evidence and store publication hints as unverified.

    Telegram's parallel ``messages``/``sent_messages`` vectors identify likely
    publication targets, but the mapping is not trusted until the corresponding
    normal message arrives with ``from_scheduled``.
    """
    timestamp = int(time.time()) if now is None else int(now)
    hints = list(sent_message_ids or ())
    require_write_transaction(conn)
    order = delivered_observation_order(conn) if observation_order is None else observation_order
    if _apply_scheduled_removals(
        conn,
        dialog_id,
        message_ids,
        hints,
        timestamp=timestamp,
        order=order,
        mutation_fence=mutation_fence,
        account_id=account_id,
    ):
        _mark_scheduled_dialog_dirty(conn, dialog_id, timestamp, mutation_fence=mutation_fence, account_id=account_id)


def _apply_scheduled_removals(  # noqa: PLR0913 - one update shares timestamp and order across queue IDs
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
    hints: Sequence[int],
    *,
    timestamp: int,
    order: int,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> bool:
    changed = False
    for index, raw_message_id in enumerate(message_ids):
        message_id = int(raw_message_id)
        if not _scheduled_observation_current(conn, dialog_id, message_id, order):
            continue
        _invalidate_scheduled_target(mutation_fence, account_id, dialog_id, message_id)
        changed = True
        hint = int(hints[index]) if index < len(hints) else None
        state = "unknown_missing" if hint is not None else "cancelled"
        conn.execute(
            _INSERT_SCHEDULED_TOMBSTONE_SQL,
            (dialog_id, message_id, state, timestamp, timestamp, timestamp if hint is None else None, hint),
        )
        if hint is None:
            conn.execute(
                "UPDATE scheduled_messages SET message_state='cancelled', unpublished=1, "
                "unseen=1, deleted_at=?, updated_at=? WHERE dialog_id=? AND message_id=? "
                "AND message_state != 'published'",
                (timestamp, timestamp, dialog_id, message_id),
            )
        else:
            conn.execute(
                "UPDATE scheduled_messages SET message_state='unknown_missing', unpublished=1, "
                "unseen=1, publication_hint_message_id=?, deleted_at=NULL, updated_at=? "
                "WHERE dialog_id=? AND message_id=? AND message_state != 'published'",
                (hint, timestamp, dialog_id, message_id),
            )
            _reconcile_scheduled_publication(
                conn, dialog_id, hint, timestamp, mutation_fence=mutation_fence, account_id=account_id
            )
        conn.execute(
            "UPDATE scheduled_messages SET observation_order=? WHERE dialog_id=? AND message_id=?",
            (order, dialog_id, message_id),
        )
        # A publication hint is unresolved evidence, not a terminal removal.
        # Keep retained text searchable until publication is independently confirmed.
        if hint is None:
            conn.execute(_DELETE_SCHEDULED_FTS_SQL, (dialog_id, message_id))
    return changed


def verify_scheduled_publication(  # noqa: PLR0913 - explicit canonical invalidation dependency
    conn: sqlite3.Connection,
    dialog_id: int,
    published_message_id: int,
    *,
    published_at: int | None = None,
    now: int | None = None,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> int:
    """Retain verified ``from_scheduled`` evidence, even before its queue hint."""
    require_write_transaction(conn)
    timestamp = int(time.time()) if now is None else int(now)
    conn.execute(
        "INSERT INTO scheduled_publication_evidence(dialog_id, published_message_id, published_at, verified_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(dialog_id, published_message_id) DO UPDATE SET "
        "published_at=COALESCE(scheduled_publication_evidence.published_at, excluded.published_at)",
        (dialog_id, int(published_message_id), published_at, timestamp),
    )
    return _reconcile_scheduled_publication(
        conn, dialog_id, int(published_message_id), timestamp, mutation_fence=mutation_fence, account_id=account_id
    )


def _reconcile_scheduled_publication(  # noqa: PLR0913 - explicit canonical invalidation dependency
    conn: sqlite3.Connection,
    dialog_id: int,
    published_message_id: int,
    timestamp: int,
    *,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> int:
    evidence = cast(
        tuple[int | None, int] | None,
        conn.execute(
            "SELECT published_at, verified_at FROM scheduled_publication_evidence "
            "WHERE dialog_id=? AND published_message_id=?",
            (dialog_id, published_message_id),
        ).fetchone(),
    )
    if evidence is None:
        return 0
    rows = cast(
        list[tuple[object]],
        conn.execute(
            "SELECT message_id FROM scheduled_messages WHERE dialog_id=? "
            "AND publication_hint_message_id=? AND message_state='unknown_missing'",
            (dialog_id, int(published_message_id)),
        ).fetchall(),
    )
    scheduled_ids = [_as_int(row[0]) for row in rows]
    for message_id in scheduled_ids:
        _invalidate_scheduled_target(mutation_fence, account_id, dialog_id, message_id)
    cursor = conn.execute(
        "UPDATE scheduled_messages SET message_state='published', visibility='chat_visible', unpublished=0, "
        "unseen=0, published_message_id=?, publication_verified_at=?, published_at=?, updated_at=? "
        "WHERE dialog_id=? "
        "AND publication_hint_message_id=? AND message_state='unknown_missing'",
        (published_message_id, evidence[1], evidence[0], timestamp, dialog_id, published_message_id),
    )
    if scheduled_ids:
        conn.executemany(_DELETE_SCHEDULED_FTS_SQL, ((dialog_id, message_id) for message_id in scheduled_ids))
        _mark_scheduled_dialog_dirty(conn, dialog_id, timestamp, mutation_fence=mutation_fence, account_id=account_id)
    return cursor.rowcount


def mark_missing_from_snapshot(  # noqa: PLR0913 - explicit canonical invalidation dependency
    conn: sqlite3.Connection,
    dialog_id: int,
    message_ids: Sequence[int],
    *,
    now: int | None = None,
    observation_order: int | None = None,
    mutation_fence: ScheduledMutationFence | None = None,
    account_id: int | None = None,
) -> int:
    """Mark older active rows absent from an authoritative snapshot as non-visible."""
    require_write_transaction(conn)
    timestamp = int(time.time()) if now is None else int(now)
    order = delivered_observation_order(conn) if observation_order is None else observation_order
    accepted = [
        message_id for message_id in message_ids if _scheduled_observation_current(conn, dialog_id, message_id, order)
    ]
    if not accepted:
        return 0
    for message_id in accepted:
        _invalidate_scheduled_target(mutation_fence, account_id, dialog_id, message_id)
    placeholders = ",".join("?" for _ in accepted)
    params: tuple[object, ...] = (timestamp, order, dialog_id, *[int(item) for item in accepted])
    cursor = conn.execute(
        "UPDATE scheduled_messages SET message_state='unknown_missing', unpublished=1, unseen=1, "
        "updated_at=?, observation_order=? WHERE dialog_id=? AND message_state='scheduled' "
        f"AND message_id IN ({placeholders})",
        params,
    )
    # Snapshot absence is unresolved; the row and its FTS entry remain useful
    # until Telegram supplies terminal cancellation/publication evidence.
    return cursor.rowcount


def _record_snapshot_success(conn: sqlite3.Connection, now: int) -> None:
    require_write_transaction(conn)
    conn.execute(
        "UPDATE scheduled_sync_state SET next_retry_at=NULL, last_snapshot_at=?, last_error=NULL WHERE key=?",
        (now, _SCHEDULED_SYNC_KEY),
    )


def _retry_at(conn: sqlite3.Connection) -> int | None:
    row = cast(
        tuple[object] | None,
        conn.execute("SELECT next_retry_at FROM scheduled_sync_state WHERE key=?", (_SCHEDULED_SYNC_KEY,)).fetchone(),
    )
    return _as_int(row[0]) if row and row[0] is not None else None


def _snapshot_message_ids(snapshot: Sequence[object]) -> set[int]:
    return {int(cast(_ScheduledSnapshotMessage, message).id) for message in snapshot if getattr(message, "id", None)}


def _missing_scheduled_ids(conn: sqlite3.Connection, dialog_id: int, snapshot_ids: set[int]) -> list[int]:
    active_rows = cast(
        list[tuple[object]],
        conn.execute(
            "SELECT message_id FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled'",
            (dialog_id,),
        ).fetchall(),
    )
    return [_as_int(row[0]) for row in active_rows if _as_int(row[0]) not in snapshot_ids]


def _upsert_snapshot_messages(
    conn: sqlite3.Connection, dialog_id: int, snapshot: Sequence[object], now: int, observation_order: int
) -> None:
    for message in snapshot:
        if getattr(message, "id", None):
            upsert_scheduled_message(
                conn, dialog_id, message, now=now, mark_dirty=False, observation_order=observation_order
            )


class ScheduledMessageReconciler:
    """Durable per-dialog scheduled-history reconciliation worker."""

    def __init__(  # noqa: PLR0913 - explicit lifecycle and writer dependencies
        self,
        client: ScheduledHistoryClient,
        conn: sqlite3.Connection,
        shutdown_event: asyncio.Event,
        *,
        policy: ScheduledReconciliationPolicy,
        writer: SQLiteWriter,
        mutation_fence: ScheduledMutationFence | None = None,
    ) -> None:
        self._client = client
        self._conn = conn
        self._writer = writer
        self._shutdown_event = shutdown_event
        self._verified_account_id: int | None = None
        self._policy = policy
        self._mutation_fence = mutation_fence

    def bind_account_id(self, account_id: int) -> None:
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        if self._verified_account_id not in (None, account_id):
            raise RuntimeError("scheduled reconciliation account does not match verified startup identity")
        self._verified_account_id = account_id

    def _candidate_ids(self) -> set[int]:
        """Return every known directory peer, plus self and retained queue peers."""
        candidate_ids = {
            _as_int(row[0])
            for row in cast(list[tuple[object]], self._conn.execute("SELECT dialog_id FROM dialogs").fetchall())
        }
        candidate_ids.update(
            _as_int(row[0])
            for row in cast(
                list[tuple[object]],
                self._conn.execute(
                    "SELECT DISTINCT dialog_id FROM scheduled_messages WHERE message_state IN ('scheduled','unknown_missing')"
                ).fetchall(),
            )
        )
        if self._verified_account_id is not None:
            candidate_ids.add(self._verified_account_id)
        return candidate_ids

    def _directory_generation(self) -> int | None:
        row = cast(
            tuple[object] | None,
            self._conn.execute("SELECT generation FROM dialog_directory_publication WHERE singleton=1").fetchone(),
        )
        return None if row is None or row[0] is None else _as_int(row[0])

    def _unseeded_candidate_ids(self) -> set[int]:
        candidate_ids = self._candidate_ids()
        if not candidate_ids:
            return set()
        seeded_ids = {
            _as_int(row[0])
            for row in cast(
                list[tuple[object]],
                self._conn.execute("SELECT dialog_id FROM scheduled_reconciliation_state").fetchall(),
            )
        }
        return candidate_ids - seeded_ids

    @staticmethod
    def _seed_revision(candidate_ids: set[int], directory_generation: int | None) -> str:
        return f"{directory_generation}:{','.join(str(dialog_id) for dialog_id in sorted(candidate_ids))}"

    async def _seed_selected_candidates(self, expected_revision: str, account_id: int) -> str | None:
        """Persist only new directory peers and bind this enumeration to the verified account."""
        now = int(time.time())
        candidate_ids = self._unseeded_candidate_ids()
        directory_generation = self._directory_generation()
        if self._seed_revision(candidate_ids, directory_generation) != expected_revision:
            return None
        if self._verified_account_id not in (None, account_id):
            raise RuntimeError("scheduled reconciliation account does not match verified startup identity")

        def persist(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                state = cast(
                    tuple[object, object] | None,
                    conn.execute(
                        "SELECT account_id,directory_generation FROM scheduled_sync_state WHERE key='account'"
                    ).fetchone(),
                )
                stored_account = None if state is None else state[0]
                if stored_account is not None and account_id is not None and _as_int(stored_account) != account_id:
                    raise RuntimeError("scheduled mirror is bound to a different Telegram account")
                cursor = conn.execute(
                    "UPDATE scheduled_sync_state SET account_id=COALESCE(account_id,?), "
                    "directory_generation=? WHERE key='account' AND (account_id IS NULL OR account_id=?)",
                    (account_id, directory_generation, account_id),
                )
                inserted = cursor.rowcount > 0
                for dialog_id in sorted(candidate_ids):
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO scheduled_reconciliation_state(
                            dialog_id, repair_due_at, discovery_due_at, updated_at,
                            coverage_state,coverage_reason,directory_generation
                        ) VALUES (?, NULL, ?, ?, 'unknown','baseline_unknown',?)
                        """,
                        (dialog_id, now, now, directory_generation),
                    )
                    inserted = inserted or cursor.rowcount > 0
            return inserted

        accepted = await self._writer.run(persist)
        if not accepted:
            return None
        return f"seeded:{expected_revision}"

    async def _discover_eligibility(self, dialog_id: int) -> bool:
        """Scheduled queues are eligible for every canonical or retained peer."""
        return dialog_id in self._candidate_ids()

    async def _fetch_scheduled_snapshot(self, dialog_id: int) -> list[object]:
        """Fetch one scheduled queue snapshot through the Telegram gateway."""
        with rpc_scope(
            TelegramRpcSource.SCHEDULED_MESSAGES,
            acquisition_kind=AcquisitionKind.SCHEDULED_MESSAGES_SNAPSHOT,
        ):
            return await fetch_scheduled_history_snapshot(self._client, dialog_id)

    def _due_rows(self, now: int, demand_kind: DemandKind | None = None) -> list[tuple[int, int, bool]]:
        if demand_kind is None:
            due_predicate = "repair_due_at <= :now OR (repair_due_at IS NULL AND discovery_due_at <= :now)"
            discovery_expression = "repair_due_at IS NULL AND discovery_due_at <= :now"
        elif demand_kind is DemandKind.SCHEDULED_REPAIR:
            due_predicate = "repair_due_at <= :now"
            discovery_expression = "0"
        elif demand_kind is DemandKind.SCHEDULED_DISCOVERY:
            due_predicate = "repair_due_at IS NULL AND discovery_due_at <= :now"
            discovery_expression = "1"
        else:
            raise ValueError("demand_kind must be scheduled repair or discovery")
        rows = cast(
            list[tuple[object, object, object]],
            self._conn.execute(
                f"""
                SELECT dialog_id, dirty_generation, {discovery_expression}
                FROM scheduled_reconciliation_state
                WHERE {due_predicate}
                ORDER BY CASE WHEN dirty_since IS NULL THEN 1 ELSE 0 END,
                         CASE
                             WHEN dirty_since IS NULL
                             THEN MIN(COALESCE(repair_due_at, discovery_due_at), discovery_due_at)
                             ELSE dirty_since
                         END,
                         MIN(COALESCE(repair_due_at, discovery_due_at), discovery_due_at),
                         dialog_id
                LIMIT :limit
                """,
                {"now": now, "limit": self._policy.max_dialogs_per_slice},
            ).fetchall(),
        )
        return [(_as_int(row[0]), _as_int(row[1]), bool(row[2])) for row in rows]

    async def _apply_snapshot(  # noqa: PLR0913 - one authoritative RPC boundary fences the full set
        self,
        dialog_id: int,
        generation: int,
        snapshot: list[object],
        *,
        discovery: bool,
        now: int,
        observation_order: int,
    ) -> tuple[int, str] | None:
        def persist(conn: sqlite3.Connection) -> tuple[int, str] | None:
            # Acquire the write lock before checking the generation.  Otherwise a
            # concurrent event can dirty the dialog after the check and be erased
            # by this older snapshot.
            with write_transaction(conn):
                current = cast(
                    tuple[int, int] | None,
                    conn.execute(
                        "SELECT dirty_generation,COALESCE(snapshot_observation_order,0) "
                        "FROM scheduled_reconciliation_state WHERE dialog_id=?",
                        (dialog_id,),
                    ).fetchone(),
                )
                if current is None or _as_int(current[0]) != generation or observation_order <= current[1]:
                    return None
                _invalidate_scheduled_target(self._mutation_fence, self._verified_account_id, dialog_id)
                snapshot_ids = _snapshot_message_ids(snapshot)
                missing_ids = _missing_scheduled_ids(conn, dialog_id, snapshot_ids)
                _upsert_snapshot_messages(conn, dialog_id, snapshot, now, observation_order)
                changed = mark_missing_from_snapshot(
                    conn, dialog_id, missing_ids, now=now, observation_order=observation_order
                )
                conn.execute(
                    """
                    UPDATE scheduled_reconciliation_state
                    SET repair_due_at=NULL,
                        discovery_due_at=NULL,
                        dirty_since=NULL,
                        updated_at=?,
                        snapshot_observation_order=?,
                        baseline_observed_at=?,coverage_state='complete',coverage_reason=NULL,
                        directory_generation=(SELECT generation FROM dialog_directory_publication WHERE singleton=1)
                    WHERE dialog_id=? AND dirty_generation=?
                    """,
                    (now, observation_order, now, dialog_id, generation),
                )
                _record_snapshot_success(conn, now)
                return changed, f"{generation}|{'discovery' if discovery else 'repair'}|{observation_order}"

        return await self._writer.run(persist)

    async def _finish_excluded_discovery(self, dialog_id: int, now: int) -> bool:
        def persist(conn: sqlite3.Connection) -> bool:
            with write_transaction(conn):
                active = cast(
                    tuple[object] | None,
                    conn.execute(
                        "SELECT 1 FROM scheduled_messages WHERE dialog_id=? AND message_state='scheduled' LIMIT 1",
                        (dialog_id,),
                    ).fetchone(),
                )
                if active is not None:
                    return False
                _invalidate_scheduled_target(self._mutation_fence, self._verified_account_id, dialog_id)
                conn.execute(
                    "UPDATE scheduled_reconciliation_state SET repair_due_at=NULL, discovery_due_at=NULL, "
                    "coverage_state='unsupported',coverage_reason='peer_not_in_directory',updated_at=? WHERE dialog_id=?",
                    (now, dialog_id),
                )
                return True

        return await self._writer.run(persist)

    async def _prepare_discovery(self, dialog_id: int, now: int) -> tuple[int, bool] | None:
        try:
            eligible = await self._discover_eligibility(dialog_id)
        except TelegramRpcAdmissionDeferred:
            raise
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            return 0, True
        if eligible is None:
            return 0, False
        if not eligible and await self._finish_excluded_discovery(dialog_id, now):
            return 0, False
        return None

    async def _fetch_snapshot_or_stop(
        self, dialog_id: int, discovery: bool, now: int
    ) -> tuple[list[object] | None, bool]:
        del discovery, now
        try:
            return await self._fetch_scheduled_snapshot(dialog_id), False
        except TelegramRpcAdmissionDeferred:
            raise
        except TelegramRpcThrottled as exc:
            _raise_if_latched(exc)
            logger.warning(
                "scheduled_reconcile_flood_wait dialog_id=%d — stopping account pass",
                dialog_id,
            )
            raise
        except RPCError as exc:
            logger.warning("scheduled_reconcile_rpc_error dialog_id=%d error_type=%s", dialog_id, type(exc).__name__)
            if isinstance(exc, ACCESS_LOST_ERRORS):
                await self._mark_inaccessible(dialog_id, now=int(time.time()), reason=type(exc).__name__)
            return None, False
        except ValueError as exc:
            logger.warning("scheduled_reconcile_incomplete_snapshot dialog_id=%d error=%s", dialog_id, exc)
            return None, False

    async def _record_snapshot_failure(self, dialog_id: int, now: int, reason: str) -> None:
        def persist(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                conn.execute(
                    "UPDATE scheduled_reconciliation_state SET coverage_reason=?,updated_at=? "
                    "WHERE dialog_id=? AND coverage_state NOT IN ('inaccessible','unsupported')",
                    (reason, now, dialog_id),
                )

        await self._writer.run(persist)

    async def _mark_inaccessible(self, dialog_id: int, *, now: int, reason: str) -> None:
        def persist(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                _invalidate_scheduled_target(self._mutation_fence, self._verified_account_id, dialog_id)
                set_access_lost(conn, dialog_id, now, reason=reason)
                conn.execute(
                    "UPDATE scheduled_reconciliation_state SET repair_due_at=NULL,discovery_due_at=NULL,"
                    "coverage_state='inaccessible',coverage_reason=?,updated_at=? WHERE dialog_id=?",
                    (reason, now, dialog_id),
                )

        await self._writer.run(persist)

    async def invalidate_continuity(self, dialog_id: int | None, reason: str = "difference_too_long") -> None:
        """Schedule bounded snapshots only after Telegram reports an unrecoverable update gap."""
        now = int(time.time())
        dialog_ids = self._candidate_ids() if dialog_id is None else {dialog_id}

        def persist(conn: sqlite3.Connection) -> None:
            with write_transaction(conn):
                if dialog_id is None and self._mutation_fence is not None:
                    if self._verified_account_id is None:
                        raise RuntimeError("scheduled mutation fence requires the verified account")
                    self._mutation_fence.invalidate_account(self._verified_account_id)
                elif dialog_id is not None:
                    _invalidate_scheduled_target(self._mutation_fence, self._verified_account_id, dialog_id)
                for candidate in dialog_ids:
                    conn.execute(
                        """
                        INSERT INTO scheduled_reconciliation_state(
                            dialog_id,discovery_due_at,dirty_since,dirty_generation,updated_at,
                            coverage_state,coverage_reason
                        ) VALUES (?, ?, ?, 1, ?, 'unknown', ?)
                        ON CONFLICT(dialog_id) DO UPDATE SET
                            discovery_due_at=CASE WHEN coverage_state='complete' OR discovery_due_at IS NULL
                                THEN excluded.discovery_due_at ELSE discovery_due_at END,
                            dirty_since=CASE WHEN coverage_state='complete' OR discovery_due_at IS NULL
                                THEN excluded.dirty_since ELSE dirty_since END,
                            dirty_generation=dirty_generation+1,
                            coverage_state='unknown',coverage_reason=excluded.coverage_reason,
                            updated_at=excluded.updated_at
                        """,
                        (candidate, now, now, now, reason),
                    )

        await self._writer.run(persist)

    async def _process_due_dialog(
        self, dialog_id: int, generation: int, discovery: bool, now: int
    ) -> tuple[int, bool, str | None]:
        if discovery:
            outcome = await self._prepare_discovery(dialog_id, now)
            if outcome is not None:
                return outcome[0], outcome[1], None
        observation_order = await self._writer.run(allocate_observation_order)
        snapshot, stopped = await self._fetch_snapshot_or_stop(dialog_id, discovery, now)
        if snapshot is None:
            await self._record_snapshot_failure(dialog_id, now, "rpc_error")
            return 0, stopped, None
        receipt = await self._apply_snapshot(
            dialog_id, generation, snapshot, discovery=discovery, now=now, observation_order=observation_order
        )
        return (0 if receipt is None else receipt[0]), False, None if receipt is None else receipt[1]

    async def run_demand_slice(self, demand_kind: DemandKind, dialog_id: int | None = None) -> int:
        return (await self._run_demand_slice_with_receipt(demand_kind, dialog_id))[0]

    async def _run_demand_slice_with_receipt(
        self, demand_kind: DemandKind, dialog_id: int | None = None, *, expected_revision: str | None = None
    ) -> tuple[int, str | None]:
        """Process one bounded slice for one scheduled demand contract."""
        if demand_kind not in (DemandKind.SCHEDULED_REPAIR, DemandKind.SCHEDULED_DISCOVERY):
            raise ValueError("demand_kind must be scheduled repair or discovery")
        if dialog_id is None:
            return await self._run_slice(demand_kind, dialog_id=dialog_id), None
        return await self._run_selected_demand(demand_kind, dialog_id, expected_revision)

    async def _run_selected_demand(
        self, demand_kind: DemandKind, dialog_id: int, expected_revision: str | None
    ) -> tuple[int, str | None]:
        row = cast(
            tuple[object, object, object, object] | None,
            self._conn.execute(
                "SELECT dialog_id,dirty_generation,repair_due_at,discovery_due_at "
                "FROM scheduled_reconciliation_state WHERE dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        if row is None:
            return 0, None
        generation = _as_int(row[1])
        now = int(time.time())
        account_retry = _retry_at(self._conn)
        if account_retry is not None and account_retry > now:
            return 0, None
        repair_due, discovery_due = row[2], row[3]
        if demand_kind is DemandKind.SCHEDULED_REPAIR:
            due = repair_due
        else:
            due = discovery_due if repair_due is None else None
        if due is None or _as_int(due) > now:
            return 0, None
        if not self._selected_revision_is_current(dialog_id, generation, expected_revision):
            return 0, None
        discovery = demand_kind is DemandKind.SCHEDULED_DISCOVERY
        changed, _stopped, receipt = await self._process_due_dialog(dialog_id, generation, discovery, now)
        return changed, receipt

    def _selected_revision_is_current(self, dialog_id: int, generation: int, expected_revision: str | None) -> bool:
        if expected_revision is None:
            return True
        selected_generation, _, selected_order = expected_revision.split("|", 2)
        current_order = cast(
            tuple[object, object] | None,
            self._conn.execute(
                "SELECT dirty_generation,COALESCE(snapshot_observation_order,0) "
                "FROM scheduled_reconciliation_state WHERE dialog_id=?",
                (dialog_id,),
            ).fetchone(),
        )
        return (
            current_order is not None
            and _as_int(current_order[0]) == generation
            and str(generation) == selected_generation
            and str(current_order[1]) == selected_order
        )

    async def _process_due_row(
        self,
        dialog_id: int,
        generation: int,
        discovery: bool,
        now: int,
        demand_kind: DemandKind | None,
    ) -> tuple[int, bool]:
        if demand_kind is None:
            row_demand_kind = DemandKind.SCHEDULED_DISCOVERY if discovery else DemandKind.SCHEDULED_REPAIR
            with durable_demand_context(row_demand_kind):
                changed, stopped, _receipt = await self._process_due_dialog(dialog_id, generation, discovery, now)
                return changed, stopped
        changed, stopped, _receipt = await self._process_due_dialog(dialog_id, generation, discovery, now)
        return changed, stopped

    async def _process_due_rows(
        self, due_rows: Sequence[tuple[int, int, bool]], now: int, demand_kind: DemandKind | None
    ) -> tuple[int, bool]:
        total = 0
        flood_waited = False
        for dialog_id, generation, discovery in due_rows:
            if self._shutdown_event.is_set():
                break
            changed, flood_waited = await self._process_due_row(dialog_id, generation, discovery, now, demand_kind)
            total += changed
            if flood_waited:
                break
        return total, flood_waited

    async def _run_slice(self, demand_kind: DemandKind | None = None, *, dialog_id: int | None = None) -> int:
        """Process one bounded slice, optionally restricted to one demand kind."""
        if self._conn.in_transaction:
            raise RuntimeError("scheduled reconciliation requires an idle connection")
        now = int(time.time())
        retry_at = _retry_at(self._conn)
        if retry_at is not None and retry_at > now:
            return 0
        return await self._process_slice_rows(now, demand_kind, dialog_id)

    async def _process_slice_rows(self, now: int, demand_kind: DemandKind | None, dialog_id: int | None) -> int:
        due_rows = self._due_rows(now, demand_kind)
        if dialog_id is not None:
            due_rows = [row for row in due_rows if row[0] == dialog_id]
        total, _flood_waited = await self._process_due_rows(due_rows, now, demand_kind)

        return total


class _ScheduledDemandAdapter(DurableDemandAdapter):
    """Expose one scheduled queue as durable demand over existing state."""

    demand_kind: DemandKind

    def __init__(self, reconciler: ScheduledMessageReconciler, *, account_id: int) -> None:
        self._reconciler = reconciler
        if type(account_id) is not int or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        self._account_id = account_id
        self._reconciler.bind_account_id(account_id)

    def _apply_fact_and_account_retries(
        self,
        release_at: float,
        *,
        now: float,
    ) -> float:
        del now
        account_retry_at = _retry_at(self._reconciler._conn)
        if account_retry_at is not None:
            release_at = max(release_at, float(account_retry_at))
        return release_at

    def status(self, now: float, *, excluded: frozenset[WorkRevision] = frozenset()) -> DemandStatus | None:
        """Read the earliest due row without claiming or changing state."""
        column = "repair_due_at" if self.demand_kind is DemandKind.SCHEDULED_REPAIR else "discovery_due_at"
        repair_precedence = "AND repair_due_at IS NULL" if self.demand_kind is DemandKind.SCHEDULED_DISCOVERY else ""
        rows = cast(
            list[tuple[object, object, object, object]],
            self._reconciler._conn.execute(
                f"SELECT dialog_id,dirty_generation,COALESCE(snapshot_observation_order,0),{column} "
                f"FROM scheduled_reconciliation_state WHERE {column} IS NOT NULL {repair_precedence} ORDER BY {column},dialog_id"
            ).fetchall(),
        )
        candidates: list[DemandStatus] = []
        for dialog_id, generation, snapshot_order, due_at in rows:
            dialog_id, generation, snapshot_order, due_at = (
                _as_int(dialog_id),
                _as_int(generation),
                _as_int(snapshot_order),
                _as_int(due_at),
            )
            selected = WorkRevision(
                WorkKey(self.demand_kind, self._account_id, str(dialog_id)),
                f"{generation}|{'discovery' if self.demand_kind is DemandKind.SCHEDULED_DISCOVERY else 'repair'}|{snapshot_order}",
            )
            if selected in excluded:
                continue
            release_at = self._apply_fact_and_account_retries(float(due_at), now=now)
            candidates.append(DemandStatus(release_at, float(due_at), selected=selected))
        seed = self._seed_status(excluded)
        if seed is not None:
            candidates.append(seed)
        return min(
            candidates,
            key=lambda candidate: (candidate.release_at, candidate.freshness_deadline or candidate.release_at),
            default=None,
        )

    def _seed_status(self, excluded: frozenset[WorkRevision]) -> DemandStatus | None:
        if self.demand_kind is not DemandKind.SCHEDULED_DISCOVERY:
            return None
        state = self._seed_account_state()
        unseeded = self._reconciler._unseeded_candidate_ids()
        directory_generation = self._reconciler._directory_generation()
        stored_generation = None if state is None or state[1] is None else _as_int(state[1])
        if not unseeded and stored_generation == directory_generation and state is not None and state[0] is not None:
            return None
        selected = WorkRevision(
            WorkKey(self.demand_kind, self._account_id, "seed"),
            self._reconciler._seed_revision(unseeded, directory_generation),
        )
        return None if selected in excluded else DemandStatus(0.0, selected=selected)

    def _seed_account_state(self) -> tuple[object, object] | None:
        state = cast(
            tuple[object, object] | None,
            self._reconciler._conn.execute(
                "SELECT account_id,directory_generation FROM scheduled_sync_state WHERE key='account'"
            ).fetchone(),
        )
        if state is not None and state[0] is not None and _as_int(state[0]) != self._account_id:
            raise RuntimeError("scheduled mirror is bound to a different Telegram account")
        return state

    async def run_slice(self, budget: RpcAttemptBudget) -> SliceOutcome:
        """Run one bounded scheduled slice under its exact demand and budget."""
        if not isinstance(budget, RpcAttemptBudget):
            raise TypeError("budget must be an RpcAttemptBudget")
        selected = budget.selected
        if selected is None:
            raise ValueError("scheduled demand requires a selected revision")
        status = self._selected_status(selected, budget)
        if isinstance(status, SliceOutcome):
            return status
        return await self._run_selected_slice(selected, budget)

    def _selected_status(self, selected: WorkRevision, budget: RpcAttemptBudget) -> DemandStatus | SliceOutcome:
        now = time.time()
        if selected.key.demand_kind is not self.demand_kind or selected.key.account_id != self._account_id:
            return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if selected.key.target == "seed":
            status = self._seed_status(frozenset())
            if status is None or status.selected != selected:
                return SliceOutcome(selected, SliceOutcomeKind.STALE)
        else:
            status = self._selected_dialog_status(selected, now)
            if status is None:
                return SliceOutcome(selected, SliceOutcomeKind.STALE)
        if not status.is_ready(now):
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if budget.exhausted and not budget.can_prepare_retry:
            return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        return status

    def _selected_dialog_status(self, selected: WorkRevision, now: float) -> DemandStatus | None:
        """Validate one selected dialog directly, independent of queue head order."""
        row = self._selected_dialog_row(selected)
        if row is None:
            return None
        generation, snapshot_order, due_at = row
        phase = "discovery" if self.demand_kind is DemandKind.SCHEDULED_DISCOVERY else "repair"
        current = WorkRevision(selected.key, f"{generation}|{phase}|{snapshot_order}")
        if current != selected:
            return None
        release_at = self._apply_fact_and_account_retries(float(due_at), now=now)
        return DemandStatus(release_at, float(due_at), selected=selected)

    def _selected_dialog_row(self, selected: WorkRevision) -> tuple[int, int, int] | None:
        target = selected.key.target
        try:
            dialog_id = int(target)
        except ValueError:
            return None
        if dialog_id == 0 or str(dialog_id) != target:
            return None
        column = "repair_due_at" if self.demand_kind is DemandKind.SCHEDULED_REPAIR else "discovery_due_at"
        repair_precedence = "AND repair_due_at IS NULL" if self.demand_kind is DemandKind.SCHEDULED_DISCOVERY else ""
        row = cast(
            tuple[object, object, object] | None,
            self._reconciler._conn.execute(
                f"SELECT dirty_generation,COALESCE(snapshot_observation_order,0),{column} "
                f"FROM scheduled_reconciliation_state WHERE dialog_id=? {repair_precedence}",
                (dialog_id,),
            ).fetchone(),
        )
        if row is None or row[2] is None:
            return None
        return _as_int(row[0]), _as_int(row[1]), _as_int(row[2])

    async def _run_selected_slice(self, selected: WorkRevision, budget: RpcAttemptBudget) -> SliceOutcome:
        with durable_demand_context(self.demand_kind):
            with rpc_attempt_budget(budget):
                try:
                    if selected.key.target == "seed":
                        committed_revision = await self._reconciler._seed_selected_candidates(
                            selected.revision, self._account_id
                        )
                    else:
                        _changed, committed_revision = await self._reconciler._run_demand_slice_with_receipt(
                            self.demand_kind, int(selected.key.target), expected_revision=selected.revision
                        )
                except TelegramRpcAdmissionDeferred, TelegramRpcMethodRecoveryPending:
                    raise
                except RpcAttemptBudgetExhaustedError:
                    return SliceOutcome(selected, SliceOutcomeKind.DEFERRED)
        if committed_revision is not None:
            return SliceOutcome(selected, SliceOutcomeKind.PROGRESSED, committed_revision)
        return SliceOutcome(selected, SliceOutcomeKind.NO_PROGRESS)


class ScheduledRepairDemandAdapter(_ScheduledDemandAdapter):
    """Durable demand adapter for active scheduled queue repair."""

    demand_kind = DemandKind.SCHEDULED_REPAIR


class ScheduledDiscoveryDemandAdapter(_ScheduledDemandAdapter):
    """Durable demand adapter for quiet scheduled queue discovery."""

    demand_kind = DemandKind.SCHEDULED_DISCOVERY


__all__ = [
    "ScheduledDiscoveryDemandAdapter",
    "ScheduledMessageReconciler",
    "ScheduledReconciliationPolicy",
    "ScheduledRepairDemandAdapter",
    "mark_missing_from_snapshot",
    "mark_scheduled_messages_removed",
    "scheduled_dialog_id",
    "scheduled_message_dialog_id",
    "upsert_scheduled_message",
    "verify_scheduled_publication",
]
