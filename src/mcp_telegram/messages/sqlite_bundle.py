"""Transaction-neutral SQLite persistence for canonical message bundles."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import cast

from mcp_telegram.delivered_update import current_delivered_update

from .. import message_contracts as _message_contracts
from ..alert_policy import incoming_human_dm_sql
from ..entity_profile.membership import MembershipChange, MembershipFact, MembershipSignalCounts, changed_members
from ..entity_profile.repository import bound_profile_account, request_membership_refreshes
from ..fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from ..hydration_queue import (
    HYDRATION_QUEUE_TABLE,
    MEDIA_METADATA_KIND,
    TRANSCRIPTION_HYDRATION_KIND,
    HydrationPriority,
    HydrationQueueRepository,
)
from ..image_authorization import ScopeToken, invalidate_message_source, invalidate_nonchannel_sources
from ..media_fact import decode_media_fact, is_transcribable_telegram_media
from ..observation_order import allocate_observation_order, has_nonchannel_deletion
from ..own_only_contracts import CHANNEL_PEER_ID_OFFSET
from ..reactions.contracts import ReactionAggregate, ReactionAggregateSource, ReactionObservationBoundary
from ..reactions.persistence import replace_reaction_aggregates
from ..rich_content import decode_rich_message
from ..sync_transactions import require_write_transaction
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
_INSERT_VERSION_SQL = "INSERT INTO message_versions (dialog_id, message_id, version, old_text, edit_date, origin, old_rich_message, old_formatting_text, old_formatting_entities) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
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
        if current[0] is not None and incoming_version > current_version:
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


def _same_body_or_enrichment(
    old_text: str | None,
    previous: tuple[str | None, str | None, str | None, int | None],
    message: _message_contracts.StoredMessage,
    edit_date: int | None,
    source: ReactionAggregateSource | str,
) -> bool:
    if (old_text, *previous[:3]) == (
        message.text,
        message.rich_message,
        message.formatting_text,
        message.formatting_entities,
    ):
        return True
    if source == ReactionAggregateSource.MESSAGE_EDIT or previous[3] != edit_date:
        return False
    old_status = decode_rich_message(previous[0])[1]
    new_status = decode_rich_message(message.rich_message)[1]
    enrichment = (old_status == "unknown" and new_status != "unknown") or (
        old_status == "partial" and new_status == "complete"
    )
    return enrichment and (previous[1] == message.formatting_text or (previous[1] is None and old_text == message.text))


def persist_edited_message(  # noqa: PLR0913
    conn: sqlite3.Connection,
    extracted: _message_contracts.ExtractedMessage,
    *,
    old_text: str | None,
    edit_date: int | None,
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
    previous = cast(
        tuple[str | None, str | None, str | None, int | None],
        conn.execute(
            "SELECT rich_message,formatting_text,formatting_entities,edit_date FROM messages WHERE dialog_id=? AND message_id=?",
            (dialog_id, message_id),
        ).fetchone(),
    )
    if reaction_source != ReactionAggregateSource.MESSAGE_EDIT:
        extracted = preserve_rich_observation(extracted, previous, current.text)
    if _same_body_or_enrichment(current.text, previous, extracted.message, edit_date, reaction_source):
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
    conn.execute(
        _INSERT_VERSION_SQL, (dialog_id, message_id, next_version, old_text, edit_date, "telegram_edit", *previous[:3])
    )
    result = insert_messages_with_fts(
        conn,
        [extracted],
        priority=priority,
        reaction_source=reaction_source,
        reaction_observed_at=edit_date,
        profile_owner_account_id=profile_owner_account_id,
    )
    if edit_date is not None:
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
    if reaction_source != ReactionAggregateSource.MESSAGE_EDIT:
        accepted = [_preserve_stored_rich(conn, item) for item in accepted]
    accepted = [_preserve_newer_received_media(conn, item) for item in accepted]
    evidence = _page_facts(conn, accepted)
    for item in accepted:
        if item.invalidate_image_source:
            invalidate_message_source(conn, item.message.dialog_id, item.message.message_id)
    _record_message_observations(conn, accepted, reaction_source)
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


def _record_message_observations(
    conn: sqlite3.Connection,
    accepted: Sequence[_message_contracts.ExtractedMessage],
    reaction_source: ReactionAggregateSource | str,
) -> None:
    fallback_sequence = (
        allocate_observation_order(conn) if any(item.observation_order is None for item in accepted) else 0
    )
    for item in accepted:
        sequence = item.observation_order if item.observation_order is not None else fallback_sequence
        conn.execute(
            "INSERT INTO message_observations(dialog_id,message_id,observation_order,source_rank) VALUES(?,?,?,?) "
            "ON CONFLICT(dialog_id,message_id) DO UPDATE SET "
            "source_rank=CASE WHEN excluded.observation_order>message_observations.observation_order THEN excluded.source_rank "
            "WHEN excluded.observation_order=message_observations.observation_order THEN MAX(message_observations.source_rank,excluded.source_rank) "
            "ELSE message_observations.source_rank END, "
            "observation_order=MAX(message_observations.observation_order,excluded.observation_order)",
            (
                item.message.dialog_id,
                item.message.message_id,
                sequence if item.body_observation_order is None else item.body_observation_order,
                ReactionAggregateSource(reaction_source).rank,
            ),
        )
        conn.execute(
            "INSERT INTO message_image_sources(dialog_id,message_id,photo_id,observation_order,eligible) VALUES(?,?,?,?,?) "
            "ON CONFLICT(dialog_id,message_id) DO UPDATE SET photo_id=excluded.photo_id, "
            "observation_order=excluded.observation_order,eligible=excluded.eligible",
            (
                item.message.dialog_id,
                item.message.message_id,
                item.private_photo_id,
                sequence if item.media_observation_order is None else item.media_observation_order,
                int(item.private_photo_eligible),
            ),
        )


class ReceivedMessageScopeChangedError(RuntimeError):
    """Abort the owning transaction when the original acquisition scope changes."""


def persist_received_messages(  # noqa: PLR0913 - original request fence belongs with the received facts
    conn: sqlite3.Connection,
    messages: Sequence[_message_contracts.ReceivedMessage],
    *,
    scope: ScopeToken,
    claim_scope: Callable[[ScopeToken], bool],
    observation_order: int,
    expected_dialog_id: int | None = None,
    reaction_boundary: ReactionObservationBoundary | None = None,
    revalidated_access: bool = False,
) -> int:
    """Publish eligible received message bodies inside the existing writer transaction."""
    require_write_transaction(conn)
    if not claim_scope(scope) or bound_profile_account(conn) != scope.scope.account_id:
        raise ReceivedMessageScopeChangedError
    accepted = 0
    for received in messages:
        if not _received_message_eligible(conn, received, observation_order, expected_dialog_id, revalidated_access):
            continue
        item = replace(received.bundle, observation_order=observation_order)
        written = _persist_received_message(conn, replace(item, reactions_observed=False), scope.scope.account_id)
        accepted += written
        if written:
            _publish_received_reactions(conn, item, reaction_boundary)
    if not claim_scope(scope) or bound_profile_account(conn) != scope.scope.account_id:
        raise ReceivedMessageScopeChangedError
    return accepted


def _publish_received_reactions(
    conn: sqlite3.Connection, item: _message_contracts.ExtractedMessage, boundary: ReactionObservationBoundary | None
) -> None:
    if not item.reactions_observed or boundary is None:
        return
    replace_reaction_aggregates(
        conn,
        item.message.dialog_id,
        item.message.message_id,
        tuple(ReactionAggregate(emoji=row.emoji, count=row.count) for row in item.reactions),
        source=boundary.source,
        observed_at=boundary.observed_at,
        observation_sequence=boundary.sequence,
        can_see_list=item.reaction_can_see_list,
    )


def _received_message_eligible(
    conn: sqlite3.Connection,
    received: _message_contracts.ReceivedMessage,
    order: int,
    expected_dialog_id: int | None,
    revalidated_access: bool,
) -> bool:
    dialog_id = received.bundle.message.dialog_id
    if expected_dialog_id is not None and expected_dialog_id != dialog_id:
        return False
    row = cast(
        tuple[str, int | None] | None,
        conn.execute(
            "SELECT s.status,e.enabled FROM synced_dialogs s LEFT JOIN full_history_enrollment e USING(dialog_id) "
            "WHERE s.dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None:
        return False
    if row[0] == "access_lost":
        if not revalidated_access or expected_dialog_id != dialog_id or row[1] != 1:
            return False
        return _received_recovery_witness(conn, dialog_id, order)
    if row[1] == 1:
        return True
    return row[0] == "own_only" and _received_own_message_eligible(conn, received)


def _received_own_message_eligible(conn: sqlite3.Connection, received: _message_contracts.ReceivedMessage) -> bool:
    if received.outgoing is not True:
        return False
    current = read_message_out(conn, received.bundle.message.dialog_id, received.bundle.message.message_id)
    return not current.found or current.outgoing


def _received_recovery_witness(conn: sqlite3.Connection, dialog_id: int, order: int) -> bool:
    witnesses = cast(
        list[tuple[str, int]],
        conn.execute(
            "SELECT field,observation_order FROM dialog_fact_observations WHERE dialog_id=? "
            "AND field IN ('access_state','access_recovery_witness')",
            (dialog_id,),
        ).fetchall(),
    )
    return dict(witnesses) == {"access_state": order, "access_recovery_witness": order}


def _persist_received_message(
    conn: sqlite3.Connection, item: _message_contracts.ExtractedMessage, account_id: int
) -> int:
    message = item.message
    state = cast(
        tuple[int, int | None] | None,
        conn.execute(
            "SELECT o.observation_order,m.edit_date FROM message_observations o "
            "JOIN messages m USING(dialog_id,message_id) WHERE o.dialog_id=? AND o.message_id=?",
            (message.dialog_id, message.message_id),
        ).fetchone(),
    )
    if has_nonchannel_deletion(conn, message.dialog_id, message.message_id):
        return 0
    if _received_body_superseded(state, item):
        return 0
    item = _preserve_stored_rich(conn, replace(item, invalidate_image_source=False))
    message = item.message
    current = read_message_text(conn, message.dialog_id, message.message_id)
    if current.found and (current.text != message.text or not _received_body_unchanged(conn, item)):
        result = persist_edited_message(
            conn,
            item,
            old_text=current.text,
            edit_date=message.edit_date,
            reaction_source=ReactionAggregateSource.HISTORY,
            profile_owner_account_id=account_id,
        )
        return int(
            result.version is not None
            or read_message_text(conn, message.dialog_id, message.message_id).text == message.text
        )
    return insert_messages_with_fts(conn, [item], profile_owner_account_id=account_id).accepted_count


def _received_body_superseded(state: tuple[int, int | None] | None, item: _message_contracts.ExtractedMessage) -> bool:
    if state is None or item.observation_order is None or state[0] <= item.observation_order:
        return False
    version = item.message.edit_date
    return state[1] is None or version is None or version <= state[1]


def _preserve_newer_received_media(
    conn: sqlite3.Connection, item: _message_contracts.ExtractedMessage
) -> _message_contracts.ExtractedMessage:
    message = item.message
    key = (message.dialog_id, message.message_id)
    current = cast(
        tuple[str | None, str | None, int | None, int, int, int | None, int | None] | None,
        conn.execute(
            "SELECT m.media_kind,m.media_payload,s.photo_id,s.eligible,s.observation_order,"
            "o.observation_order,m.edit_date "
            "FROM messages m JOIN message_image_sources s USING(dialog_id,message_id) "
            "LEFT JOIN message_observations o USING(dialog_id,message_id) "
            "WHERE m.dialog_id=? AND m.message_id=?",
            key,
        ).fetchone(),
    )
    if current is None:
        return item
    if current[5] is not None and _received_body_unchanged(conn, item):
        item = replace(item, body_observation_order=current[5])
    unchanged = (message.media_kind, message.media_payload, item.private_photo_id, item.private_photo_eligible) == (
        current[0],
        current[1],
        current[2],
        bool(current[3]),
    )
    invalidate = _image_source_invalidation_requested(item, unchanged)
    if item.observation_order is None or current[4] <= item.observation_order:
        return replace(item, invalidate_image_source=invalidate)
    if (
        current[4] == current[5]
        and current[6] is not None
        and message.edit_date is not None
        and message.edit_date > current[6]
    ):
        return replace(
            item,
            media_observation_order=current[4],
            invalidate_image_source=invalidate,
        )
    return replace(
        item,
        message=replace(message, media_kind=current[0], media_payload=current[1]),
        private_photo_id=current[2],
        private_photo_eligible=bool(current[3]),
        media_observation_order=current[4],
    )


def _image_source_invalidation_requested(item: _message_contracts.ExtractedMessage, unchanged: bool) -> bool:
    return item.invalidate_image_source or not unchanged


def preserve_rich_observation(
    item: _message_contracts.ExtractedMessage,
    previous: tuple[str | None, str | None, str | None, int | None],
    previous_text: str | None,
) -> _message_contracts.ExtractedMessage:
    message = item.message
    incoming_less_complete = message.rich_message is None or (
        decode_rich_message(message.rich_message)[1] == "partial" and decode_rich_message(previous[0])[1] == "complete"
    )
    if (
        incoming_less_complete
        and previous[0] is not None
        and message.edit_date == previous[3]
        and message.formatting_text is not None
        and message.formatting_text == previous[1]
    ):
        return replace(item, message=replace(message, rich_message=previous[0], text=previous_text))
    return item


def _preserve_stored_rich(
    conn: sqlite3.Connection, item: _message_contracts.ExtractedMessage
) -> _message_contracts.ExtractedMessage:
    message = item.message
    row = cast(
        tuple[str | None, str | None, str | None, int | None, str | None] | None,
        conn.execute(
            "SELECT rich_message,formatting_text,formatting_entities,edit_date,text FROM messages WHERE dialog_id=? AND message_id=?",
            (message.dialog_id, message.message_id),
        ).fetchone(),
    )
    return item if row is None else preserve_rich_observation(item, row[:4], row[4])


def _received_body_unchanged(conn: sqlite3.Connection, item: _message_contracts.ExtractedMessage) -> bool:
    """Keep body ordering separate when a complete response only changed media."""
    message = item.message
    if message.edit_date is None:
        return False
    key = (message.dialog_id, message.message_id)
    row = cast(
        tuple[object, ...] | sqlite3.Row | None,
        conn.execute(
            "SELECT text,edit_date,formatting_entities,rich_message,formatting_text,service_action,reply_to_msg_id,forum_topic_id,"
            "grouped_id,reply_to_peer_id,is_service,post_author FROM messages WHERE dialog_id=? AND message_id=?",
            key,
        ).fetchone(),
    )
    if row is None or tuple(row) != (
        message.text,
        message.edit_date,
        message.formatting_entities,
        message.rich_message,
        message.formatting_text,
        message.service_action,
        message.reply_to_msg_id,
        message.forum_topic_id,
        message.grouped_id,
        message.reply_to_peer_id,
        message.is_service,
        message.post_author,
    ):
        return False
    entities = cast(
        list[tuple[int, int, str, str | None] | sqlite3.Row],
        conn.execute(
            "SELECT offset,length,type,value FROM message_entities WHERE dialog_id=? AND message_id=? ORDER BY offset,length,type,value",
            key,
        ).fetchall(),
    )
    expected = sorted(
        ((entity.offset, entity.length, entity.type, entity.value) for entity in item.entities),
        key=lambda value: (value[0], value[1], value[2], value[3] is not None, value[3] or ""),
    )
    if [tuple(entity) for entity in entities] != expected:
        return False
    forward = cast(
        tuple[object, ...] | sqlite3.Row | None,
        conn.execute(
            "SELECT fwd_from_peer_id,fwd_from_name,fwd_date,fwd_channel_post FROM message_forwards WHERE dialog_id=? AND message_id=?",
            key,
        ).fetchone(),
    )
    if item.forward is None:
        return forward is None
    return forward is not None and tuple(forward) == (
        item.forward.fwd_from_peer_id,
        item.forward.fwd_from_name,
        item.forward.fwd_date,
        item.forward.fwd_channel_post,
    )


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
