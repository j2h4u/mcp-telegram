"""Finite, deployment-enrolled repair of two bot-dialog topic projections.

The manifest is intentionally one small ``daemon_state`` value. It is
transitional and PR2 removes this module and its operator route after repair.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Sequence
from typing import cast

from .dialog_classification import is_bot_dialog_type
from .message_contracts import ExtractedMessage
from .message_history.contracts import HISTORY_PAGE_SIZE

logger = logging.getLogger(__name__)

CAMPAIGN_STATE_KEY = "topic_attribution_campaign_v1"
CAMPAIGN_VERSION = 1
CAMPAIGN_DIALOG_COUNT = 2
CAMPAIGN_MAX_FAILURES_PER_DIALOG = 16
CAMPAIGN_FAILURE_RETRY_SECONDS = 60
CAMPAIGN_DEADLINE_SECONDS = 7 * 24 * 60 * 60
_COUNTER_KEYS = ("attributed", "no_longer_needed", "unresolved")
_TERMINAL_REASON_PRIORITY = {
    "exhausted": 0,
    "deadline": 1,
    "status_ineligible": 2,
    "access_lost": 3,
    "failure_limit": 4,
    "invalid_manifest": 5,
    "incompatible_manifest": 5,
}
_TERMINAL_SEVERITY = {
    "exhausted": "complete",
    "status_ineligible": "degraded",
    "access_lost": "degraded",
    "deadline": "degraded",
    "failure_limit": "failed",
    "invalid_manifest": "failed",
    "incompatible_manifest": "failed",
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
    return cast(dict[str, object], raw)


def _save(conn: sqlite3.Connection, manifest: dict[str, object]) -> None:
    conn.execute(
        "INSERT INTO daemon_state(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (CAMPAIGN_STATE_KEY, _encode(manifest)),
    )


def enroll_campaign(
    conn: sqlite3.Connection, dialog_ids: Sequence[int], *, now: int | None = None
) -> dict[str, object]:
    """Persist the fixed two-dialog manifest after local eligibility checks."""
    if len(dialog_ids) != CAMPAIGN_DIALOG_COUNT or any(isinstance(value, bool) for value in dialog_ids):
        raise TopicAttributionCampaignError("exactly two dialog ids are required")
    ids = sorted(set(dialog_ids))
    if len(ids) != CAMPAIGN_DIALOG_COUNT:
        raise TopicAttributionCampaignError("the two dialog ids must be distinct")
    observed_at = int(time.time()) if now is None else now
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _load(conn)
        if existing is not None:
            if existing.get("state") == "active" and existing.get("dialog_ids") == ids:
                conn.commit()
                return existing
            raise TopicAttributionCampaignError("a topic-attribution campaign already exists")
        rows = cast(
            list[tuple[object, object]],
            conn.execute(
                "SELECT sd.dialog_id,d.type FROM synced_dialogs sd JOIN dialogs d USING(dialog_id) "
                "WHERE sd.dialog_id IN (?,?) AND sd.status='synced'",
                tuple(ids),
            ).fetchall(),
        )
        if len(rows) != CAMPAIGN_DIALOG_COUNT or any(not is_bot_dialog_type(row[1]) for row in rows):
            raise TopicAttributionCampaignError("each enrolled dialog must currently be a synced bot dialog")
        manifest: dict[str, object] = {
            "version": CAMPAIGN_VERSION,
            "state": "active",
            "created_at": observed_at,
            "deadline_at": observed_at + CAMPAIGN_DEADLINE_SECONDS,
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
        _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("topic_attribution_campaign_enrolled dialog_count=%d", CAMPAIGN_DIALOG_COUNT)
    return manifest


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
    if int(cast(int | str, manifest["deadline_at"])) <= now:
        return 0.0
    retry_times: list[float] = []
    for _dialog_id, item in _pending_items(manifest):
        retry_at = item.get("next_retry_at")
        if retry_at is None or int(cast(int | str, retry_at)) <= now:
            return 0.0
        retry_times.append(float(cast(int | str, retry_at)))
    return min(retry_times, default=0.0)


def advance_campaign(conn: sqlite3.Connection, *, now: int) -> tuple[int, int] | None:
    """Mutate only to quarantine, terminalize, or skip an ineligible dialog."""
    try:
        manifest = _load(conn)
    except TopicAttributionCampaignError as exc:
        _quarantine_invalid(conn, str(exc), now)
        return None
    if manifest is None or manifest.get("state") != "active":
        return None
    conn.execute("BEGIN IMMEDIATE")
    try:
        if int(cast(int | str, manifest["deadline_at"])) <= now:
            _finish(conn, manifest, "deadline", now)
            conn.commit()
            _log_terminal(manifest)
            return None
        while True:
            pending = _pending_items(manifest)
            if not pending:
                _finish(conn, manifest, "exhausted", now)
                conn.commit()
                _log_terminal(manifest)
                return None
            changed = False
            for dialog_id, item in pending:
                retry_at = item.get("next_retry_at")
                if retry_at is not None and int(cast(int | str, retry_at)) > now:
                    continue
                status = cast(
                    tuple[object] | None,
                    conn.execute("SELECT status FROM synced_dialogs WHERE dialog_id=?", (dialog_id,)).fetchone(),
                )
                if status is not None and status[0] == "synced":
                    conn.commit()
                    return dialog_id, int(cast(int | str, item["cursor"]))
                reason = "access_lost" if status is not None and status[0] == "access_lost" else "status_ineligible"
                item["state"] = "done"
                item["last_error"] = reason
                item["terminal_reason"] = reason
                changed = True
            if not changed:
                conn.commit()
                return None
    except BaseException:
        conn.rollback()
        raise


def record_page(
    conn: sqlite3.Connection,
    dialog_id: int,
    checkpoint: int,
    messages: Sequence[ExtractedMessage],
    *,
    observed_at: int,
) -> dict[str, object]:
    """Apply a page only with a live-NULL topic update and persist its cursor."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest, item = _require_active_dialog(conn, dialog_id, checkpoint)
        counts = cast(dict[str, int], item["counts"])
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
                continue
            if local[0] is not None or local[1] != 0:
                counts["no_longer_needed"] += 1
                continue
            if message.forum_topic_id is None:
                counts["unresolved"] += 1
                continue
            updated = conn.execute(
                "UPDATE messages SET forum_topic_id=? WHERE dialog_id=? AND message_id=? "
                "AND forum_topic_id IS NULL AND is_deleted=0",
                (message.forum_topic_id, dialog_id, message.message_id),
            ).rowcount
            counts["attributed" if updated else "unresolved"] += 1
        item["failure_attempts"] = 0
        item["next_retry_at"] = None
        if messages:
            item["cursor"] = min(message.message.message_id for message in messages)
        if not messages or len(messages) < HISTORY_PAGE_SIZE:
            item["state"] = "done"
        conn.execute(
            "UPDATE synced_dialogs SET topic_attribution_state='partial', topic_attribution_observed_at=? "
            "WHERE dialog_id=? AND topic_attribution_state='unknown'",
            (observed_at, dialog_id),
        )
        terminal = _next_pending(manifest) is None
        if terminal:
            _finish(conn, manifest, "exhausted", observed_at)
        else:
            _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info(
        "topic_attribution_campaign_page_result attributed=%d no_longer_needed=%d unresolved=%d",
        counts["attributed"],
        counts["no_longer_needed"],
        counts["unresolved"],
    )
    if terminal:
        _log_terminal(manifest)
    return manifest


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
        retry_at=observed_at + CAMPAIGN_FAILURE_RETRY_SECONDS,
        count_failure=True,
    )


def record_access_lost(conn: sqlite3.Connection, dialog_id: int, checkpoint: int, *, observed_at: int) -> None:
    """Exclude a newly inaccessible dialog before another page can be acquired."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest, item = _require_active_dialog(conn, dialog_id, checkpoint)
        item["state"] = "done"
        item["last_error"] = "access_lost"
        item["terminal_reason"] = "access_lost"
        terminal = _next_pending(manifest) is None
        if terminal:
            _finish(conn, manifest, "exhausted", observed_at)
        else:
            _save(conn, manifest)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    logger.info("topic_attribution_campaign_page_failure reason=access_lost")
    if terminal:
        _log_terminal(manifest)


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
    return {
        "state": manifest.get("state"),
        "terminal_reason": terminal_reason,
        "terminal_severity": _TERMINAL_SEVERITY.get(str(terminal_reason), "none"),
        "dialog_count": len(dialogs),
        "pending_dialogs": sum(item.get("state") == "pending" for item in dialogs.values()),
        "failed_dialogs": sum(item.get("terminal_reason") == "failure_limit" for item in dialogs.values()),
        "abandoned_dialogs": sum(
            item.get("terminal_reason") in {"access_lost", "status_ineligible"} for item in dialogs.values()
        ),
        "counts": totals,
    }


def _log_terminal(manifest: dict[str, object]) -> None:
    status = _status_from_manifest(manifest)
    counts = cast(dict[str, int], status["counts"])
    logger.info(
        "topic_attribution_campaign_terminal reason=%s severity=%s attributed=%d no_longer_needed=%d unresolved=%d",
        status["terminal_reason"],
        status["terminal_severity"],
        counts["attributed"],
        counts["no_longer_needed"],
        counts["unresolved"],
    )


def _most_severe_terminal_reason(manifest: dict[str, object], requested: str) -> str:
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    reasons = [requested]
    reasons.extend(str(item["terminal_reason"]) for item in dialogs.values() if "terminal_reason" in item)
    return max(reasons, key=lambda reason: _TERMINAL_REASON_PRIORITY.get(reason, 0))
