"""Transaction-neutral SQLite policy for durable hydration jobs."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn, cast

from .. import message_contracts as _message_contracts
from ..hydration_queue import (
    MEDIA_METADATA_KIND,
    RICH_COMPOSITION_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationJob,
    HydrationPriority,
    HydrationQueueRepository,
)
from ..media_fact import MediaFact, decode_media_fact, encode_media_payload, is_transcribable_telegram_media
from ..observation_order import hydration_observation_current
from ..rich_hydration_policy import (
    _FACT_HYDRATION_ELIGIBILITY_SQL,
    _RICH_AUTOMATIC_DATE_SQL,
    _RICH_AUTOMATIC_ELIGIBILITY_SQL,
    fact_hydration_eligible,
    rich_composition_hydration_eligible,
)
from ..rich_hydration_policy import (
    rich_composition_automatic_eligible as rich_composition_automatic_eligible,
)

_EMPTY_MEDIA_PAYLOAD = "{}"
_FACT_HYDRATION_EMPTY_KINDS = frozenset(("contact", "other", "todo"))
_TRANSCRIBABLE_MEDIA_SQL = (
    "(json_valid(m.media_payload) "
    "AND json_type(CASE WHEN json_valid(m.media_payload) THEN m.media_payload ELSE '{}' END) = 'object' "
    "AND (m.media_kind = 'voice' OR (m.media_kind = 'video' "
    "AND json_type(CASE WHEN json_valid(m.media_payload) THEN m.media_payload ELSE '{}' END, "
    "'$.round_message') = 'true')))"
)
_TRANSCRIPTION_HYDRATION_MESSAGE_SQL = (
    "m.is_deleted = 0 AND (" + _TRANSCRIBABLE_MEDIA_SQL + ") "
    "AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
    "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id)"
)


@dataclass(frozen=True, slots=True)
class HydrationRepairCursor:
    """Position in the raw index order: newest time, then increasing peer/message IDs."""

    sent_at: int
    dialog_id: int
    message_id: int


@dataclass(frozen=True, slots=True)
class TranscriptionHydrationRepair:
    """Bounded result of repairing missing transcription jobs."""

    has_more: bool
    next_cursor: HydrationRepairCursor | None = None


@dataclass(frozen=True, slots=True)
class HydrationJobEvidence:
    """Queue and transcription facts captured for one message in this transaction."""

    queue: HydrationQueueRepository
    existing_kinds: frozenset[str] | None = None
    transcription_exists: bool | None = None


@dataclass(frozen=True, slots=True)
class MediaMetadataHydrationRepair:
    """Bounded result of repairing missing media metadata jobs."""

    has_more: bool
    next_contact_other_cursor: HydrationRepairCursor | None = None
    next_video_cursor: HydrationRepairCursor | None = None


def _first_json_object_key_wins(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Decode JSON objects with SQLite JSON1's first-key-wins semantics."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key not in result:
            result[key] = value
    return result


def _reject_json_constant(_value: str) -> NoReturn:
    """Reject Python JSON extensions that SQLite JSON1 does not accept."""
    raise ValueError("non-finite JSON number")


def _is_transcribable_media_pair(kind: object, payload: object) -> bool:
    """Apply the domain predicate to one projected SQLite media pair."""
    if not isinstance(payload, str):
        return False
    try:
        decoded_payload = cast(
            object,
            json.loads(
                payload,
                object_pairs_hook=_first_json_object_key_wins,
                parse_constant=_reject_json_constant,
            ),
        )
    except TypeError, ValueError, json.JSONDecodeError:
        return False
    if not isinstance(decoded_payload, dict):
        return False
    fact = decode_media_fact(kind, decoded_payload)
    return fact is not None and fact.payload == decoded_payload and is_transcribable_telegram_media(fact)


def _is_canonical_media_pair(kind: object, payload: object, *, fact: MediaFact | None = None) -> bool:
    """Return whether a stored media pair round-trips through the codec."""
    decoded = fact if fact is not None else decode_media_fact(kind, payload)
    return isinstance(payload, str) and decoded is not None and encode_media_payload(decoded) == payload


def media_fact_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether one unresolved media message may be hydrated now."""
    return (
        conn.execute(
            "SELECT 1 FROM messages m WHERE m.dialog_id = ? AND m.message_id = ? "
            "AND m.is_deleted = 0 AND ((m.media_kind IN ('contact', 'other', 'todo') AND m.media_payload = '{}') OR (m.media_kind='todo' AND EXISTS (SELECT 1 FROM hydration_jobs h WHERE h.kind='media_metadata' AND h.dialog_id=m.dialog_id AND h.message_id=m.message_id AND h.terminal=0)) "
            "OR (m.media_kind = 'video' AND json_valid(m.media_payload) "
            "AND json_type(m.media_payload) = 'object' "
            "AND json_type(m.media_payload, '$.round_message') IS NULL)) "
            "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone()
        is not None
    )


def rich_composition_is_unknown(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Distinguish unknown content from an already observed partial message."""
    row = cast(
        tuple[str | None] | None,
        conn.execute(
            "SELECT rich_message FROM messages WHERE dialog_id=? AND message_id=?", (dialog_id, message_id)
        ).fetchone(),
    )
    return row is not None and row[0] is None


def request_rich_composition_repair(
    conn: sqlite3.Connection, dialog_id: int, message_id: int, *, now: int
) -> dict[str, object]:
    """Enqueue one retained authorized target without changing retry state."""
    if not fact_hydration_eligible(conn, dialog_id) or not hydration_observation_current(
        conn, dialog_id, message_id, None
    ):
        return {"status": "ineligible"}
    row = cast(
        tuple[int, str | None, int] | None,
        conn.execute(
            "SELECT sent_at,rich_message,is_deleted FROM messages WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if row is None or row[2] or not rich_composition_hydration_eligible(conn, dialog_id, message_id):
        if row is not None and not row[2] and row[1] is not None:
            return {"status": "already_known"}
        return {"status": "ineligible"}
    HydrationQueueRepository(conn).enqueue(
        HydrationJob(RICH_COMPOSITION_KIND, dialog_id, message_id, now, 0, row[0], HydrationPriority.FOREGROUND)
    )
    job = cast(
        tuple[int, int, int, str | None] | None,
        conn.execute(
            "SELECT terminal,attempts,due_at,last_outcome FROM hydration_jobs WHERE kind=? AND dialog_id=? AND message_id=?",
            (RICH_COMPOSITION_KIND, dialog_id, message_id),
        ).fetchone(),
    )
    assert job is not None
    return {"status": "queued", "terminal": bool(job[0]), "attempts": job[1], "due_at": job[2], "last_outcome": job[3]}


_RICH_CURSOR_SIZE = 3


def _rich_repair_cursor(value: object) -> tuple[int, ...]:
    if value is None:
        return (9223372036854775807, 9223372036854775807, 9223372036854775807)
    if (
        not isinstance(value, (list, tuple))
        or len(value) != _RICH_CURSOR_SIZE
        or not all(type(item) is int for item in value)
    ):
        raise ValueError("invalid rich composition repair cursor")
    return tuple(value)


def rich_composition_cleanup_pending(conn: sqlite3.Connection) -> bool:
    """Return whether the local queue scope still needs reconciliation."""
    row = cast(
        tuple[str] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key='rich_composition_hydration_cursor'").fetchone(),
    )
    return row is None or cast(dict[str, object], json.loads(row[0])).get("cleanup_done") is not True


def cleanup_rich_composition_hydration_jobs(conn: sqlite3.Connection, *, max_jobs: int) -> bool:
    """Remove one bounded page of obsolete historical jobs in the caller transaction."""
    if max_jobs <= 0:
        return False
    key = "rich_composition_hydration_cursor"
    stored = cast(tuple[str] | None, conn.execute("SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone())
    state: dict[str, object] = {"cursor": None, "done": False} if stored is None else json.loads(stored[0])
    if state.get("cleanup_done", False):
        return False
    cleanup_cursor = cast(list[int], state.get("cleanup_cursor") or [-9223372036854775808, -9223372036854775808])
    cleanup_rows = cast(
        list[tuple[int, int]],
        conn.execute(
            "SELECT dialog_id,message_id FROM hydration_jobs WHERE kind='rich_composition' "
            "AND (dialog_id,message_id) > (?,?) ORDER BY dialog_id,message_id LIMIT ?",
            (*cleanup_cursor, max_jobs),
        ).fetchall(),
    )
    cleanup_examined = cleanup_rows
    conn.executemany(
        "DELETE FROM hydration_jobs WHERE kind='rich_composition' AND priority=0 AND dialog_id=? AND message_id=? "
        "AND EXISTS (SELECT 1 FROM messages m WHERE m.dialog_id=hydration_jobs.dialog_id "
        "AND m.message_id=hydration_jobs.message_id AND m.rich_message IS NULL AND NOT "
        + _RICH_AUTOMATIC_DATE_SQL
        + ")",
        cleanup_examined,
    )
    state["cleanup_cursor"] = list(cleanup_examined[-1]) if cleanup_examined else cleanup_cursor
    state["cleanup_done"] = len(cleanup_rows) < max_jobs
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(state)),
    )
    return not state["cleanup_done"]


def repair_rich_composition_hydration_jobs(conn: sqlite3.Connection, *, due_at: int, max_jobs: int) -> bool:
    """Commit a bounded historical scan and its queue entries in the caller's transaction."""
    if max_jobs <= 0:
        return False
    key = "rich_composition_hydration_cursor"
    stored = cast(tuple[str] | None, conn.execute("SELECT value FROM daemon_state WHERE key=?", (key,)).fetchone())
    state: dict[str, object] = {"cursor": None, "done": False} if stored is None else json.loads(stored[0])
    if not state.get("cleanup_done", False):
        return False
    if state["done"]:
        return _repair_rich_dialog(conn, due_at=due_at, max_jobs=max_jobs)
    cursor = _rich_repair_cursor(state["cursor"])
    rows = cast(
        list[_RepairRow],
        conn.execute(
            "SELECT m.sent_at,m.dialog_id,m.message_id FROM messages m INDEXED BY idx_messages_rich_unknown "
            "WHERE m.rich_message IS NULL AND m.is_deleted=0 "
            "AND (m.sent_at,m.dialog_id,m.message_id) < (?,?,?) "
            "ORDER BY m.sent_at DESC,m.dialog_id DESC,m.message_id DESC LIMIT ?",
            (*cursor, max_jobs + 1),
        ).fetchall(),
    )
    examined = rows[:max_jobs]
    _enqueue_repair_rows(
        conn,
        examined,
        kind=RICH_COMPOSITION_KIND,
        due_at=due_at,
        eligibility="m.rich_message IS NULL AND " + _RICH_AUTOMATIC_ELIGIBILITY_SQL,
    )
    has_more = len(rows) > max_jobs
    state.update(cursor=tuple(examined[-1]) if examined else state["cursor"], done=not has_more)
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(state)),
    )
    return has_more


def rich_composition_repair_revision(conn: sqlite3.Connection) -> str | None:
    """Inspect one indexed candidate without creating work on ordinary reads."""
    row = cast(
        tuple[str] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key='rich_composition_hydration_cursor'").fetchone(),
    )
    state = {"cursor": None, "done": False} if row is None else cast(dict[str, object], json.loads(row[0]))
    if not state.get("cleanup_done", False):
        return None
    if state["done"]:
        target = _rich_dialog_target(conn)
        return None if target is None else repr(target)
    cursor = _rich_repair_cursor(state["cursor"])
    candidate = cast(
        tuple[int, int, int] | None,
        conn.execute(
            "SELECT m.sent_at,m.dialog_id,m.message_id FROM messages m INDEXED BY idx_messages_rich_unknown "
            "WHERE m.rich_message IS NULL AND m.is_deleted=0 "
            "AND (m.sent_at,m.dialog_id,m.message_id) < (?,?,?) "
            "ORDER BY m.sent_at DESC,m.dialog_id DESC,m.message_id DESC LIMIT 1",
            cursor,
        ).fetchone(),
    )
    return repr((state["cursor"], None if candidate is None else tuple(candidate)))


def _rich_dialog_target(conn: sqlite3.Connection) -> tuple[int, int, int] | None:
    return cast(
        tuple[int, int, int] | None,
        conn.execute(
            "SELECT r.dialog_id,r.cursor,r.generation FROM rich_dialog_repairs r "
            "JOIN synced_dialogs s ON s.dialog_id=r.dialog_id AND s.status IN ('syncing','synced') "
            "JOIN full_history_enrollment e ON e.dialog_id=r.dialog_id AND e.enabled=1 "
            "WHERE r.cursor>=0 ORDER BY r.dialog_id LIMIT 1"
        ).fetchone(),
    )


def _repair_rich_dialog(conn: sqlite3.Connection, *, due_at: int, max_jobs: int) -> bool:
    """Scan one eligible dialog in bounded PK order inside the writer transaction."""
    target = _rich_dialog_target(conn)
    if target is None:
        return False
    dialog_id, cursor, generation = target
    rows = cast(
        list[tuple[int, int]],
        conn.execute(
            "SELECT message_id,sent_at FROM messages WHERE dialog_id=? AND message_id>? ORDER BY message_id LIMIT ?",
            (dialog_id, cursor, max_jobs + 1),
        ).fetchall(),
    )
    examined = rows[:max_jobs]
    _enqueue_repair_rows(
        conn,
        [(sent_at, dialog_id, message_id) for message_id, sent_at in examined],
        kind=RICH_COMPOSITION_KIND,
        due_at=due_at,
        eligibility=_RICH_AUTOMATIC_ELIGIBILITY_SQL,
    )
    if len(rows) > max_jobs:
        conn.execute(
            "UPDATE rich_dialog_repairs SET cursor=? WHERE dialog_id=? AND cursor=? AND generation=?",
            (examined[-1][0], dialog_id, cursor, generation),
        )
    else:
        conn.execute(
            "UPDATE rich_dialog_repairs SET cursor=-1 WHERE dialog_id=? AND cursor=? AND generation=?",
            (dialog_id, cursor, generation),
        )
    return _rich_dialog_target(conn) is not None


def transcription_hydration_eligible(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether one undeleted transcribable message still needs work."""
    row = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT m.media_kind, m.media_payload FROM messages m "
            "WHERE m.dialog_id = ? AND m.message_id = ? AND m.is_deleted = 0 "
            "AND (" + _TRANSCRIBABLE_MEDIA_SQL + ") "
            "AND NOT EXISTS (SELECT 1 FROM message_transcriptions mt "
            "WHERE mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id) "
            "AND EXISTS (" + _FACT_HYDRATION_ELIGIBILITY_SQL + ")",
            (dialog_id, message_id, dialog_id),
        ).fetchone(),
    )
    return row is not None and _is_transcribable_media_pair(*row)


_REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL = (
    "FROM messages m INDEXED BY idx_messages_transcribable_undeleted_sent "
    "WHERE m.is_deleted = 0 AND " + _TRANSCRIBABLE_MEDIA_SQL
)
_MEDIA_METADATA_HYDRATION_ELIGIBILITY_SQL = (
    "m.is_deleted = 0 AND ((m.media_kind IN ('contact', 'other', 'todo') AND m.media_payload = '{}') "
    "OR (m.media_kind = 'video' AND json_valid(m.media_payload) "
    "AND json_type(m.media_payload) = 'object' "
    "AND json_type(m.media_payload, '$.round_message') IS NULL))"
)
_REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL = (
    "FROM messages m INDEXED BY idx_messages_media_unresolved_contact_other "
    "WHERE m.is_deleted = 0 AND m.media_kind IN ('contact', 'other', 'todo') AND m.media_payload = '{}'"
)
_REPAIR_MEDIA_METADATA_VIDEO_SQL = (
    "FROM messages m INDEXED BY idx_messages_media_unresolved_video "
    "WHERE m.is_deleted = 0 AND m.media_kind = 'video' AND json_valid(m.media_payload) "
    "AND json_type(m.media_payload) = 'object' "
    "AND json_type(m.media_payload, '$.round_message') IS NULL"
)
type _RepairRow = tuple[int, int, int]
_REPAIR_ORDER_SQL = " ORDER BY m.sent_at DESC, m.dialog_id, m.message_id LIMIT ?"


def _repair_raw_page(
    conn: sqlite3.Connection, candidates_sql: str, cursor: HydrationRepairCursor | None, limit: int
) -> list[_RepairRow]:
    """Bound raw index reads before eligibility/queue checks, including skipped rows."""
    selection = "SELECT m.sent_at, m.dialog_id, m.message_id " + candidates_sql
    if cursor is None:
        return cast(list[_RepairRow], conn.execute(selection + _REPAIR_ORDER_SQL, (limit,)).fetchall())
    # Mixed-direction indexes need disjoint seeks; an OR/negated-time tuple scans old prefixes.
    seeks = (
        (
            "m.sent_at = ? AND m.dialog_id = ? AND m.message_id > ?",
            (cursor.sent_at, cursor.dialog_id, cursor.message_id),
        ),
        ("m.sent_at = ? AND m.dialog_id > ?", (cursor.sent_at, cursor.dialog_id)),
        ("m.sent_at < ?", (cursor.sent_at,)),
    )
    rows: list[_RepairRow] = []
    for predicate, parameters in seeks:
        remaining = limit - len(rows)
        if remaining == 0:
            break
        rows.extend(
            cast(
                list[_RepairRow],
                conn.execute(selection + " AND " + predicate + _REPAIR_ORDER_SQL, (*parameters, remaining)).fetchall(),
            )
        )
    return rows


def _enqueue_repair_rows(
    conn: sqlite3.Connection, rows: Sequence[_RepairRow], *, kind: str, due_at: int, eligibility: str
) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO hydration_jobs "
        "(kind, dialog_id, message_id, due_at, attempts, priority, message_sent_at, terminal) "
        "SELECT ?, m.dialog_id, m.message_id, ?, 0, ?, m.sent_at, 0 FROM messages m "
        "WHERE m.dialog_id = ? AND m.message_id = ? AND "
        + eligibility
        + " AND EXISTS ("
        + _FACT_HYDRATION_ELIGIBILITY_SQL
        + ")",
        ((kind, due_at, int(HydrationPriority.BACKFILL), dialog, message, dialog) for _, dialog, message in rows),
    )


def repair_transcription_hydration_jobs(
    conn: sqlite3.Connection, *, due_at: int, max_jobs: int, cursor: HydrationRepairCursor | None = None
) -> TranscriptionHydrationRepair:
    """Examine one bounded raw page; callers retain the cursor only after commit."""
    if max_jobs <= 0:
        return TranscriptionHydrationRepair(False, cursor)
    rows = _repair_raw_page(conn, _REPAIR_TRANSCRIPTION_CANDIDATES_FROM_SQL, cursor, max_jobs + 1)
    examined = rows[:max_jobs]
    _enqueue_repair_rows(
        conn,
        examined,
        kind=TRANSCRIPTION_HYDRATION_KIND,
        due_at=due_at,
        eligibility=_TRANSCRIPTION_HYDRATION_MESSAGE_SQL,
    )
    next_cursor = HydrationRepairCursor(*examined[-1]) if examined else cursor
    return TranscriptionHydrationRepair(len(rows) > max_jobs, next_cursor)


def repair_media_metadata_hydration_jobs(
    conn: sqlite3.Connection,
    *,
    due_at: int,
    max_jobs: int,
    contact_other_cursor: HydrationRepairCursor | None = None,
    video_cursor: HydrationRepairCursor | None = None,
) -> MediaMetadataHydrationRepair:
    """Merge bounded raw pages from the two partial indexes before checking eligibility."""
    if max_jobs <= 0:
        return MediaMetadataHydrationRepair(False, contact_other_cursor, video_cursor)
    contacts = _repair_raw_page(conn, _REPAIR_MEDIA_METADATA_CONTACT_OTHER_SQL, contact_other_cursor, max_jobs + 1)
    videos = _repair_raw_page(conn, _REPAIR_MEDIA_METADATA_VIDEO_SQL, video_cursor, max_jobs + 1)
    combined = sorted(
        [(False, row) for row in contacts] + [(True, row) for row in videos],
        key=lambda pair: (-pair[1][0], pair[1][1], pair[1][2]),
    )
    examined = combined[:max_jobs]
    _enqueue_repair_rows(
        conn,
        [row for _, row in examined],
        kind=MEDIA_METADATA_KIND,
        due_at=due_at,
        eligibility=_MEDIA_METADATA_HYDRATION_ELIGIBILITY_SQL,
    )
    for video, row in examined:
        if video:
            video_cursor = HydrationRepairCursor(*row)
        else:
            contact_other_cursor = HydrationRepairCursor(*row)
    return MediaMetadataHydrationRepair(len(combined) > max_jobs, contact_other_cursor, video_cursor)


def reconcile_fact_hydration_job(
    conn: sqlite3.Connection,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
    evidence: HydrationJobEvidence | None = None,
) -> None:
    """Reconcile one persisted message and its queue row in caller's transaction."""
    if evidence is None:
        queue = HydrationQueueRepository(conn)
        if not queue.is_available():
            return
        evidence = HydrationJobEvidence(queue)
    _reconcile_media_hydration_job(conn, evidence.queue, message, due_at=due_at, existing_kinds=evidence.existing_kinds)
    _reconcile_rich_composition_job(
        conn, evidence.queue, message, due_at=due_at, existing_kinds=evidence.existing_kinds
    )
    _reconcile_transcription_hydration_job(
        conn,
        message,
        due_at=due_at,
        priority=priority,
        evidence=evidence,
    )


def _reconcile_rich_composition_job(
    conn: sqlite3.Connection,
    queue: HydrationQueueRepository,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    existing_kinds: frozenset[str] | None,
) -> None:
    job = HydrationJob(
        RICH_COMPOSITION_KIND,
        message.dialog_id,
        message.message_id,
        due_at,
        0,
        message.sent_at,
        HydrationPriority.BACKFILL,
    )
    if message.rich_message is None:
        _enqueue_repair_rows(
            conn,
            [(message.sent_at, message.dialog_id, message.message_id)],
            kind=RICH_COMPOSITION_KIND,
            due_at=due_at,
            eligibility="m.rich_message IS NULL AND " + _RICH_AUTOMATIC_ELIGIBILITY_SQL,
        )
        return
    if rich_composition_hydration_eligible(conn, message.dialog_id, message.message_id):
        queue.enqueue(job)
    elif existing_kinds is None or RICH_COMPOSITION_KIND in existing_kinds:
        queue.remove(job)


def _reconcile_media_hydration_job(
    conn: sqlite3.Connection,
    queue: HydrationQueueRepository,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    existing_kinds: frozenset[str] | None,
) -> None:
    job = HydrationJob(
        MEDIA_METADATA_KIND,
        message.dialog_id,
        message.message_id,
        due_at,
        0,
        message.sent_at,
        HydrationPriority.BACKFILL,
    )
    unresolved = message.media_kind in _FACT_HYDRATION_EMPTY_KINDS and message.media_payload == _EMPTY_MEDIA_PAYLOAD
    if unresolved and media_fact_hydration_eligible(conn, message.dialog_id, message.message_id):
        queue.enqueue(job)
        return
    if existing_kinds is not None and MEDIA_METADATA_KIND not in existing_kinds:
        return
    if unresolved:
        queue.remove_active(job)
    elif message.media_kind != "todo":
        queue.remove(job)


def _reconcile_transcription_hydration_job(
    conn: sqlite3.Connection,
    message: _message_contracts.StoredMessage,
    *,
    due_at: int,
    priority: HydrationPriority,
    evidence: HydrationJobEvidence,
) -> None:
    queue = evidence.queue
    existing_kinds = evidence.existing_kinds
    transcription_exists = evidence.transcription_exists
    job = HydrationJob(
        TRANSCRIPTION_HYDRATION_KIND, message.dialog_id, message.message_id, due_at, 0, message.sent_at, priority
    )
    if transcription_exists is None:
        transcription_exists = (
            conn.execute(
                "SELECT 1 FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?",
                (message.dialog_id, message.message_id),
            ).fetchone()
            is not None
        )
    transcribable = _is_transcribable_media_pair(message.media_kind, message.media_payload)
    if transcribable and not transcription_exists:
        if transcription_hydration_eligible(conn, message.dialog_id, message.message_id):
            queue.enqueue(job)
        elif existing_kinds is None or TRANSCRIPTION_HYDRATION_KIND in existing_kinds:
            queue.remove_active(job)
    elif existing_kinds is None or TRANSCRIPTION_HYDRATION_KIND in existing_kinds:
        queue.remove(job)


def reconcile_fact_hydration_jobs_for_dialog(
    conn: sqlite3.Connection,
    dialog_id: int,
    *,
    due_at: int,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    """Enqueue all unresolved media for an eligible dialog after revalidation."""
    queue = HydrationQueueRepository(conn)
    if not queue.is_available() or not fact_hydration_eligible(conn, dialog_id):
        return
    rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            "SELECT message_id, sent_at FROM messages WHERE dialog_id = ? AND is_deleted = 0 "
            "AND media_kind IN ('contact', 'other', 'todo') AND media_payload = '{}' UNION ALL "
            "SELECT message_id, sent_at FROM messages WHERE dialog_id = ? AND is_deleted = 0 AND media_kind = 'video' "
            "AND json_valid(media_payload) AND json_type(media_payload) = 'object' "
            "AND json_type(media_payload, '$.round_message') IS NULL",
            (dialog_id, dialog_id),
        ).fetchall(),
    )
    for message_id, sent_at in rows:
        queue.enqueue(
            HydrationJob(
                MEDIA_METADATA_KIND, dialog_id, int(message_id), due_at, 0, int(sent_at), HydrationPriority.BACKFILL
            )
        )
    transcribable_rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            "SELECT m.message_id, m.sent_at FROM messages m LEFT JOIN message_transcriptions mt "
            "ON mt.dialog_id = m.dialog_id AND mt.message_id = m.message_id WHERE m.dialog_id = ? "
            "AND m.is_deleted = 0 AND (" + _TRANSCRIBABLE_MEDIA_SQL + ") AND mt.message_id IS NULL",
            (dialog_id,),
        ).fetchall(),
    )
    for message_id, sent_at in transcribable_rows:
        queue.enqueue(
            HydrationJob(TRANSCRIPTION_HYDRATION_KIND, dialog_id, int(message_id), due_at, 0, int(sent_at), priority)
        )


def enqueue_todo_refresh(
    conn: sqlite3.Connection, dialog_id: int, msg: object, now: int, observation_order: int
) -> None:
    reply = getattr(msg, "reply_to", None)
    parent = getattr(reply, "reply_to_msg_id", None)
    if (
        not isinstance(parent, int)
        or parent <= 0
        or getattr(reply, "reply_to_peer_id", None) is not None
        or bool(getattr(reply, "reply_to_scheduled", False))
    ):
        return
    row = cast(
        tuple[int] | None,
        conn.execute(
            "SELECT m.sent_at FROM messages m JOIN synced_dialogs d USING(dialog_id) "
            "JOIN full_history_enrollment e USING(dialog_id) "
            "WHERE m.dialog_id=? AND m.message_id=? AND m.is_deleted=0 AND m.media_kind='todo' "
            "AND d.status IN ('syncing','synced') AND e.enabled=1",
            (dialog_id, parent),
        ).fetchone(),
    )
    if row is None or not hydration_observation_current(conn, dialog_id, parent, None):
        return
    advanced = cast(
        tuple[int] | None,
        conn.execute(
            "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted,source_rank) "
            "VALUES(?,?,?,0,0) ON CONFLICT(dialog_id,message_id) DO UPDATE SET "
            "observation_order=excluded.observation_order "
            "WHERE message_observations.observation_order < excluded.observation_order RETURNING observation_order",
            (dialog_id, parent, observation_order),
        ).fetchone(),
    )
    if advanced is None:
        return
    conn.execute(
        "INSERT INTO hydration_jobs(kind,dialog_id,message_id,due_at,attempts,priority,message_sent_at,terminal) "
        "VALUES('media_metadata',?,?,?,0,1,?,0) ON CONFLICT(kind,dialog_id,message_id) DO UPDATE SET "
        "attempts=CASE WHEN terminal=0 AND last_outcome='processing' THEN MAX(attempts-1,0) ELSE attempts END, "
        "last_outcome=CASE WHEN terminal=0 AND last_outcome='processing' THEN 'queued' ELSE last_outcome END",
        (dialog_id, parent, now, row[0]),
    )
