"""Transaction-neutral SQLite persistence for canonical message bundles."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import cast

from mcp_telegram.delivered_update import current_delivered_update

from .. import message_contracts as _message_contracts
from ..alert_policy import incoming_human_dm_sql
from ..entity_profile.membership import MembershipChange, MembershipFact, MembershipSignalCounts, changed_members
from ..entity_profile.repository import request_membership_refreshes
from ..fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from ..hydration_queue import (
    HYDRATION_QUEUE_TABLE,
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationPriority,
    HydrationQueueRepository,
)
from ..image_authorization import invalidate_message_source, invalidate_nonchannel_sources
from ..media_fact import decode_media_fact, is_transcribable_telegram_media
from ..observation_order import allocate_observation_order
from ..own_only_contracts import CHANNEL_PEER_ID_OFFSET, is_channel_peer_id
from ..reactions.contracts import ReactionAggregate, ReactionAggregateSource
from ..reactions.persistence import replace_reaction_aggregates
from .sqlite_hydration_jobs import (
    _FACT_HYDRATION_EMPTY_KINDS,
    HydrationJobEvidence,
    _is_canonical_media_pair,
    reconcile_fact_hydration_job,
)


def _insert_sql(table: str, dataclass_type: type) -> str:
    column_names = tuple(field.name for field in fields(dataclass_type))
    return f"INSERT OR REPLACE INTO {table} ({', '.join(column_names)}) VALUES ({', '.join(':' + name for name in column_names)})"


_STORED_MESSAGE_FIELDS = tuple(field.name for field in fields(_message_contracts.StoredMessage))
_INSERT_MESSAGE_SQL = f"INSERT OR REPLACE INTO messages ({', '.join(_STORED_MESSAGE_FIELDS)}, reply_count, is_deleted) VALUES ({', '.join(':' + name for name in _STORED_MESSAGE_FIELDS)}, :reply_count, 0)"
_INSERT_ENTITY_SQL = _insert_sql("message_entities", _message_contracts.EntityRecord)
_INSERT_FORWARD_SQL = _insert_sql("message_forwards", _message_contracts.ForwardRecord)
_DELETE_ENTITIES_SQL = "DELETE FROM message_entities WHERE dialog_id = ? AND message_id = ?"
_DELETE_FORWARD_SQL = "DELETE FROM message_forwards WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_TEXT_SQL = "SELECT text FROM messages WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_EXISTS_SQL = "SELECT 1 FROM messages WHERE dialog_id = ? AND message_id = ?"
_SELECT_MESSAGE_OUT_SQL = "SELECT out FROM messages WHERE dialog_id = ? AND message_id = ?"
_NEXT_VERSION_SQL = "SELECT COALESCE(MAX(version), 0) + 1 FROM message_versions WHERE dialog_id = ? AND message_id = ?"
_INSERT_VERSION_SQL = "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date, origin) VALUES (?, ?, ?, ?, ?, ?)"
_UPDATE_MESSAGE_TEXT_SQL = "UPDATE messages SET text = ? WHERE dialog_id = ? AND message_id = ?"
_MARK_DELETED_SQL = (
    "UPDATE messages SET is_deleted = 1, deleted_at = ? WHERE dialog_id = ? AND message_id = ? AND is_deleted = 0"
)
_INSERT_HUMAN_DM_EDIT_ALERT_SQL = f"""
INSERT OR IGNORE INTO conversation_history_events(kind, occurred_at, time_basis, dialog_id, message_id, version)
SELECT 'edit', ?, 'telegram', ?, ?, ?
FROM messages m
WHERE m.dialog_id = ? AND m.message_id = ?
  AND {incoming_human_dm_sql("m")}
"""
_SELECT_HUMAN_DM_MESSAGE_SQL = f"""
SELECT 1
FROM messages m
WHERE m.dialog_id = ? AND m.message_id = ?
  AND {incoming_human_dm_sql("m")}
"""
_SELECT_UNDELETED_MESSAGES_SQL = (
    "SELECT message_id FROM messages WHERE dialog_id = ? AND is_deleted = 0 AND sent_at < ?"
)


@dataclass(frozen=True, slots=True)
class MessageTextLookup:
    found: bool
    text: str | None


@dataclass(frozen=True, slots=True)
class MessageOutLookup:
    found: bool
    outgoing: bool


@dataclass(frozen=True, slots=True)
class MessageWriteResult:
    accepted_count: int
    membership_counts: MembershipSignalCounts


@dataclass(frozen=True, slots=True)
class EditedMessageWriteResult:
    version: int | None
    membership_counts: MembershipSignalCounts


@dataclass(frozen=True, slots=True)
class _StoredPageFact:
    exists: bool
    text: str | None
    indexed_text: str | None
    index_matches_key: bool
    transcription: tuple[str, int] | None
    membership: MembershipFact | None


@dataclass(frozen=True, slots=True)
class _PageEvidence:
    facts: dict[tuple[int, int], _StoredPageFact]
    jobs: dict[tuple[int, int], frozenset[str]]
    queue: HydrationQueueRepository | None


def _page_facts(conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]) -> _PageEvidence:
    """Read current text, index, transcription and queue keys in bounded pages."""
    facts: dict[tuple[int, int], _StoredPageFact] = {}
    jobs: dict[tuple[int, int], frozenset[str]] = {}
    queue = HydrationQueueRepository(conn)
    queue_available = queue.is_available()
    keys = [(item.message.dialog_id, item.message.message_id) for item in extracted]
    for offset in range(0, len(keys), 400):
        batch = keys[offset : offset + 400]
        values = ",".join("(?,?)" for _ in batch)
        params = tuple(value for key in batch for value in key)
        prefix = f"WITH wanted(dialog_id,message_id) AS (VALUES {values}) "
        facts.update(_read_stored_page(conn, prefix, params))
        if queue_available:
            jobs.update(_read_page_jobs(conn, prefix, params))
    return _PageEvidence(facts, jobs, queue if queue_available else None)


def _read_stored_page(
    conn: sqlite3.Connection, prefix: str, params: tuple[int, ...]
) -> dict[tuple[int, int], _StoredPageFact]:
    rows = cast(
        list[
            tuple[
                int,
                int,
                int | None,
                str | None,
                str | None,
                int | None,
                int | None,
                str | None,
                int | None,
                int | None,
                str | None,
                int | None,
            ]
        ],
        conn.execute(
            prefix + "SELECT w.dialog_id,w.message_id,m.message_id,m.text,f.stemmed_text,"
            "f.dialog_id,f.message_id,t.text,t.transcription_id,m.is_service,m.service_action,m.sender_id "
            "FROM wanted w LEFT JOIN messages m USING(dialog_id,message_id) "
            "LEFT JOIN message_fts_keys k ON k.dialog_id=w.dialog_id AND k.message_id=w.message_id "
            "LEFT JOIN messages_fts f ON f.rowid=k.id "
            "LEFT JOIN message_transcriptions t ON t.dialog_id=w.dialog_id AND t.message_id=w.message_id",
            params,
        ).fetchall(),
    )
    return {
        (dialog_id, message_id): _StoredPageFact(
            present is not None,
            text,
            indexed,
            (fts_dialog, fts_message) == (dialog_id, message_id),
            None if transcript_id is None else (cast(str, transcript), transcript_id),
            None if present is None else MembershipFact(service or 0, action, sender),
        )
        for dialog_id, message_id, present, text, indexed, fts_dialog, fts_message, transcript, transcript_id, service, action, sender in rows
    }


def _read_page_jobs(
    conn: sqlite3.Connection, prefix: str, params: tuple[int, ...]
) -> dict[tuple[int, int], frozenset[str]]:
    rows = cast(
        list[tuple[int, int, str]],
        conn.execute(
            prefix + f"SELECT q.dialog_id,q.message_id,q.kind FROM wanted w CROSS JOIN {HYDRATION_QUEUE_TABLE} q "
            "ON q.dialog_id=w.dialog_id AND q.message_id=w.message_id WHERE q.kind IN (?,?)",
            (*params, MEDIA_METADATA_KIND, TRANSCRIPTION_HYDRATION_KIND),
        ).fetchall(),
    )
    jobs: dict[tuple[int, int], set[str]] = {}
    for dialog_id, message_id, kind in rows:
        jobs.setdefault((dialog_id, message_id), set()).add(kind)
    return {key: frozenset(kinds) for key, kinds in jobs.items()}


def message_exists(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Return whether the canonical message key is already persisted."""
    return conn.execute(_SELECT_MESSAGE_EXISTS_SQL, (dialog_id, message_id)).fetchone() is not None


def read_message_text(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> MessageTextLookup:
    """Read one message text without opening or committing a transaction."""
    row = cast(tuple[str | None] | None, conn.execute(_SELECT_MESSAGE_TEXT_SQL, (dialog_id, message_id)).fetchone())
    return MessageTextLookup(found=row is not None, text=None if row is None else row[0])


def read_message_out(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> MessageOutLookup:
    """Read the canonical outgoing marker without opening a transaction."""
    row = cast(tuple[int] | None, conn.execute(_SELECT_MESSAGE_OUT_SQL, (dialog_id, message_id)).fetchone())
    return MessageOutLookup(found=row is not None, outgoing=bool(row[0]) if row is not None else False)


def has_nonchannel_deletion(conn: sqlite3.Connection, dialog_id: int, message_id: int) -> bool:
    """Channel message IDs are independent of the account's nonchannel namespace."""
    if is_channel_peer_id(dialog_id):
        return False
    return (
        conn.execute("SELECT 1 FROM nonchannel_message_deletions WHERE message_id=?", (message_id,)).fetchone()
        is not None
    )


def _validated_message_ids(message_ids: Sequence[int]) -> tuple[int, ...]:
    return tuple(
        dict.fromkeys(
            message_id
            for message_id in message_ids
            if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0
        )
    )


def record_nonchannel_deletions(
    conn: sqlite3.Connection, message_ids: Sequence[int], deleted_at: int
) -> tuple[int, ...]:
    """Retain terminal evidence even before the canonical peer/body is known."""
    ids = _validated_message_ids(message_ids)
    invalidate_nonchannel_sources(conn, ids)
    conn.executemany(
        "INSERT OR IGNORE INTO nonchannel_message_deletions(message_id,deleted_at) VALUES(?,?)",
        ((message_id, deleted_at) for message_id in ids),
    )
    return ids


def record_message_deletions(conn: sqlite3.Connection, dialog_id: int, message_ids: Sequence[int]) -> tuple[int, ...]:
    """Retain known-peer terminal keys independently of message-body coverage."""
    ids = _validated_message_ids(message_ids)
    pending = [
        message_id
        for message_id in ids
        if conn.execute(
            "SELECT 1 FROM message_observations WHERE dialog_id=? AND message_id=? AND is_deleted=1",
            (dialog_id, message_id),
        ).fetchone()
        is None
    ]
    if pending:
        for message_id in pending:
            invalidate_message_source(conn, dialog_id, message_id)
        observation_order = allocate_observation_order(conn)
        conn.executemany(
            "INSERT INTO message_observations(dialog_id,message_id,observation_order,is_deleted) VALUES(?,?,?,1) ON CONFLICT(dialog_id,message_id) DO UPDATE SET is_deleted=1,observation_order=excluded.observation_order",
            ((dialog_id, message_id, observation_order) for message_id in pending),
        )
    return ids


def _is_current_observation(
    conn: sqlite3.Connection,
    item: _message_contracts.ExtractedMessage,
    source: ReactionAggregateSource | str = ReactionAggregateSource.HISTORY,
) -> bool:
    message = item.message
    if has_nonchannel_deletion(conn, message.dialog_id, message.message_id):
        return False
    key = (message.dialog_id, message.message_id)
    state = cast(
        tuple[int, int, int] | None,
        conn.execute(
            "SELECT observation_order,is_deleted,source_rank FROM message_observations WHERE dialog_id=? AND message_id=?",
            key,
        ).fetchone(),
    )
    current = cast(
        tuple[int | None, int] | None,
        conn.execute("SELECT edit_date,is_deleted FROM messages WHERE dialog_id=? AND message_id=?", key).fetchone(),
    )
    if _stored_message_deleted(state, current):
        return False
    if current is not None:
        incoming_version, current_version = message.edit_date or 0, current[0] or 0
        if incoming_version < current_version:
            return False
        if incoming_version > current_version:
            return True
    return _follows_stored_observation(state, item.observation_order, ReactionAggregateSource(source).rank)


def _stored_message_deleted(state: tuple[int, int, int] | None, current: tuple[int | None, int] | None) -> bool:
    return (state is not None and bool(state[1])) or (current is not None and bool(current[1]))


def _follows_stored_observation(state: tuple[int, int, int] | None, order: int | None, source_rank: int) -> bool:
    if state is None:
        return True
    if order is not None and order != state[0]:
        return order > state[0]
    return source_rank >= state[2]


def persist_edited_message(  # noqa: PLR0913
    conn: sqlite3.Connection,
    extracted: _message_contracts.ExtractedMessage,
    *,
    old_text: str | None,
    edit_date: int,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
    reaction_source: ReactionAggregateSource | str = ReactionAggregateSource.MESSAGE_EDIT,
    profile_owner_account_id: int | None = None,
) -> EditedMessageWriteResult:
    """Version and persist a changed message in the caller's transaction."""
    dialog_id, message_id = extracted.message.dialog_id, extracted.message.message_id
    if not _is_current_observation(conn, extracted, reaction_source):
        return EditedMessageWriteResult(None, MembershipSignalCounts())
    current = read_message_text(conn, dialog_id, message_id)
    if not current.found:
        return EditedMessageWriteResult(None, MembershipSignalCounts())
    if current.text == extracted.message.text:
        result = insert_messages_with_fts(
            conn,
            [extracted],
            priority=priority,
            reaction_source=reaction_source,
            reaction_observed_at=edit_date,
            profile_owner_account_id=profile_owner_account_id,
        )
        return EditedMessageWriteResult(None, result.membership_counts)
    old_text = current.text
    keep_history = conn.execute(_SELECT_HUMAN_DM_MESSAGE_SQL, (dialog_id, message_id)).fetchone() is not None
    if not keep_history:
        result = insert_messages_with_fts(
            conn,
            [extracted],
            priority=priority,
            reaction_source=reaction_source,
            reaction_observed_at=edit_date,
            profile_owner_account_id=profile_owner_account_id,
        )
        return EditedMessageWriteResult(None, result.membership_counts)
    version_row = cast(tuple[int], conn.execute(_NEXT_VERSION_SQL, (dialog_id, message_id)).fetchone())
    next_version = int(version_row[0])
    conn.execute(_INSERT_VERSION_SQL, (dialog_id, message_id, next_version, old_text, edit_date, "telegram_edit"))
    result = insert_messages_with_fts(
        conn,
        [extracted],
        priority=priority,
        reaction_source=reaction_source,
        reaction_observed_at=edit_date,
        profile_owner_account_id=profile_owner_account_id,
    )
    conn.execute(
        _INSERT_HUMAN_DM_EDIT_ALERT_SQL,
        (edit_date, dialog_id, message_id, next_version, dialog_id, message_id),
    )
    return EditedMessageWriteResult(next_version, result.membership_counts)


def persist_transcribed_text(
    conn: sqlite3.Connection,
    dialog_id: int,
    message_id: int,
    *,
    old_text: str | None,
    transcribed_text: str,
) -> bool:
    """Persist changed transcription text without creating edit history."""
    if old_text == transcribed_text:
        return False
    conn.execute(_UPDATE_MESSAGE_TEXT_SQL, (transcribed_text, dialog_id, message_id))
    conn.execute(DELETE_FTS_SQL, (dialog_id, message_id))
    conn.execute(INSERT_FTS_SQL, (dialog_id, message_id, stem_text(transcribed_text)))
    return True


def mark_message_deleted(conn: sqlite3.Connection, dialog_id: int, message_id: int, deleted_at: int) -> bool:
    """Tombstone one message and report whether this call changed its state."""
    if not record_message_deletions(conn, dialog_id, (message_id,)):
        return False
    cursor = conn.execute(_MARK_DELETED_SQL, (deleted_at, dialog_id, message_id))
    conn.execute(DELETE_FTS_SQL, (dialog_id, message_id))
    if cursor.rowcount > 0:
        HydrationQueueRepository(conn).remove_for_message(dialog_id, message_id)
    return cursor.rowcount > 0


def find_unique_nonchannel_message_dialogs(conn: sqlite3.Connection, message_ids: Sequence[int]) -> dict[int, int]:
    """Resolve only unique cached keys in Telegram's nonchannel ID namespace."""
    unique_ids = tuple(dict.fromkeys(int(message_id) for message_id in message_ids))
    if not unique_ids:
        return {}
    placeholders = ", ".join("?" for _ in unique_ids)
    rows = cast(
        Sequence[tuple[int, int]],
        conn.execute(
            f"""SELECT m.message_id, m.dialog_id
                FROM messages m
                WHERE m.message_id IN ({placeholders}) AND m.is_deleted = 0
                  AND m.dialog_id >= {-CHANNEL_PEER_ID_OFFSET}""",
            unique_ids,
        ).fetchall(),
    )
    candidates: dict[int, list[int]] = {}
    for message_id, dialog_id in rows:
        candidates.setdefault(int(message_id), []).append(int(dialog_id))
    return {message_id: dialog_ids[0] for message_id, dialog_ids in candidates.items() if len(dialog_ids) == 1}


def list_undeleted_message_ids(conn: sqlite3.Connection, dialog_id: int, sent_before: int) -> tuple[int, ...]:
    """List undeleted message IDs sent strictly before the caller's cutoff."""
    rows = cast(Sequence[tuple[int]], conn.execute(_SELECT_UNDELETED_MESSAGES_SQL, (dialog_id, sent_before)).fetchall())
    return tuple(int(message_id) for (message_id,) in rows)


def _accepted_message_bundles(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    source: ReactionAggregateSource | str,
) -> list[_message_contracts.ExtractedMessage]:
    accepted_by_key: dict[tuple[int, int], _message_contracts.ExtractedMessage] = {}
    for item in extracted:
        if _is_current_observation(conn, item, source):
            key = (item.message.dialog_id, item.message.message_id)
            prior = accepted_by_key.get(key)
            if prior is None or (item.message.edit_date or 0, item.observation_order or 0) >= (
                prior.message.edit_date or 0,
                prior.observation_order or 0,
            ):
                accepted_by_key[key] = item
    return list(accepted_by_key.values())


def insert_messages_with_fts(  # noqa: PLR0913
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
    reaction_source: ReactionAggregateSource | str = ReactionAggregateSource.HISTORY,
    reaction_observed_at: int | None = None,
    profile_owner_account_id: int | None = None,
) -> MessageWriteResult:
    """Persist accepted bundles and return their unique key count."""
    accepted = _accepted_message_bundles(conn, extracted, reaction_source)
    if not accepted:
        return MessageWriteResult(0, MembershipSignalCounts())
    evidence = _page_facts(conn, accepted)
    for item in accepted:
        invalidate_message_source(conn, item.message.dialog_id, item.message.message_id)
    fallback_sequence = (
        allocate_observation_order(conn) if any(item.observation_order is None for item in accepted) else 0
    )
    for item in accepted:
        sequence = item.observation_order if item.observation_order is not None else fallback_sequence
        conn.execute(
            "INSERT INTO message_observations(dialog_id,message_id,observation_order,source_rank) VALUES(?,?,?,?) ON CONFLICT(dialog_id,message_id) DO UPDATE SET observation_order=excluded.observation_order,source_rank=excluded.source_rank",
            (item.message.dialog_id, item.message.message_id, sequence, ReactionAggregateSource(reaction_source).rank),
        )
        conn.execute(
            "INSERT INTO message_image_sources(dialog_id,message_id,photo_id,observation_order,eligible) VALUES(?,?,?,?,?) "
            "ON CONFLICT(dialog_id,message_id) DO UPDATE SET photo_id=excluded.photo_id, "
            "observation_order=excluded.observation_order,eligible=excluded.eligible",
            (
                item.message.dialog_id,
                item.message.message_id,
                item.private_photo_id,
                sequence,
                int(item.private_photo_eligible),
            ),
        )
    projected = _overlay_message_transcriptions(
        conn, _preserve_transcribed_texts(accepted, evidence.facts), evidence.facts
    )
    _write_message_rows_and_fts(conn, projected, evidence, priority=priority)
    _delete_entity_and_forward_projections(conn, projected)
    _replace_reaction_projections(conn, projected, source=reaction_source, observed_at=reaction_observed_at)
    _insert_entity_and_forward_projections(conn, projected)
    changes = _membership_changes(projected, evidence.facts)
    counts = (
        request_membership_refreshes(conn, profile_owner_account_id, changes, now=int(time.time()))
        if changes and profile_owner_account_id is not None
        else MembershipSignalCounts(received=len(changes), ignored=len(changes), ignored_account_mismatch=len(changes))
    )
    return MessageWriteResult(len(projected), counts)


def _membership_changes(
    extracted: Sequence[_message_contracts.ExtractedMessage], facts: dict[tuple[int, int], _StoredPageFact]
) -> list[MembershipChange]:
    changes = []
    for item in extracted:
        message = item.message
        before = facts[(message.dialog_id, message.message_id)].membership
        after = MembershipFact(message.is_service, message.service_action, message.sender_id)
        subjects = changed_members(before, after)
        if subjects:
            revision = message.edit_date if before is not None else (message.edit_date or message.sent_at)
            changes.append(MembershipChange(message.dialog_id, subjects, revision))
    return changes


def _write_message_rows_and_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    evidence: _PageEvidence,
    *,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    messages = [item.message for item in extracted]
    conn.executemany(
        _INSERT_MESSAGE_SQL, [{**asdict(item.message), "reply_count": item.reply_count} for item in extracted]
    )
    changed = []
    for message in messages:
        key = (message.dialog_id, message.message_id)
        fact = evidence.facts[key]
        stemmed = stem_text(message.text)
        if _fts_refresh_needed(fact, message.text, stemmed):
            changed.append((*key, stemmed))
    conn.executemany(DELETE_FTS_SQL, ((dialog, message) for dialog, message, _ in changed))
    conn.executemany(INSERT_FTS_SQL, changed)
    if evidence.queue is not None:
        now = int(time.time())
        for message in messages:
            key = (message.dialog_id, message.message_id)
            reconcile_fact_hydration_job(
                conn,
                message,
                due_at=now,
                priority=priority,
                evidence=HydrationJobEvidence(
                    evidence.queue, evidence.jobs.get(key, frozenset()), evidence.facts[key].transcription is not None
                ),
            )


def _fts_refresh_needed(fact: _StoredPageFact, text: str | None, stemmed: str) -> bool:
    return not fact.exists or fact.text != text or not fact.index_matches_key or fact.indexed_text != stemmed


def _overlay_message_transcriptions(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    facts: dict[tuple[int, int], _StoredPageFact],
) -> list[_message_contracts.ExtractedMessage]:
    projected: list[_message_contracts.ExtractedMessage] = []
    for item in extracted:
        dialog_id, message_id = item.message.dialog_id, item.message.message_id
        key = (dialog_id, message_id)
        row = facts[key].transcription
        if row is None:
            projected.append(item)
            continue
        fact = decode_media_fact(item.message.media_kind, item.message.media_payload)
        if _is_canonical_media_pair(
            item.message.media_kind, item.message.media_payload, fact=fact
        ) and is_transcribable_telegram_media(fact):
            projected.append(replace(item, message=replace(item.message, text=row[0])))
            continue
        if (item.message.media_kind, item.message.media_payload) == (None, None) or _is_canonical_media_pair(
            item.message.media_kind, item.message.media_payload, fact=fact
        ):
            conn.execute(
                "DELETE FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?", (dialog_id, message_id)
            )
            facts[key] = replace(facts[key], transcription=None)
        projected.append(item)
    return projected


def _delete_entity_and_forward_projections(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> None:
    id_pairs = [(item.message.dialog_id, item.message.message_id) for item in extracted]
    conn.executemany(_DELETE_ENTITIES_SQL, id_pairs)
    conn.executemany(_DELETE_FORWARD_SQL, id_pairs)


def _replace_reaction_projections(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    source: ReactionAggregateSource | str = ReactionAggregateSource.HISTORY,
    observed_at: int | None = None,
) -> None:
    for item in extracted:
        if not item.reactions_observed:
            continue
        retained = (
            current_delivered_update()
            if source in {ReactionAggregateSource.REALTIME_MESSAGE, ReactionAggregateSource.MESSAGE_EDIT}
            else None
        )
        replace_reaction_aggregates(
            conn,
            item.message.dialog_id,
            item.message.message_id,
            tuple(ReactionAggregate(emoji=row.emoji, count=row.count) for row in item.reactions),
            source=source,
            observed_at=observed_at if retained is None else int(retained.captured_at),
            observation_sequence=None if retained is None else retained.reaction_sequence,
            can_see_list=item.reaction_can_see_list,
        )


def _insert_entity_and_forward_projections(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> None:
    entities = [entity for item in extracted for entity in item.entities]
    if entities:
        conn.executemany(_INSERT_ENTITY_SQL, [asdict(entity) for entity in entities])
    forwards = [item.forward for item in extracted if item.forward is not None]
    if forwards:
        conn.executemany(_INSERT_FORWARD_SQL, [asdict(forward) for forward in forwards])


def _preserve_transcribed_texts(
    extracted: Sequence[_message_contracts.ExtractedMessage], facts: dict[tuple[int, int], _StoredPageFact]
) -> list[_message_contracts.ExtractedMessage]:
    preserved_texts: dict[tuple[int, int], str] = {}
    for item in extracted:
        if item.message.text is not None and item.message.text.strip():
            continue
        if item.message.media_kind not in _FACT_HYDRATION_EMPTY_KINDS and item.message.media_kind != "voice":
            continue
        key = (item.message.dialog_id, item.message.message_id)
        text = facts[key].text
        if text:
            preserved_texts[key] = text
    if not preserved_texts:
        return list(extracted)
    return [
        replace(
            item,
            message=replace(
                item.message,
                text=preserved_texts.get((item.message.dialog_id, item.message.message_id), item.message.text),
            ),
        )
        for item in extracted
    ]
