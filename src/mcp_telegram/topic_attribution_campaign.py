"""Temporary, bounded repair for two explicitly enrolled bot-dialog projections.

The campaign is intentionally stored as one small daemon-state manifest.  It
is not a general history index: operator enrollment is exactly two currently
synced bot dialogs and every per-dialog checkpoint is a single message id.
PR2 removes this module and its enrollment route after the production repair.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from typing import cast

from .message_contracts import ExtractedMessage

CAMPAIGN_STATE_KEY = "topic_attribution_campaign_v1"
CAMPAIGN_VERSION = 1
CAMPAIGN_DIALOG_COUNT = 2
CAMPAIGN_MAX_ATTEMPTS_PER_DIALOG = 256
CAMPAIGN_DEADLINE_SECONDS = 7 * 24 * 60 * 60
HISTORY_PAGE_SIZE = 100
_COUNTER_KEYS = ("attributed", "no_topic", "no_longer_needed", "unresolved")


class TopicAttributionCampaignError(ValueError):
    """The explicit, narrow campaign request is not eligible."""


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(_COUNTER_KEYS, 0)


def _encode(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, separators=(",", ":"), sort_keys=True)


def _load(conn: sqlite3.Connection) -> dict[str, object] | None:
    row = cast(
        tuple[object] | None,
        conn.execute("SELECT value FROM daemon_state WHERE key=?", (CAMPAIGN_STATE_KEY,)).fetchone(),
    )
    if row is None or not isinstance(row[0], str):
        return None
    try:
        raw = cast(object, json.loads(row[0]))
    except json.JSONDecodeError as exc:
        raise TopicAttributionCampaignError("stored topic-attribution campaign manifest is invalid") from exc
    if not isinstance(raw, dict) or raw.get("version") != CAMPAIGN_VERSION:
        raise TopicAttributionCampaignError("stored topic-attribution campaign manifest is incompatible")
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
        if existing is not None and existing.get("state") == "active":
            if existing.get("dialog_ids") == ids:
                conn.commit()
                return existing
            raise TopicAttributionCampaignError("an active topic-attribution campaign already has different dialogs")
        rows = cast(
            list[tuple[object, object]],
            conn.execute(
                "SELECT sd.dialog_id, d.type FROM synced_dialogs sd JOIN dialogs d USING(dialog_id) "
                "WHERE sd.dialog_id IN (?,?) AND sd.status='synced'",
                tuple(ids),
            ).fetchall(),
        )
        if len(rows) != CAMPAIGN_DIALOG_COUNT or any(str(row[1]).lower() != "bot" for row in rows):
            raise TopicAttributionCampaignError("each enrolled dialog must currently be a synced bot dialog")
        dialogs = {
            str(dialog_id): {
                "cursor": 0,
                "attempts": 0,
                "state": "pending",
                "counts": _empty_counts(),
            }
            for dialog_id in ids
        }
        manifest: dict[str, object] = {
            "version": CAMPAIGN_VERSION,
            "state": "active",
            "created_at": observed_at,
            "deadline_at": observed_at + CAMPAIGN_DEADLINE_SECONDS,
            "max_attempts_per_dialog": CAMPAIGN_MAX_ATTEMPTS_PER_DIALOG,
            "dialog_ids": ids,
            "dialogs": dialogs,
        }
        _save(conn, manifest)
        conn.commit()
        return manifest
    except BaseException:
        conn.rollback()
        raise


def campaign_pending(conn: sqlite3.Connection, *, now: int | None = None) -> bool:
    """Return whether one bounded page remains, terminalizing expired state locally."""
    manifest = _load(conn)
    if manifest is None or manifest.get("state") != "active":
        return False
    if int(cast(int | str, manifest["deadline_at"])) <= (int(time.time()) if now is None else now):
        _terminalize_expired(conn, manifest)
        return False
    return _next_pending(manifest) is not None


def next_checkpoint(conn: sqlite3.Connection, *, now: int | None = None) -> tuple[int, int] | None:
    """Return one enrolled pending dialog and its exclusive history checkpoint."""
    manifest = _load(conn)
    if manifest is None or manifest.get("state") != "active":
        return None
    if int(cast(int | str, manifest["deadline_at"])) <= (int(time.time()) if now is None else now):
        _terminalize_expired(conn, manifest)
        return None
    pending = _next_pending(manifest)
    if pending is None:
        _finish(conn, manifest, "exhausted")
        return None
    dialog_id, item = pending
    return dialog_id, int(cast(int | str, item["cursor"]))


def record_page(  # noqa: PLR0912
    conn: sqlite3.Connection,
    dialog_id: int,
    checkpoint: int,
    messages: Sequence[ExtractedMessage],
    *,
    observed_at: int | None = None,
) -> dict[str, object]:
    """Apply one history page using only a narrow NULL-topic UPDATE.

    A missing remote topic marker stays unresolved.  It is not evidence that
    the Telegram message has no topic, so ``no_topic`` remains zero unless a
    future extractor supplies an explicit negative fact.
    """
    now = int(time.time()) if observed_at is None else observed_at
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest = _require_active_dialog(conn, dialog_id, checkpoint)
        dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
        item = dialogs[str(dialog_id)]
        counts = cast(dict[str, int], item["counts"])
        for extracted in messages:
            message = extracted.message
            topic_id = message.forum_topic_id
            if topic_id is not None:
                updated = conn.execute(
                    "UPDATE messages SET forum_topic_id=? WHERE dialog_id=? AND message_id=? "
                    "AND forum_topic_id IS NULL AND is_deleted=0",
                    (topic_id, dialog_id, message.message_id),
                ).rowcount
                if updated:
                    counts["attributed"] += updated
                else:
                    counts["no_longer_needed"] += 1
            else:
                row = cast(
                    tuple[object] | None,
                    conn.execute(
                        "SELECT 1 FROM messages WHERE dialog_id=? AND message_id=? "
                        "AND forum_topic_id IS NULL AND is_deleted=0",
                        (dialog_id, message.message_id),
                    ).fetchone(),
                )
                if row is None:
                    counts["no_longer_needed"] += 1
                else:
                    counts["unresolved"] += 1
        attempts = int(cast(int | str, item["attempts"])) + 1
        item["attempts"] = attempts
        if messages:
            item["cursor"] = min(message.message.message_id for message in messages)
        if (
            not messages
            or len(messages) < HISTORY_PAGE_SIZE
            or attempts >= int(cast(int | str, manifest["max_attempts_per_dialog"]))
        ):
            item["state"] = "done"
        conn.execute(
            "UPDATE synced_dialogs SET topic_attribution_state='partial', topic_attribution_observed_at=? "
            "WHERE dialog_id=? AND topic_attribution_state='unknown'",
            (now, dialog_id),
        )
        if _next_pending(manifest) is None:
            _finish(conn, manifest, "exhausted")
        else:
            _save(conn, manifest)
        conn.commit()
        return manifest
    except BaseException:
        conn.rollback()
        raise


def record_failed_attempt(
    conn: sqlite3.Connection, dialog_id: int, checkpoint: int, *, reason: str
) -> dict[str, object]:
    """Bound retries and make a restart-safe terminal decision after failures."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        manifest = _require_active_dialog(conn, dialog_id, checkpoint)
        item = cast(dict[str, dict[str, object]], manifest["dialogs"])[str(dialog_id)]
        item["attempts"] = int(cast(int | str, item["attempts"])) + 1
        item["last_error"] = reason
        if int(cast(int | str, item["attempts"])) >= int(cast(int | str, manifest["max_attempts_per_dialog"])):
            item["state"] = "done"
        if _next_pending(manifest) is None:
            _finish(conn, manifest, "attempt_limit")
        else:
            _save(conn, manifest)
        conn.commit()
        return manifest
    except BaseException:
        conn.rollback()
        raise


def _require_active_dialog(conn: sqlite3.Connection, dialog_id: int, checkpoint: int) -> dict[str, object]:
    manifest = _load(conn)
    if manifest is None or manifest.get("state") != "active":
        raise TopicAttributionCampaignError("topic-attribution campaign is no longer active")
    dialogs = manifest.get("dialogs")
    if not isinstance(dialogs, dict) or str(dialog_id) not in dialogs:
        raise TopicAttributionCampaignError("dialog is not enrolled in the topic-attribution campaign")
    item = dialogs[str(dialog_id)]
    if (
        not isinstance(item, dict)
        or item.get("state") != "pending"
        or int(cast(int | str, item.get("cursor", -1))) != checkpoint
    ):
        raise TopicAttributionCampaignError("topic-attribution campaign checkpoint changed")
    return manifest


def _next_pending(manifest: dict[str, object]) -> tuple[int, dict[str, object]] | None:
    dialogs = manifest.get("dialogs")
    if not isinstance(dialogs, dict):
        return None
    for dialog_id in cast(list[int], manifest.get("dialog_ids", [])):
        item = dialogs.get(str(dialog_id))
        if isinstance(item, dict) and item.get("state") == "pending":
            return dialog_id, cast(dict[str, object], item)
    return None


def _terminalize_expired(conn: sqlite3.Connection, manifest: dict[str, object]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if manifest.get("state") == "active":
            _finish(conn, manifest, "deadline")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _finish(conn: sqlite3.Connection, manifest: dict[str, object], reason: str) -> None:
    dialogs = cast(dict[str, dict[str, object]], manifest["dialogs"])
    for item in dialogs.values():
        if item.get("state") == "pending":
            item["state"] = "done"
    manifest["state"] = "complete"
    manifest["terminal_reason"] = reason
    manifest["completed_at"] = int(time.time())
    _save(conn, manifest)
