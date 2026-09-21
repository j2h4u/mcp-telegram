"""Transaction-neutral SQLite persistence for canonical message bundles."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, fields, replace
from typing import cast

from .. import message_contracts as _message_contracts
from ..alert_policy import incoming_human_dm_sql
from ..dialog_classification import is_bot_dialog_type
from ..fts import DELETE_FTS_SQL, INSERT_FTS_SQL, stem_text
from ..hydration_queue import HydrationPriority, HydrationQueueRepository
from ..media_fact import decode_media_fact, is_transcribable_telegram_media
from ..message_history.contracts import MESSAGE_HISTORY_PAGE_LIMIT, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION
from ..reactions.contracts import ReactionAggregate, ReactionAggregateSource
from ..reactions.persistence import replace_reaction_aggregates
from ..telegram_rpc_consumers import topic_attribution_campaign_lifetime, topic_attribution_failure_delay
from .sqlite_hydration_jobs import _FACT_HYDRATION_EMPTY_KINDS, _is_canonical_media_pair, reconcile_fact_hydration_job

SQLiteConnection = sqlite3.Connection


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
_SELECT_MESSAGE_TRANSCRIPTION_SQL = (
    "SELECT text, transcription_id FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?"
)
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


def persist_edited_message(  # noqa: PLR0913
    conn: sqlite3.Connection,
    extracted: _message_contracts.ExtractedMessage,
    *,
    old_text: str | None,
    edit_date: int,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
    reaction_source: ReactionAggregateSource | str = ReactionAggregateSource.MESSAGE_EDIT,
) -> int | None:
    """Version and persist a changed message in the caller's transaction."""
    dialog_id, message_id = extracted.message.dialog_id, extracted.message.message_id
    current = read_message_text(conn, dialog_id, message_id)
    if not current.found or current.text == extracted.message.text:
        return None
    old_text = current.text
    keep_history = conn.execute(_SELECT_HUMAN_DM_MESSAGE_SQL, (dialog_id, message_id)).fetchone() is not None
    if not keep_history:
        insert_messages_with_fts(
            conn, [extracted], priority=priority, reaction_source=reaction_source, reaction_observed_at=edit_date
        )
        return None
    version_row = cast(tuple[int], conn.execute(_NEXT_VERSION_SQL, (dialog_id, message_id)).fetchone())
    next_version = int(version_row[0])
    conn.execute(_INSERT_VERSION_SQL, (dialog_id, message_id, next_version, old_text, edit_date, "telegram_edit"))
    insert_messages_with_fts(
        conn, [extracted], priority=priority, reaction_source=reaction_source, reaction_observed_at=edit_date
    )
    conn.execute(
        _INSERT_HUMAN_DM_EDIT_ALERT_SQL,
        (edit_date, dialog_id, message_id, next_version, dialog_id, message_id),
    )
    return next_version


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
    cursor = conn.execute(_MARK_DELETED_SQL, (deleted_at, dialog_id, message_id))
    if cursor.rowcount > 0:
        HydrationQueueRepository(conn).remove_for_message(dialog_id, message_id)
    return cursor.rowcount > 0


def find_unique_incoming_human_dm_dialogs(conn: sqlite3.Connection, message_ids: Sequence[int]) -> dict[int, int]:
    """Resolve peer-less Telegram deletions whose local DM rows are unique."""
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
                  AND {incoming_human_dm_sql("m")}""",
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


def insert_messages_with_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.FOREGROUND,
    reaction_source: ReactionAggregateSource | str = ReactionAggregateSource.HISTORY,
    reaction_observed_at: int | None = None,
) -> None:
    """Persist message bundles in the caller-owned transaction."""
    projected = _overlay_message_transcriptions(conn, _preserve_transcribed_texts(conn, extracted))
    _write_message_rows_and_fts(conn, projected, priority=priority)
    _delete_entity_and_forward_projections(conn, projected)
    _replace_reaction_projections(conn, projected, source=reaction_source, observed_at=reaction_observed_at)
    _insert_entity_and_forward_projections(conn, projected)


def _write_message_rows_and_fts(
    conn: sqlite3.Connection,
    extracted: Sequence[_message_contracts.ExtractedMessage],
    *,
    priority: HydrationPriority = HydrationPriority.BACKFILL,
) -> None:
    messages = [item.message for item in extracted]
    conn.executemany(
        _INSERT_MESSAGE_SQL, [{**asdict(item.message), "reply_count": item.reply_count} for item in extracted]
    )
    conn.executemany(DELETE_FTS_SQL, ((item.dialog_id, item.message_id) for item in messages))
    conn.executemany(INSERT_FTS_SQL, ((item.dialog_id, item.message_id, stem_text(item.text)) for item in messages))
    for message in messages:
        reconcile_fact_hydration_job(conn, message, due_at=int(time.time()), priority=priority)


def _overlay_message_transcriptions(
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> list[_message_contracts.ExtractedMessage]:
    projected: list[_message_contracts.ExtractedMessage] = []
    for item in extracted:
        dialog_id, message_id = item.message.dialog_id, item.message.message_id
        row = cast(
            tuple[str, int] | None, conn.execute(_SELECT_MESSAGE_TRANSCRIPTION_SQL, (dialog_id, message_id)).fetchone()
        )
        if row is None:
            projected.append(item)
            continue
        fact = decode_media_fact(item.message.media_kind, item.message.media_payload)
        if _is_canonical_media_pair(
            item.message.media_kind, item.message.media_payload, fact=fact
        ) and is_transcribable_telegram_media(fact):
            projected.append(replace(item, message=replace(item.message, text=row[0])))
            continue
        if _is_canonical_media_pair(item.message.media_kind, item.message.media_payload, fact=fact):
            conn.execute(
                "DELETE FROM message_transcriptions WHERE dialog_id = ? AND message_id = ?", (dialog_id, message_id)
            )
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
        replace_reaction_aggregates(
            conn,
            item.message.dialog_id,
            item.message.message_id,
            tuple(ReactionAggregate(emoji=row.emoji, count=row.count) for row in item.reactions),
            source=source,
            observed_at=observed_at,
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
    conn: sqlite3.Connection, extracted: Sequence[_message_contracts.ExtractedMessage]
) -> list[_message_contracts.ExtractedMessage]:
    preserved_texts: dict[tuple[int, int], str] = {}
    for item in extracted:
        if item.message.text is not None and item.message.text.strip():
            continue
        if item.message.media_kind not in _FACT_HYDRATION_EMPTY_KINDS and item.message.media_kind != "voice":
            continue
        row = cast(
            tuple[str | None] | None,
            conn.execute(_SELECT_MESSAGE_TEXT_SQL, (item.message.dialog_id, item.message.message_id)).fetchone(),
        )
        if row is not None and row[0]:
            preserved_texts[(item.message.dialog_id, item.message.message_id)] = row[0]
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


# Temporary topic-attribution repair state. PR2 removes this section and its daemon_state row.

logger = logging.getLogger("mcp_telegram.topic_attribution_campaign")

CAMPAIGN_STATE_KEY = "topic_attribution_campaign_v1"
CAMPAIGN_VERSION = 1
CAMPAIGN_DIALOG_COUNT = 2
CAMPAIGN_MAX_FAILURES_PER_DIALOG = 16
_COUNTER_KEYS = ("attributed", "no_topic", "no_longer_needed", "unresolved")
_TERMINAL_REASON_PRIORITY = {
    "exhausted": 0,
    "expiry": 1,
    "status_ineligible": 2,
    "access_lost": 3,
    "history_disabled": 3,
    "failure_limit": 4,
    "invalid_manifest": 5,
    "incompatible_manifest": 5,
    "operator_abort": 6,
}
_TERMINAL_SEVERITY = {
    "exhausted": "complete",
    "status_ineligible": "degraded",
    "access_lost": "degraded",
    "expiry": "degraded",
    "failure_limit": "failed",
    "invalid_manifest": "failed",
    "incompatible_manifest": "failed",
    "operator_abort": "abandoned",
    "history_disabled": "abandoned",
}


class TopicAttributionCampaignError(ValueError):
    """The explicitly limited campaign cannot continue as requested."""


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNTER_KEYS, 0)


def _encode(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True)


def _load(conn: sqlite3.Connection) -> dict[str, object] | None:
    try:
        row = cast(
            tuple[object] | None,
            conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone(),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc).lower():
            raise
        return None
    if row is None or not isinstance(row[0], str):
        return None
    try:
        raw = cast(object, json.loads(row[0]))
    except json.JSONDecodeError as exc:
        raise TopicAttributionCampaignError("invalid_manifest") from exc
    if not isinstance(raw, dict) or raw.get("version") != CAMPAIGN_VERSION:
        raise TopicAttributionCampaignError("incompatible_manifest")
    manifest = cast(dict[str, object], raw)
    if not _valid_manifest_shape(manifest):
        raise TopicAttributionCampaignError("invalid_manifest")
    return manifest


def _valid_manifest_shape(manifest: dict[str, object]) -> bool:
    dialog_ids = manifest.get("dialog_ids")
    dialogs = manifest.get("dialogs")
    return _valid_manifest_header(manifest, dialog_ids, dialogs) and _valid_manifest_dialogs(dialog_ids, dialogs)


def _valid_manifest_header(manifest: dict[str, object], dialog_ids: object, dialogs: object) -> bool:
    if (
        manifest.get("state") not in {"active", "complete"}
        or not isinstance(dialog_ids, list)
        or not isinstance(dialogs, dict)
    ):
        return False
    if manifest.get("state") != "active":
        return True
    return len(dialog_ids) == CAMPAIGN_DIALOG_COUNT and _nonnegative_int(manifest.get("expires_at"))


def _nonnegative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_manifest_dialogs(dialog_ids: object, dialogs: object) -> bool:
    if not isinstance(dialog_ids, list) or not isinstance(dialogs, dict):
        return False
    return all(_valid_manifest_dialog(dialog_id, dialogs.get(str(dialog_id))) for dialog_id in dialog_ids)


def _valid_manifest_dialog(dialog_id: object, item: object) -> bool:
    return _valid_dialog_identity(dialog_id, item) and _valid_dialog_progress(item) and _valid_dialog_counts(item)


def _valid_dialog_identity(dialog_id: object, item: object) -> bool:
    return not isinstance(dialog_id, bool) and isinstance(dialog_id, int) and isinstance(item, dict)


def _valid_dialog_progress(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    retry_at = item.get("next_retry_at")
    return (
        item.get("state") in {"pending", "done"}
        and _nonnegative_int(item.get("cursor"))
        and _nonnegative_int(item.get("failure_attempts"))
        and (retry_at is None or _nonnegative_int(retry_at))
    )


def _valid_dialog_counts(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    counts = item.get("counts")
    return isinstance(counts, dict) and all(_nonnegative_int(counts.get(key)) for key in _COUNTER_KEYS)


def _save(conn: sqlite3.Connection, manifest: dict[str, object]) -> None:
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (CAMPAIGN_STATE_KEY, _encode(manifest)),
    )


def enroll_campaign(
    conn: sqlite3.Connection, dialog_ids: Sequence[int], *, now: int | None = None
) -> dict[str, object]:
    """Persist the fixed two-dialog manifest after local eligibility checks."""
    ids = _normalized_campaign_ids(dialog_ids)
    observed_at = int(time.time()) if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _load(conn)
        if existing is not None:
            if existing.get("state") == "active" and existing.get("dialog_ids") == ids:
                conn.commit()
                return existing
            raise TopicAttributionCampaignError("a topic-attribution campaign already exists")
        _require_synced_bot_dialogs(conn, ids)
        manifest = _new_campaign_manifest(ids, observed_at)
        _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("topic_attribution_campaign_enrolled dialog_count=%d", CAMPAIGN_DIALOG_COUNT)
    return manifest


def _normalized_campaign_ids(dialog_ids: Sequence[int]) -> list[int]:
    if len(dialog_ids) != CAMPAIGN_DIALOG_COUNT or any(isinstance(value, bool) for value in dialog_ids):
        raise TopicAttributionCampaignError("exactly two dialog ids are required")
    ids = sorted(set(dialog_ids))
    if len(ids) != CAMPAIGN_DIALOG_COUNT:
        raise TopicAttributionCampaignError("the two dialog ids must be distinct")
    return ids


def _require_synced_bot_dialogs(conn: sqlite3.Connection, ids: Sequence[int]) -> None:
    rows = cast(
        list[tuple[object, object]],
        conn.execute(
            "SELECT sd.dialog_id,d.type FROM synced_dialogs sd JOIN dialogs d USING(dialog_id) "
            "WHERE sd.dialog_id IN (?,?) AND sd.status='synced' AND d.hidden=0",
            tuple(ids),
        ).fetchall(),
    )
    if len(rows) != CAMPAIGN_DIALOG_COUNT or any(not is_bot_dialog_type(row[1]) for row in rows):
        raise TopicAttributionCampaignError("each enrolled dialog must currently be a synced bot dialog")


def _new_campaign_manifest(ids: list[int], observed_at: int) -> dict[str, object]:
    return {
        "version": CAMPAIGN_VERSION,
        "state": "active",
        "created_at": observed_at,
        "expires_at": observed_at + topic_attribution_campaign_lifetime(),
        "max_failures_per_dialog": CAMPAIGN_MAX_FAILURES_PER_DIALOG,
        "dialog_ids": ids,
        "dialogs": {
            str(dialog_id): {
                "cursor": 0,
                "failure_attempts": 0,
                "next_retry_at": None,
                "state": "pending",
                "counts": _empty_counts(),
            }
            for dialog_id in ids
        },
    }


def reset_campaign(conn: sqlite3.Connection) -> dict[str, object]:
    """Remove only a terminal manifest before an explicit fresh enrollment."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest = _load(conn)
        if manifest is None:
            raise TopicAttributionCampaignError("campaign_not_found")
        if manifest.get("state") != "complete":
            raise TopicAttributionCampaignError("campaign_is_not_terminal")
        reason = str(manifest.get("terminal_reason") or "exhausted")
        conn.execute("DELETE FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("topic_attribution_campaign_reset prior_terminal_reason=%s", reason)
    return {"previous_terminal_reason": reason}


def campaign_release_at(conn: sqlite3.Connection, *, now: int) -> float | None:
    """Pure readiness projection for the durable adapter status method."""
    try:
        manifest = _load(conn)
    except TopicAttributionCampaignError:
        return 0.0
    if manifest is None or manifest.get("state") != "active":
        return None
    if int(cast(int | str, manifest["expires_at"])) <= now:
        return 0.0
    scheduled_at: list[float] = []
    for _dialog_id, item in _pending_items(manifest):
        retry_at = item.get("next_retry_at")
        if retry_at is None or int(cast(int | str, retry_at)) <= now:
            return 0.0
        scheduled_at.append(float(cast(int | str, retry_at)))
    return min(scheduled_at, default=0.0)


def advance_campaign(conn: sqlite3.Connection, *, now: int) -> tuple[int, int] | None:
    """Mutate only to quarantine, terminalize, or select a ready local dialog."""
    try:
        manifest = _load(conn)
    except TopicAttributionCampaignError as exc:
        _quarantine_invalid(conn, str(exc), now)
        return None
    if manifest is None or manifest.get("state") != "active":
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        result, terminal_reason = _advance_active_manifest(conn, manifest, now)
        if terminal_reason is not None:
            _finish(conn, manifest, terminal_reason, now)
        else:
            _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    if terminal_reason is not None:
        _log_terminal(manifest)
    return result


def _advance_active_manifest(
    conn: sqlite3.Connection, manifest: dict[str, object], now: int
) -> tuple[tuple[int, int] | None, str | None]:
    if int(cast(int | str, manifest["expires_at"])) <= now:
        return None, "expiry"
    candidate = _select_ready_campaign_item(conn, manifest, now)
    if candidate is not None:
        return candidate, None
    return None, "exhausted" if _next_pending(manifest) is None else None


def _select_ready_campaign_item(
    conn: sqlite3.Connection, manifest: dict[str, object], now: int
) -> tuple[int, int] | None:
    for dialog_id, item in _pending_items(manifest):
        if _is_delayed(item, now):
            continue
        status = _campaign_dialog_status(conn, dialog_id)
        if status == "synced":
            return dialog_id, int(cast(int | str, item["cursor"]))
        _abandon_ineligible_item(item, status)
    return None


def _is_delayed(item: dict[str, object], now: int) -> bool:
    retry_at = item.get("next_retry_at")
    return retry_at is not None and int(cast(int | str, retry_at)) > now


def _campaign_dialog_status(conn: sqlite3.Connection, dialog_id: int) -> object:
    row = cast(
        tuple[object, object] | None,
        conn.execute(
            "SELECT sd.status,d.hidden FROM synced_dialogs sd JOIN dialogs d USING(dialog_id) WHERE sd.dialog_id=?",
            (dialog_id,),
        ).fetchone(),
    )
    if row is None or row[1] != 0:
        return None
    return row[0]


def campaign_dialog_visible(conn: sqlite3.Connection, dialog_id: int) -> bool:
    """Return whether canonical full-history eligibility still permits execution."""
    return _campaign_dialog_status(conn, dialog_id) == "synced"


def _abandon_ineligible_item(item: dict[str, object], status: object) -> None:
    reason = "access_lost" if status == "access_lost" else "status_ineligible"
    item.update(state="done", last_error=reason, terminal_reason=reason)


def record_page(
    conn: sqlite3.Connection,
    dialog_id: int,
    checkpoint: int,
    messages: Sequence[_message_contracts.ExtractedMessage],
    *,
    observed_at: int,
) -> dict[str, object]:
    """Apply one page through the narrow live-NULL topic projection."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest, item = _require_active_dialog(conn, dialog_id, checkpoint)
        counts = cast(dict[str, int], item["counts"])
        previous_no_topic = counts["no_topic"]
        _reconcile_page_messages(conn, dialog_id, messages, counts)
        _advance_page_checkpoint(item, messages)
        no_topic_delta = counts["no_topic"] - previous_no_topic
        _record_campaign_no_topic_receipt(conn, dialog_id, observed_at, no_topic_delta)
        if item["state"] == "done":
            _publish_campaign_dialog_receipt(conn, dialog_id, counts, observed_at)
        terminal = _next_pending(manifest) is None
        if terminal:
            _finish(conn, manifest, "exhausted", observed_at)
        else:
            _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    counts = cast(dict[str, int], item["counts"])
    logger.info(
        "topic_attribution_campaign_page_result attributed=%d no_topic=%d no_longer_needed=%d unresolved=%d",
        counts["attributed"],
        counts["no_topic"],
        counts["no_longer_needed"],
        counts["unresolved"],
    )
    if terminal:
        _log_terminal(manifest)
    return manifest


def _reconcile_page_messages(
    conn: sqlite3.Connection,
    dialog_id: int,
    messages: Sequence[_message_contracts.ExtractedMessage],
    counts: dict[str, int],
) -> None:
    for extracted in messages:
        message = extracted.message
        local = cast(
            tuple[object, object] | None,
            conn.execute(
                "SELECT forum_topic_id,is_deleted FROM messages WHERE dialog_id=? AND message_id=?",
                (dialog_id, message.message_id),
            ).fetchone(),
        )
        if local is None:
            counts["unresolved"] += 1
        elif local[0] is not None or local[1] != 0:
            counts["no_longer_needed"] += 1
        elif message.forum_topic_id is None:
            counts["no_topic"] += 1
        else:
            updated = conn.execute(
                "UPDATE messages SET forum_topic_id=? WHERE dialog_id=? AND message_id=? "
                "AND forum_topic_id IS NULL AND is_deleted=0",
                (message.forum_topic_id, dialog_id, message.message_id),
            ).rowcount
            counts["attributed" if updated else "unresolved"] += 1


def _advance_page_checkpoint(item: dict[str, object], messages: Sequence[_message_contracts.ExtractedMessage]) -> None:
    item["failure_attempts"] = 0
    item["next_retry_at"] = None
    if messages:
        item["cursor"] = min(extracted.message.message_id for extracted in messages)
    if not messages or len(messages) < MESSAGE_HISTORY_PAGE_LIMIT:
        item["state"] = "done"


def _record_campaign_no_topic_receipt(
    conn: sqlite3.Connection, dialog_id: int, observed_at: int, no_topic_delta: int
) -> None:
    """Publish a partial campaign receipt and its evaluated legal-NULL outcomes."""
    conn.execute(
        "UPDATE synced_dialogs SET topic_attribution_version=?, topic_attribution_state='partial', "
        "topic_attribution_observed_at=?, topic_attribution_completed_at=NULL, "
        "topic_attribution_no_topic_count=CASE "
        "WHEN topic_attribution_version=? AND topic_attribution_state='partial' "
        "THEN topic_attribution_no_topic_count+? ELSE ? END WHERE dialog_id=?",
        (
            TOPIC_ATTRIBUTION_EXTRACTOR_VERSION,
            observed_at,
            TOPIC_ATTRIBUTION_EXTRACTOR_VERSION,
            no_topic_delta,
            no_topic_delta,
            dialog_id,
        ),
    )


def _publish_campaign_dialog_receipt(
    conn: sqlite3.Connection, dialog_id: int, counts: dict[str, int], observed_at: int
) -> None:
    """Certify this clean traversal without waiting for the other enrolled dialog."""
    if counts["unresolved"]:
        return
    conn.execute(
        "UPDATE synced_dialogs SET topic_attribution_state='complete', topic_attribution_completed_at=? "
        "WHERE dialog_id=? AND topic_attribution_version=? AND topic_attribution_state='partial'",
        (observed_at, dialog_id, TOPIC_ATTRIBUTION_EXTRACTOR_VERSION),
    )


def record_deferred(
    conn: sqlite3.Connection, dialog_id: int, checkpoint: int, *, reason: str, observed_at: int
) -> None:
    """Keep the checkpoint while recording governed deferral without a failure burn."""
    _record_error(
        conn, dialog_id, checkpoint, reason=reason, observed_at=observed_at, retry_at=None, count_failure=False
    )


def record_failed_attempt(
    conn: sqlite3.Connection, dialog_id: int, checkpoint: int, *, reason: str, observed_at: int
) -> None:
    """Bound ordinary transport failures while keeping a resumable cursor."""
    _record_error(
        conn,
        dialog_id,
        checkpoint,
        reason=reason,
        observed_at=observed_at,
        retry_at=observed_at + topic_attribution_failure_delay(),
        count_failure=True,
    )


def record_access_lost(
    conn: sqlite3.Connection, dialog_id: int, checkpoint: int, *, observed_at: int, reason: str = "access_lost"
) -> None:
    """Exclude a newly inaccessible dialog before another page can be acquired."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest, item = _require_active_dialog(conn, dialog_id, checkpoint)
        item["state"] = "done"
        item["last_error"] = reason
        item["terminal_reason"] = reason
        terminal = _next_pending(manifest) is None
        if terminal:
            _finish(conn, manifest, "exhausted", observed_at)
        else:
            _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("topic_attribution_campaign_page_failure reason=%s", reason)
    if terminal:
        _log_terminal(manifest)


def abort_campaign(conn: sqlite3.Connection, *, observed_at: int | None = None) -> dict[str, object]:
    """Terminalize an active enrollment so an operator can reset it safely."""
    now = int(time.time()) if observed_at is None else observed_at
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest = _load(conn)
        if manifest is None:
            raise TopicAttributionCampaignError("campaign_not_found")
        if manifest.get("state") != "active":
            raise TopicAttributionCampaignError("campaign_is_not_active")
        _finish(conn, manifest, "operator_abort", now)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.warning("topic_attribution_campaign_aborted")
    _log_terminal(manifest)
    return {"terminal_reason": "operator_abort"}


def campaign_status(conn: sqlite3.Connection) -> dict[str, object]:
    """Return privacy-safe operator progress without exposing enrolled ids."""
    try:
        manifest = _load(conn)
    except TopicAttributionCampaignError as exc:
        return {
            "state": "invalid",
            "terminal_reason": str(exc),
            "terminal_severity": "failed",
            "dialog_count": 0,
            "pending_dialogs": 0,
            "failed_dialogs": 0,
            "abandoned_dialogs": 0,
            "counts": _empty_counts(),
        }
    if manifest is None:
        return {
            "state": "none",
            "terminal_reason": None,
            "terminal_severity": "none",
            "dialog_count": 0,
            "pending_dialogs": 0,
            "failed_dialogs": 0,
            "abandoned_dialogs": 0,
            "counts": _empty_counts(),
        }
    return _status_from_manifest(manifest)


def _record_error(  # noqa: PLR0913
    conn: sqlite3.Connection,
    dialog_id: int,
    checkpoint: int,
    *,
    reason: str,
    observed_at: int,
    retry_at: int | None,
    count_failure: bool,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest, item = _require_active_dialog(conn, dialog_id, checkpoint)
        item["last_error"] = reason
        item["next_retry_at"] = retry_at
        if count_failure:
            item["failure_attempts"] = int(cast(int | str, item["failure_attempts"])) + 1
            if int(cast(int | str, item["failure_attempts"])) >= int(
                cast(int | str, manifest["max_failures_per_dialog"])
            ):
                item["state"] = "done"
                item["terminal_reason"] = "failure_limit"
        terminal = _next_pending(manifest) is None
        if terminal:
            _finish(conn, manifest, "failure_limit", observed_at)
        else:
            _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("topic_attribution_campaign_page_failure reason=%s", reason)
    if terminal:
        _log_terminal(manifest)


def _require_active_dialog(
    conn: sqlite3.Connection, dialog_id: int, checkpoint: int
) -> tuple[dict[str, object], dict[str, object]]:
    manifest = _load(conn)
    if manifest is None or manifest.get("state") != "active":
        raise TopicAttributionCampaignError("campaign_inactive")
    dialogs = manifest.get("dialogs")
    if not isinstance(dialogs, dict):
        raise TopicAttributionCampaignError("invalid_manifest")
    item = dialogs.get(str(dialog_id))
    if (
        not isinstance(item, dict)
        or item.get("state") != "pending"
        or int(cast(int | str, item.get("cursor", -1))) != checkpoint
    ):
        raise TopicAttributionCampaignError("checkpoint_changed")
    return manifest, cast(dict[str, object], item)


def _pending_items(manifest: dict[str, object]) -> list[tuple[int, dict[str, object]]]:
    dialogs = manifest.get("dialogs")
    if not isinstance(dialogs, dict):
        return []
    pending: list[tuple[int, dict[str, object]]] = []
    for dialog_id in cast(list[int], manifest.get("dialog_ids", [])):
        item = dialogs.get(str(dialog_id))
        if isinstance(item, dict) and item.get("state") == "pending":
            pending.append((dialog_id, cast(dict[str, object], item)))
    return pending


def _next_pending(manifest: dict[str, object]) -> tuple[int, dict[str, object]] | None:
    return next(iter(_pending_items(manifest)), None)


def _finish(conn: sqlite3.Connection, manifest: dict[str, object], reason: str, completed_at: int) -> None:
    for item in cast(dict[str, dict[str, object]], manifest["dialogs"]).values():
        if item.get("state") == "pending":
            item["state"] = "done"
    manifest["state"] = "complete"
    manifest["terminal_reason"] = _most_severe_terminal_reason(manifest, reason)
    manifest["completed_at"] = completed_at
    _save(conn, manifest)


def _quarantine_invalid(conn: sqlite3.Connection, reason: str, observed_at: int) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        _save(
            conn,
            {
                "version": CAMPAIGN_VERSION,
                "state": "complete",
                "terminal_reason": reason,
                "completed_at": observed_at,
                "dialog_ids": [],
                "dialogs": {},
            },
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.warning("topic_attribution_campaign_quarantined reason=%s", reason)


def _status_from_manifest(manifest: dict[str, object]) -> dict[str, object]:
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    totals = _empty_counts()
    for item in dialogs.values():
        item_counts = cast(dict[str, object], item.get("counts", {}))
        for key in _COUNTER_KEYS:
            totals[key] += int(cast(int | str, item_counts.get(key, 0)))
    terminal_reason = manifest.get("terminal_reason")
    failed_dialogs = sum(item.get("terminal_reason") == "failure_limit" for item in dialogs.values())
    abandoned_dialogs = sum(
        item.get("terminal_reason") in {"access_lost", "history_disabled", "status_ineligible", "operator_abort"}
        for item in dialogs.values()
    )
    return {
        "state": manifest.get("state"),
        "terminal_reason": terminal_reason,
        "terminal_severity": _terminal_severity(terminal_reason, totals, failed_dialogs, abandoned_dialogs),
        "dialog_count": len(dialogs),
        "pending_dialogs": sum(item.get("state") == "pending" for item in dialogs.values()),
        "failed_dialogs": failed_dialogs,
        "abandoned_dialogs": abandoned_dialogs,
        "counts": totals,
    }


def _terminal_severity(
    terminal_reason: object, totals: dict[str, int], failed_dialogs: int, abandoned_dialogs: int
) -> str:
    if terminal_reason == "exhausted" and (totals["unresolved"] or failed_dialogs or abandoned_dialogs):
        return "degraded"
    return _TERMINAL_SEVERITY.get(str(terminal_reason), "none")


def _log_terminal(manifest: dict[str, object]) -> None:
    status = _status_from_manifest(manifest)
    counts = cast(dict[str, int], status["counts"])
    logger.info(
        "topic_attribution_campaign_terminal reason=%s severity=%s attributed=%d no_topic=%d no_longer_needed=%d unresolved=%d",
        status["terminal_reason"],
        status["terminal_severity"],
        counts["attributed"],
        counts["no_topic"],
        counts["no_longer_needed"],
        counts["unresolved"],
    )


def _most_severe_terminal_reason(manifest: dict[str, object], requested: str) -> str:
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    reasons = [requested]
    reasons.extend(str(item["terminal_reason"]) for item in dialogs.values() if "terminal_reason" in item)
    return max(reasons, key=lambda reason: _TERMINAL_REASON_PRIORITY.get(reason, 0))
