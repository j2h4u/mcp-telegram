"""Source-export helper functions for the daemon API."""

import hashlib
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

_SOURCE_CURSOR_PREFIX = "telegram:v1:dialog:"
_SOURCE_UNIT_PREFIX = "dialog:"
SourceRow = Mapping[str, object]


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


def _row_int(row: SourceRow, key: str) -> int:
    value = row[key]
    return int(cast(int | float | str, value))


def _row_optional_int(row: SourceRow, key: str) -> int | None:
    value = row[key]
    return None if value is None else int(cast(int | float | str, value))


def _row_text(row: SourceRow, key: str) -> str:
    value = row[key]
    return "" if value is None else str(value)


def _row_optional_text(row: SourceRow, key: str) -> str | None:
    value = row[key]
    return None if value is None else str(value)


@dataclass(frozen=True)
class _SourceExportRequest:
    cursor_key: tuple[int, int] | None
    updated_after_cursor: tuple[int, int] | None
    updated_after_epoch: int | None
    limit: int
    cursor_value: str | None
    updated_after_value: str | None
    updated_after_cursor_value: str | None

    @classmethod
    def parse(cls, req: dict) -> _SourceExportRequest:
        try:
            cursor_key = _parse_source_cursor(req.get("cursor"))
            updated_after_cursor = _parse_source_cursor(req.get("updated_after_cursor"))
            updated_after_epoch = _parse_source_watermark(req.get("updated_after"))
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        try:
            limit = _clamp(int(req.get("limit", 100)), 1, 500)
        except TypeError, ValueError:
            limit = 100

        return cls(
            cursor_key=cursor_key,
            updated_after_cursor=updated_after_cursor,
            updated_after_epoch=updated_after_epoch,
            limit=limit,
            cursor_value=req.get("cursor") if isinstance(req.get("cursor"), str) else None,
            updated_after_value=req.get("updated_after") if isinstance(req.get("updated_after"), str) else None,
            updated_after_cursor_value=req.get("updated_after_cursor")
            if isinstance(req.get("updated_after_cursor"), str)
            else None,
        )


def _collect_unit_changes(
    identity_rows: list[SourceRow],
    update_rows: list[SourceRow],
) -> list[dict]:
    return [_source_row_to_change(row) for row in [*identity_rows, *update_rows]]


def _resolve_checkpoint_cursor(
    request: _SourceExportRequest,
    identity_rows: list[SourceRow],
) -> str | None:
    if identity_rows:
        last_identity = identity_rows[-1]
        return _source_cursor(_row_int(last_identity, "dialog_id"), _row_int(last_identity, "message_id"))
    return request.cursor_value


def _resolve_export_watermark(
    request: _SourceExportRequest,
    update_rows: list[SourceRow],
) -> tuple[str | None, str | None]:
    if not update_rows:
        return request.updated_after_value, request.updated_after_cursor_value

    max_update_epoch = max(_row_int(row, "unit_updated_epoch") for row in update_rows)
    updated_after = _source_iso(max_update_epoch)
    latest = [row for row in update_rows if _row_int(row, "unit_updated_epoch") == max_update_epoch][-1]
    updated_after_cursor = _source_cursor(_row_int(latest, "dialog_id"), _row_int(latest, "message_id"))
    return updated_after, updated_after_cursor


def _resolve_next_cursor(
    identity_rows: list[SourceRow],
    has_more_identity: bool,
) -> str | None:
    if not (identity_rows and has_more_identity):
        return None
    last_identity = identity_rows[-1]
    return _source_cursor(_row_int(last_identity, "dialog_id"), _row_int(last_identity, "message_id"))


def _source_fingerprint(*parts: object) -> str:
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _source_row_to_change(row: SourceRow) -> dict[str, object]:
    dialog_id = _row_int(row, "dialog_id")
    message_id = _row_int(row, "message_id")
    document_ref = f"dialog:{dialog_id}"
    unit_ref = _unit_ref(dialog_id, message_id)
    unit_updated_at = _source_iso(_row_int(row, "unit_updated_epoch"))
    text = _row_text(row, "text") or _row_text(row, "media_description")
    dialog_name = _row_optional_text(row, "dialog_name") or str(dialog_id)
    dialog_type = _row_optional_text(row, "dialog_type") or "Unknown"
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
        "sent_at": _source_iso(_row_int(row, "sent_at")),
        "sender_id": row["sender_id"],
        "sender_name": row["sender_first_name"],
        "topic_id": row["forum_topic_id"],
        "topic_title": row["topic_title"],
        "reply_to_msg_id": row["reply_to_msg_id"],
        "edit_date": _source_iso(_row_optional_int(row, "edit_date")) if edit_date is not None else None,
        "deleted_at": _source_iso(_row_optional_int(row, "deleted_at")) if row["deleted_at"] is not None else None,
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
) -> tuple[list[SourceRow], bool]:
    dialog_cursor, message_cursor = cursor_key if cursor_key is not None else (-9223372036854775808, -1)
    rows = cast(
        list[SourceRow],
        conn.execute(
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
          COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title
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
        ).fetchall(),
    )
    return rows[:limit], len(rows) > limit


def _source_rows_after_update_watermark(
    conn: sqlite3.Connection,
    updated_after_epoch: int,
    updated_after_cursor: tuple[int, int] | None,
    limit: int,
    excluded_keys: set[tuple[int, int]],
) -> list[SourceRow]:
    cursor_dialog, cursor_message = (
        updated_after_cursor if updated_after_cursor is not None else (-9223372036854775808, -1)
    )
    rows = cast(
        list[SourceRow],
        conn.execute(
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
          COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title
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
        ).fetchall(),
    )
    return [row for row in rows if (_row_int(row, "dialog_id"), _row_int(row, "message_id")) not in excluded_keys][
        :limit
    ]


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
        request = _SourceExportRequest.parse(req)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    identity_rows, has_more_identity = _source_rows_after_identity_cursor(conn, request.cursor_key, request.limit)
    identity_rows = identity_rows[: request.limit]
    remaining = max(0, request.limit - len(identity_rows))
    identity_keys = {(_row_int(row, "dialog_id"), _row_int(row, "message_id")) for row in identity_rows}

    update_rows = (
        _source_rows_after_update_watermark(
            conn,
            request.updated_after_epoch,
            request.updated_after_cursor,
            remaining,
            identity_keys,
        )
        if request.updated_after_epoch is not None and remaining > 0
        else []
    )

    changes = _collect_unit_changes(identity_rows, update_rows)
    checkpoint_cursor = _resolve_checkpoint_cursor(request, identity_rows)
    updated_after, updated_after_cursor_out = _resolve_export_watermark(request, update_rows)
    next_cursor = _resolve_next_cursor(identity_rows, has_more_identity)

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

    target = cast(
        object | None,
        conn.execute(
            """
        SELECT 1
        FROM messages m
        JOIN synced_dialogs sd ON sd.dialog_id = m.dialog_id
        WHERE m.dialog_id = ? AND m.message_id = ? AND m.is_deleted = 0
          AND sd.status IN ('synced', 'syncing', 'access_lost')
        """,
            (dialog_id, message_id),
        ).fetchone(),
    )
    if target is None:
        return {"ok": False, "error": "not_found"}

    rows = cast(
        list[SourceRow],
        conn.execute(
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
          COALESCE(tm.title, CASE WHEN m.forum_topic_id = 1 THEN 'General' END) AS topic_title
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
        ).fetchall(),
    )

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
