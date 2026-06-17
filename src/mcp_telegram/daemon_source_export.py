"""Source-export helper functions for the daemon API."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

_SOURCE_CURSOR_PREFIX = "telegram:v1:dialog:"
_SOURCE_UNIT_PREFIX = "dialog:"


def _source_cursor(dialog_id: int, message_id: int) -> str:
    return f"{_SOURCE_CURSOR_PREFIX}{dialog_id}:message:{message_id}"


def _unit_ref(dialog_id: int, message_id: int) -> str:
    return f"{_SOURCE_UNIT_PREFIX}{dialog_id}:message:{message_id}"


def _parse_source_cursor(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid_cursor")
    try:
        dialog_part, message_part = value.rsplit(":message:", 1)
        if not dialog_part.startswith(_SOURCE_CURSOR_PREFIX):
            raise ValueError
        dialog_id = int(dialog_part.removeprefix(_SOURCE_CURSOR_PREFIX))
        message_id = int(message_part)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_cursor") from exc
    return dialog_id, message_id


def _parse_unit_ref(value: object) -> tuple[int, int]:
    if not isinstance(value, str):
        raise ValueError("invalid_unit_ref")
    try:
        dialog_part, message_part = value.rsplit(":message:", 1)
        if not dialog_part.startswith(_SOURCE_UNIT_PREFIX):
            raise ValueError
        dialog_id = int(dialog_part.removeprefix(_SOURCE_UNIT_PREFIX))
        message_id = int(message_part)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_unit_ref") from exc
    return dialog_id, message_id


def _parse_source_watermark(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        if stripped.isdigit():
            return int(stripped)
        try:
            return int(datetime.fromisoformat(stripped).timestamp())
        except ValueError as exc:
            raise ValueError("invalid_updated_after") from exc
    raise ValueError("invalid_updated_after")


def _source_iso(epoch_seconds: int | None) -> str | None:
    if epoch_seconds is None:
        return None
    return datetime.fromtimestamp(int(epoch_seconds), tz=UTC).strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def _source_fingerprint(*parts: object) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_row_to_change(row: sqlite3.Row) -> dict:
    dialog_id = int(row["dialog_id"])
    message_id = int(row["message_id"])
    document_ref = f"dialog:{dialog_id}"
    unit_ref = _unit_ref(dialog_id, message_id)
    unit_updated_at = _source_iso(int(row["unit_updated_epoch"]))
    text = row["text"] or row["media_description"] or ""
    dialog_name = row["dialog_name"] or str(dialog_id)
    dialog_type = row["dialog_type"] or "Unknown"
    username = row["username"]
    edit_date = row["edit_date"]

    document_metadata = {
        "dialog_id": dialog_id,
        "dialog_type": dialog_type,
        "username": username,
        "sync_status": row["sync_status"],
    }
    unit_metadata = {
        "dialog_id": dialog_id,
        "message_id": message_id,
        "sent_at": _source_iso(int(row["sent_at"])),
        "sender_id": row["sender_id"],
        "sender_name": row["sender_first_name"],
        "topic_id": row["forum_topic_id"],
        "topic_title": row["topic_title"],
        "reply_to_msg_id": row["reply_to_msg_id"],
        "edit_date": _source_iso(int(edit_date)) if edit_date is not None else None,
        "deleted_at": _source_iso(int(row["deleted_at"])) if row["deleted_at"] is not None else None,
        "is_deleted": bool(row["is_deleted"]),
        "unit_updated_at": unit_updated_at,
    }

    return {
        "document": {
            "namespace": "telegram",
            "document_ref": document_ref,
            "ref": f"telegram:{document_ref}",
            "title": dialog_name,
            "source_uri": f"telegram://dialog/{dialog_id}",
            "media_type": "text/plain",
            "parser_name": "telegram-message",
            "document_type": "dialog",
            "updated_at": unit_updated_at,
            "content_fingerprint": _source_fingerprint("dialog-content", dialog_id),
            "metadata_fingerprint": _source_fingerprint(
                "dialog-meta",
                dialog_id,
                dialog_name,
                dialog_type,
                username,
            ),
            "metadata_json": document_metadata,
        },
        "unit": {
            "namespace": "telegram",
            "document_ref": document_ref,
            "unit_ref": unit_ref,
            "unit_type": "message",
            "text": text,
            "order_key": f"{message_id:020d}",
            "fingerprint": _source_fingerprint(
                "message",
                dialog_id,
                message_id,
                text,
                row["sent_at"],
                edit_date,
            ),
            "updated_at": unit_updated_at,
            "metadata_json": unit_metadata,
            "chunking_hints": {},
        },
    }


def _source_rows_after_identity_cursor(
    conn: sqlite3.Connection,
    cursor_key: tuple[int, int] | None,
    limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    dialog_cursor, message_cursor = cursor_key if cursor_key is not None else (-9223372036854775808, -1)
    rows = conn.execute(
        """
        SELECT
          m.dialog_id, m.message_id, m.sent_at, m.text, m.sender_id,
          COALESCE(sender.name, m.sender_first_name) AS sender_first_name,
          m.media_description, m.reply_to_msg_id, m.forum_topic_id,
          m.is_deleted, m.deleted_at, m.edit_date,
          CASE
            WHEN m.edit_date IS NOT NULL AND m.edit_date > m.sent_at THEN m.edit_date
            ELSE m.sent_at
          END AS unit_updated_epoch,
          sd.status AS sync_status,
          COALESCE(d.name, dialog_entity.name, CAST(m.dialog_id AS TEXT)) AS dialog_name,
          COALESCE(d.type, dialog_entity.type, 'Unknown') AS dialog_type,
          dialog_entity.username AS username,
          tm.title AS topic_title
        FROM messages m
        JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
        LEFT JOIN entities dialog_entity ON dialog_entity.id = m.dialog_id
        LEFT JOIN entities sender ON sender.id = m.sender_id
        LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id
        LEFT JOIN topic_metadata tm ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id
        WHERE sd.status IN ('synced', 'syncing', 'access_lost')
          AND m.is_deleted = 0
          AND (m.dialog_id > :dialog_cursor OR (m.dialog_id = :dialog_cursor AND m.message_id > :message_cursor))
        ORDER BY m.dialog_id ASC, m.message_id ASC
        LIMIT :limit_plus_one
        """,
        {
            "dialog_cursor": dialog_cursor,
            "message_cursor": message_cursor,
            "limit_plus_one": limit + 1,
        },
    ).fetchall()
    return rows[:limit], len(rows) > limit


def _source_rows_after_update_watermark(
    conn: sqlite3.Connection,
    updated_after_epoch: int,
    updated_after_cursor: tuple[int, int] | None,
    limit: int,
    excluded_keys: set[tuple[int, int]],
) -> list[sqlite3.Row]:
    cursor_dialog, cursor_message = (
        updated_after_cursor if updated_after_cursor is not None else (-9223372036854775808, -1)
    )
    rows = conn.execute(
        """
        SELECT
          m.dialog_id, m.message_id, m.sent_at, m.text, m.sender_id,
          COALESCE(sender.name, m.sender_first_name) AS sender_first_name,
          m.media_description, m.reply_to_msg_id, m.forum_topic_id,
          m.is_deleted, m.deleted_at, m.edit_date,
          CASE
            WHEN m.edit_date IS NOT NULL AND m.edit_date > m.sent_at THEN m.edit_date
            ELSE m.sent_at
          END AS unit_updated_epoch,
          sd.status AS sync_status,
          COALESCE(d.name, dialog_entity.name, CAST(m.dialog_id AS TEXT)) AS dialog_name,
          COALESCE(d.type, dialog_entity.type, 'Unknown') AS dialog_type,
          dialog_entity.username AS username,
          tm.title AS topic_title
        FROM messages m
        JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
        LEFT JOIN entities dialog_entity ON dialog_entity.id = m.dialog_id
        LEFT JOIN entities sender ON sender.id = m.sender_id
        LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id
        LEFT JOIN topic_metadata tm ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id
        WHERE sd.status IN ('synced', 'syncing', 'access_lost')
          AND m.is_deleted = 0
          AND (
            unit_updated_epoch > :updated_after
            OR (
              unit_updated_epoch = :updated_after
              AND (m.dialog_id > :cursor_dialog OR (m.dialog_id = :cursor_dialog AND m.message_id > :cursor_message))
            )
          )
        ORDER BY unit_updated_epoch ASC, m.dialog_id ASC, m.message_id ASC
        LIMIT :limit
        """,
        {
            "updated_after": updated_after_epoch,
            "cursor_dialog": cursor_dialog,
            "cursor_message": cursor_message,
            "limit": limit + len(excluded_keys),
        },
    ).fetchall()
    return [row for row in rows if (int(row["dialog_id"]), int(row["message_id"])) not in excluded_keys][:limit]


def _describe_source(req: dict) -> dict:
    return {
        "ok": True,
        "data": {
            "namespace": "telegram",
            "source_kind": "chat",
            "display_name": "Telegram",
            "capabilities": ["incremental-export", "unit-window"],
            "metadata_json": {"transport": "mcp-telegram-daemon"},
        },
    }


def _export_source_changes(conn: sqlite3.Connection, req: dict) -> dict:
    try:
        cursor_key = _parse_source_cursor(req.get("cursor"))
        updated_after_cursor = _parse_source_cursor(req.get("updated_after_cursor"))
        updated_after_epoch = _parse_source_watermark(req.get("updated_after"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        limit = _clamp(int(req.get("limit", 100)), 1, 500)
    except TypeError, ValueError:
        limit = 100

    identity_rows, has_more_identity = _source_rows_after_identity_cursor(conn, cursor_key, limit)
    identity_rows = identity_rows[:limit]
    remaining = max(0, limit - len(identity_rows))
    identity_keys = {(int(row["dialog_id"]), int(row["message_id"])) for row in identity_rows}

    update_rows: list[sqlite3.Row] = []
    if updated_after_epoch is not None and remaining > 0:
        update_rows = _source_rows_after_update_watermark(
            conn,
            updated_after_epoch,
            updated_after_cursor,
            remaining,
            identity_keys,
        )

    changes = [_source_row_to_change(row) for row in [*identity_rows, *update_rows]]

    checkpoint_cursor: str | None
    if identity_rows:
        checkpoint_cursor = _source_cursor(
            int(identity_rows[-1]["dialog_id"]),
            int(identity_rows[-1]["message_id"]),
        )
    else:
        cursor_value = req.get("cursor")
        checkpoint_cursor = cursor_value if isinstance(cursor_value, str) else None

    updated_after: str | None
    updated_after_cursor_out: str | None
    if update_rows:
        max_update_epoch = max(int(row["unit_updated_epoch"]) for row in update_rows)
        updated_after = _source_iso(max_update_epoch)
        last_at_watermark = [row for row in update_rows if int(row["unit_updated_epoch"]) == max_update_epoch][-1]
        updated_after_cursor_out = _source_cursor(
            int(last_at_watermark["dialog_id"]),
            int(last_at_watermark["message_id"]),
        )
    else:
        updated_after_value = req.get("updated_after")
        updated_after = updated_after_value if isinstance(updated_after_value, str) else None
        updated_after_cursor_value = req.get("updated_after_cursor")
        updated_after_cursor_out = updated_after_cursor_value if isinstance(updated_after_cursor_value, str) else None

    next_cursor: str | None = None
    if identity_rows and has_more_identity:
        next_cursor = _source_cursor(
            int(identity_rows[-1]["dialog_id"]),
            int(identity_rows[-1]["message_id"]),
        )

    return {
        "ok": True,
        "data": {
            "changes": changes,
            "next_cursor": next_cursor,
            "checkpoint_cursor": checkpoint_cursor,
            "updated_after": updated_after,
            "updated_after_cursor": updated_after_cursor_out,
        },
    }


def _read_source_unit_window(conn: sqlite3.Connection, req: dict) -> dict:
    try:
        dialog_id, message_id = _parse_unit_ref(req.get("unit_ref"))
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        before = _clamp(int(req.get("before", 0)), 0, 50)
    except TypeError, ValueError:
        before = 0
    try:
        after = _clamp(int(req.get("after", 0)), 0, 50)
    except TypeError, ValueError:
        after = 0

    target = conn.execute(
        """
        SELECT 1
        FROM messages m
        JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
        WHERE m.dialog_id = ? AND m.message_id = ? AND m.is_deleted = 0
          AND sd.status IN ('synced', 'syncing', 'access_lost')
        """,
        (dialog_id, message_id),
    ).fetchone()
    if target is None:
        return {"ok": False, "error": "not_found"}

    rows = conn.execute(
        """
        WITH selected AS (
          SELECT dialog_id, message_id FROM (
            SELECT m.dialog_id, m.message_id
            FROM messages m
            WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0 AND m.message_id < :message_id
            ORDER BY m.message_id DESC
            LIMIT :before
          )
          UNION ALL
          SELECT :dialog_id, :message_id
          UNION ALL
          SELECT dialog_id, message_id FROM (
            SELECT m.dialog_id, m.message_id
            FROM messages m
            WHERE m.dialog_id = :dialog_id AND m.is_deleted = 0 AND m.message_id > :message_id
            ORDER BY m.message_id ASC
            LIMIT :after
          )
        )
        SELECT
          m.dialog_id, m.message_id, m.sent_at, m.text, m.sender_id,
          COALESCE(sender.name, m.sender_first_name) AS sender_first_name,
          m.media_description, m.reply_to_msg_id, m.forum_topic_id,
          m.is_deleted, m.deleted_at, m.edit_date,
          CASE
            WHEN m.edit_date IS NOT NULL AND m.edit_date > m.sent_at THEN m.edit_date
            ELSE m.sent_at
          END AS unit_updated_epoch,
          sd.status AS sync_status,
          COALESCE(d.name, dialog_entity.name, CAST(m.dialog_id AS TEXT)) AS dialog_name,
          COALESCE(d.type, dialog_entity.type, 'Unknown') AS dialog_type,
          dialog_entity.username AS username,
          tm.title AS topic_title
        FROM selected s
        JOIN messages m ON m.dialog_id = s.dialog_id AND m.message_id = s.message_id
        JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
        LEFT JOIN entities dialog_entity ON dialog_entity.id = m.dialog_id
        LEFT JOIN entities sender ON sender.id = m.sender_id
        LEFT JOIN dialogs d ON d.dialog_id = m.dialog_id
        LEFT JOIN topic_metadata tm ON tm.dialog_id = m.dialog_id AND tm.topic_id = m.forum_topic_id
        WHERE sd.status IN ('synced', 'syncing', 'access_lost')
        ORDER BY m.message_id ASC
        """,
        {
            "dialog_id": dialog_id,
            "message_id": message_id,
            "before": before,
            "after": after,
        },
    ).fetchall()

    return {
        "ok": True,
        "data": {
            "namespace": "telegram",
            "document_ref": f"dialog:{dialog_id}",
            "unit_ref": _unit_ref(dialog_id, message_id),
            "units": [_source_row_to_change(row)["unit"] for row in rows],
            "metadata_json": {"dialog_id": dialog_id},
        },
    }


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))
