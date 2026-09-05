"""Daemon-owned global sync-alert feed backed by an immutable observed-order projection."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
from dataclasses import dataclass
from typing import cast

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 50
_TOKEN_VERSION = 1
_MIN_PAGE_DEPTH = 2
_TOKEN_MAX_LENGTH = 8192
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class SyncAlertRequest:
    since: int
    page_limit: int
    navigation: str | None
    since_supplied: bool
    limit_supplied: bool
    page_limit_supplied: bool


@dataclass(frozen=True, slots=True)
class SyncAlertCursor:
    since: int
    page_limit: int
    snapshot_seq: int
    snapshot_upper_event_at: int
    after_seq: int
    page_depth: int


@dataclass(frozen=True, slots=True)
class _AlertQueryContext:
    since: int
    limit: int
    snapshot_seq: int
    snapshot_upper_event_at: int
    after_seq: int | None
    page_depth: int


def _strict_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer")
    return value


def parse_request(req: dict[str, object]) -> SyncAlertRequest:
    """Validate the wire request without coercion or silent clamping."""
    since_supplied = "since" in req
    since = _strict_int(req.get("since", 0), "since")
    if since < 0:
        raise ValueError("since must be greater than or equal to 0")
    limit_supplied = "limit" in req
    limit = _strict_int(req.get("limit", DEFAULT_PAGE_SIZE), "limit")
    page_limit_supplied = "page_limit" in req
    if page_limit_supplied:
        page_limit = _strict_int(req.get("page_limit"), "page_limit")
        if limit_supplied and page_limit != limit:
            raise ValueError("limit and page_limit must match when both are provided")
    else:
        page_limit = limit
    if not 1 <= page_limit <= MAX_PAGE_SIZE:
        raise ValueError("limit/page_limit must be between 1 and 500")
    navigation = req.get("navigation")
    if navigation is not None and not isinstance(navigation, str):
        raise ValueError("navigation must be a string when present")
    return SyncAlertRequest(
        since,
        page_limit,
        navigation,
        since_supplied,
        limit_supplied,
        page_limit_supplied,
    )


class SyncAlertTokenCodec:
    """HMAC codec owned by the daemon process; stdio only forwards its token."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def encode(self, cursor: SyncAlertCursor) -> str:
        body = {
            "kind": "sync_alerts",
            "version": _TOKEN_VERSION,
            "since": cursor.since,
            "snapshot_seq": cursor.snapshot_seq,
            "snapshot_upper_event_at": cursor.snapshot_upper_event_at,
            "after_seq": cursor.after_seq,
            "page_depth": cursor.page_depth,
            "page_limit": cursor.page_limit,
        }
        encoded = _b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
        return f"{encoded}.{_b64encode(signature)}"

    def decode(self, token: str) -> SyncAlertCursor:
        data = _decode_signed_payload(self._secret, token)
        return _decode_cursor_payload(data)


def _decode_signed_payload(secret: bytes, token: str) -> object:
    if not isinstance(token, str) or len(token) > _TOKEN_MAX_LENGTH or token.count(".") != 1:
        raise ValueError("invalid_navigation")
    encoded, signature_text = token.split(".")
    try:
        supplied = _b64decode(signature_text)
        expected = hmac.new(secret, encoded.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("invalid_navigation")
        return cast(object, json.loads(_b64decode(encoded)))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise ValueError("invalid_navigation") from exc


def _decode_cursor_payload(data: object) -> SyncAlertCursor:
    if not isinstance(data, dict):
        raise ValueError("invalid_navigation")
    try:
        version = _strict_int(data.get("version"), "version")
        since = _strict_int(data.get("since"), "since")
        snapshot_seq = _strict_int(data.get("snapshot_seq"), "snapshot_seq")
        snapshot_upper_event_at = _strict_int(data.get("snapshot_upper_event_at"), "snapshot_upper_event_at")
        after_seq = _strict_int(data.get("after_seq"), "after_seq")
        page_limit = _strict_int(data.get("page_limit"), "page_limit")
        page_depth = _strict_int(data.get("page_depth"), "page_depth")
    except ValueError as exc:
        raise ValueError("invalid_navigation") from exc
    if data.get("kind") != "sync_alerts" or version != _TOKEN_VERSION:
        raise ValueError("invalid_navigation")
    if any(value < 0 for value in (since, snapshot_seq, snapshot_upper_event_at)) or after_seq <= 0:
        raise ValueError("invalid_navigation")
    if not 1 <= page_limit <= MAX_PAGE_SIZE or page_depth < _MIN_PAGE_DEPTH:
        raise ValueError("invalid_navigation")
    return SyncAlertCursor(since, page_limit, snapshot_seq, snapshot_upper_event_at, after_seq, page_depth)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > _TOKEN_MAX_LENGTH or not _B64_RE.fullmatch(value):
        raise ValueError("invalid_navigation")
    if len(value) % 4 == 1:
        raise ValueError("invalid_navigation")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid_navigation") from exc
    if _b64encode(decoded) != value:
        raise ValueError("invalid_navigation")
    return decoded


def _legacy_text(
    conn: sqlite3.Connection, kind: object, dialog_id: object, message_id: object, version: object
) -> object:
    if kind == "deleted_message":
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT text FROM messages WHERE dialog_id = ? AND message_id = ?",
                (dialog_id, message_id),
            ).fetchone(),
        )
    elif kind == "edit":
        row = cast(
            tuple[object, ...] | None,
            conn.execute(
                "SELECT old_text FROM message_versions WHERE dialog_id = ? AND message_id = ? AND version = ?",
                (dialog_id, message_id, version),
            ).fetchone(),
        )
    else:
        return None
    return row[0] if row is not None else None


def _query_rows(
    conn: sqlite3.Connection,
    *,
    since: int,
    snapshot_seq: int,
    after_seq: int | None,
    limit: int,
) -> list[tuple[object, ...]]:
    query = (
        "SELECT seq, kind, occurred_at, dialog_id, message_id, version, daemon_event_id "
        "FROM sync_alert_events WHERE seq <= ? AND occurred_at > ?"
    )
    params: list[int] = [snapshot_seq, since]
    if after_seq is not None:
        query += " AND seq < ?"
        params.append(after_seq)
    query += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    return cast(list[tuple[object, ...]], conn.execute(query, params).fetchall())


def _snapshot_event_at(conn: sqlite3.Connection, snapshot_seq: int, since: int) -> int:
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT MAX(occurred_at) FROM sync_alert_events WHERE seq <= ? AND occurred_at > ?",
            (snapshot_seq, since),
        ).fetchone(),
    )
    return int(cast(int, row[0])) if row is not None and row[0] is not None else 0


def _navigation_request(req: SyncAlertRequest, cursor: SyncAlertCursor) -> tuple[int, int]:
    if req.since_supplied and req.since != cursor.since:
        raise ValueError("invalid_navigation")
    if req.page_limit_supplied:
        return cursor.since, req.page_limit
    if req.limit_supplied and req.page_limit != cursor.page_limit:
        raise ValueError("invalid_navigation")
    return cursor.since, cursor.page_limit


def _resolve_query_context(
    conn: sqlite3.Connection, req: dict[str, object], codec: SyncAlertTokenCodec
) -> _AlertQueryContext:
    parsed = parse_request(req)
    cursor = codec.decode(parsed.navigation) if parsed.navigation is not None else None
    if cursor is not None:
        effective_since, effective_limit = _navigation_request(parsed, cursor)
        return _AlertQueryContext(
            effective_since,
            effective_limit,
            cursor.snapshot_seq,
            cursor.snapshot_upper_event_at,
            cursor.after_seq,
            cursor.page_depth,
        )
    effective_since, effective_limit = parsed.since, parsed.page_limit
    snapshot_seq = cast(int, conn.execute("SELECT COALESCE(MAX(seq), 0) FROM sync_alert_events").fetchone()[0])
    return _AlertQueryContext(
        effective_since,
        effective_limit,
        snapshot_seq,
        _snapshot_event_at(conn, snapshot_seq, effective_since),
        None,
        1,
    )


def _query_error(exc: ValueError | sqlite3.OperationalError) -> dict[str, object]:
    if isinstance(exc, sqlite3.OperationalError):
        return {"ok": False, "error": "backend_error", "message": "sync alerts unavailable"}
    message = str(exc)
    return {
        "ok": False,
        "error": "invalid_navigation" if message == "invalid_navigation" else "invalid_input",
        "message": message,
    }


def _alert_narrative(kind: object, message_id: object, version: object, occurred_at: object) -> tuple[str, str]:
    if kind == "access_lost":
        return f"Access lost at {occurred_at}", "Use get_sync_status for coverage details."
    if kind == "deleted_message":
        return (
            f"Deleted message msg={message_id} deleted_at={occurred_at}",
            "Inspect the dialog history around this message id if surrounding context is needed.",
        )
    return (
        f"Edited message msg={message_id} v{version} edit_date={occurred_at}",
        "Treat cached text as versioned; inspect edit history before relying on older wording.",
    )


def _alert_from_row(row: tuple[object, ...]) -> dict[str, object]:
    _seq, kind, occurred_at, dialog_id, message_id, version, daemon_event_id = row
    message, action = _alert_narrative(kind, message_id, version, occurred_at)
    item: dict[str, object] = {
        "kind": kind,
        "dialog_id": dialog_id,
        "message_id": message_id if kind != "access_lost" else None,
        "version": version if kind == "edit" else None,
        "deleted_at": occurred_at if kind == "deleted_message" else None,
        "edit_date": occurred_at if kind == "edit" else None,
        "access_lost_at": occurred_at if kind == "access_lost" else None,
        "source_id": daemon_event_id if kind == "access_lost" else 0,
        "occurred_at": occurred_at,
        "severity": "high" if kind == "access_lost" else "medium" if kind == "deleted_message" else "low",
        "message": message,
        "action": action,
    }
    return item


def _project_alerts(page_rows: list[tuple[object, ...]]) -> list[dict[str, object]]:
    return [_alert_from_row(row) for row in page_rows]


def _next_navigation(
    codec: SyncAlertTokenCodec,
    context: _AlertQueryContext,
    page_rows: list[tuple[object, ...]],
    has_more: bool,
) -> str | None:
    if not has_more or not page_rows:
        return None
    return codec.encode(
        SyncAlertCursor(
            context.since,
            context.limit,
            context.snapshot_seq,
            context.snapshot_upper_event_at,
            _strict_int(page_rows[-1][0], "seq"),
            context.page_depth + 1,
        )
    )


def _legacy_projections(
    conn: sqlite3.Connection,
    alerts: list[dict[str, object]],
    page_rows: list[tuple[object, ...]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    deleted: list[dict[str, object]] = []
    edits: list[dict[str, object]] = []
    access: list[dict[str, object]] = []
    for item, _row in zip(alerts, page_rows, strict=True):
        kind, dialog_id, message_id, version = item["kind"], item["dialog_id"], item["message_id"], item["version"]
        if kind == "deleted_message":
            deleted.append(
                {
                    "dialog_id": dialog_id,
                    "message_id": message_id,
                    "text": _legacy_text(conn, kind, dialog_id, message_id, version),
                    "deleted_at": item["deleted_at"],
                }
            )
        elif kind == "edit":
            edits.append(
                {
                    "dialog_id": dialog_id,
                    "message_id": message_id,
                    "version": version,
                    "old_text": _legacy_text(conn, kind, dialog_id, message_id, version),
                    "edit_date": item["edit_date"],
                }
            )
        else:
            access.append({"dialog_id": dialog_id, "access_lost_at": item["access_lost_at"]})
    return deleted, edits, access


def _alert_page_data(
    context: _AlertQueryContext,
    alerts: list[dict[str, object]],
    projections: tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]],
    *,
    has_more: bool,
    next_navigation: str | None,
) -> dict[str, object]:
    deleted, edits, access = projections
    return {
        "alerts": alerts,
        "deleted_messages": deleted,
        "edits": edits,
        "access_lost": access,
        "counts": {
            "deleted_messages": len(deleted),
            "edits": len(edits),
            "access_lost": len(access),
            "total": len(alerts),
        },
        "count": len(alerts),
        "since": context.since,
        "limit": context.limit,
        "page_limit": context.limit,
        "limited_by": {
            "deleted_messages": {"since": context.since, "limit": context.limit},
            "edits": {"since": context.since, "limit": context.limit},
            "access_lost": {"since": context.since, "limit": context.limit},
        },
        "has_more": has_more,
        "next_navigation": next_navigation,
        "snapshot_upper_event_at": context.snapshot_upper_event_at,
        "result_count_semantics": "count=len(alerts)=sum(counts)",
        "page_depth": context.page_depth,
    }


def query_alerts(conn: sqlite3.Connection, req: dict[str, object], codec: SyncAlertTokenCodec) -> dict[str, object]:
    """Return one canonical page and exact legacy projections for that page."""
    try:
        context = _resolve_query_context(conn, req, codec)
    except (ValueError, sqlite3.OperationalError) as exc:
        return _query_error(exc)
    rows = _query_rows(
        conn,
        since=context.since,
        snapshot_seq=context.snapshot_seq,
        after_seq=context.after_seq,
        limit=context.limit + 1,
    )
    has_more = len(rows) > context.limit
    page_rows = rows[: context.limit]
    alerts = _project_alerts(page_rows)
    projections = _legacy_projections(conn, alerts, page_rows)
    data = _alert_page_data(
        context,
        alerts,
        projections,
        has_more=has_more,
        next_navigation=_next_navigation(codec, context, page_rows, has_more),
    )
    return {"ok": True, "data": data}


__all__ = ["SyncAlertRequest", "SyncAlertTokenCodec", "parse_request", "query_alerts"]
