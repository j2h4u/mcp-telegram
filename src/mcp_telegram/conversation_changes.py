"""Canonical, snapshot-paginated history of user-relevant conversation changes."""

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

from .conversation_change_contracts import CHANGE_KINDS

_TOKEN_VERSION = 1
_MIN_PAGE_DEPTH = 2
_TOKEN_MAX_LENGTH = 8192
_B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True, slots=True)
class ConversationChangesRequest:
    since_utc: int | None
    until_utc: int | None
    kinds: tuple[str, ...]
    dialog_id: int | None
    page_limit: int
    navigation: str | None
    filters_supplied: bool
    page_limit_supplied: bool


@dataclass(frozen=True, slots=True)
class ConversationChangesCursor:
    since_utc: int | None
    until_utc: int | None
    kinds: tuple[str, ...]
    dialog_id: int | None
    page_limit: int
    snapshot_seq: int
    after_seq: int
    page_depth: int


def _optional_strict_int(value: object, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer when present")
    return value


def _strict_int(value: object, field: str) -> int:
    parsed = _optional_strict_int(value, field)
    if parsed is None:
        raise ValueError(f"{field} must be an integer")
    return parsed


def _parse_kinds(value: object) -> tuple[str, ...]:
    if value is None:
        return CHANGE_KINDS
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ValueError("kinds must be a non-empty list of strings")
    kinds = tuple(dict.fromkeys(cast(list[str], value)))
    if any(kind not in CHANGE_KINDS for kind in kinds):
        raise ValueError("kinds contains an unsupported conversation change kind")
    return kinds


def _parse_time_bounds(req: dict[str, object]) -> tuple[int | None, int | None]:
    since_utc = _optional_strict_int(req.get("since_utc"), "since_utc")
    until_utc = _optional_strict_int(req.get("until_utc"), "until_utc")
    if since_utc is not None and since_utc < 0:
        raise ValueError("since_utc must be greater than or equal to 0")
    if until_utc is not None and until_utc < 0:
        raise ValueError("until_utc must be greater than or equal to 0")
    if since_utc is not None and until_utc is not None and since_utc >= until_utc:
        raise ValueError("since_utc must be earlier than until_utc")
    return since_utc, until_utc


def _parse_page_limit(req: dict[str, object]) -> int:
    page_limit = _strict_int(req.get("page_limit", 50), "page_limit")
    if not 1 <= page_limit <= 500:  # noqa: PLR2004 - public wire bound
        raise ValueError("page_limit must be between 1 and 500")
    return page_limit


def parse_request(req: dict[str, object]) -> ConversationChangesRequest:
    since_utc, until_utc = _parse_time_bounds(req)
    kinds = _parse_kinds(req.get("kinds"))
    dialog_id = _optional_strict_int(req.get("dialog_id"), "dialog_id")
    page_limit = _parse_page_limit(req)
    navigation = req.get("navigation")
    if navigation is not None and not isinstance(navigation, str):
        raise ValueError("navigation must be a string when present")
    return ConversationChangesRequest(
        since_utc,
        until_utc,
        kinds,
        dialog_id,
        page_limit,
        navigation,
        any(field in req for field in ("since_utc", "until_utc", "kinds", "dialog_id")),
        "page_limit" in req,
    )


class ConversationChangesTokenCodec:
    """Daemon-owned signed traversal cursor."""

    def __init__(self) -> None:
        self._secret = secrets.token_bytes(32)

    def encode(self, cursor: ConversationChangesCursor) -> str:
        body = {
            "kind": "conversation_changes",
            "version": _TOKEN_VERSION,
            "since_utc": cursor.since_utc,
            "until_utc": cursor.until_utc,
            "kinds": list(cursor.kinds),
            "dialog_id": cursor.dialog_id,
            "page_limit": cursor.page_limit,
            "snapshot_seq": cursor.snapshot_seq,
            "after_seq": cursor.after_seq,
            "page_depth": cursor.page_depth,
        }
        encoded = _b64encode(json.dumps(body, separators=(",", ":"), sort_keys=True).encode())
        signature = hmac.new(self._secret, encoded.encode(), hashlib.sha256).digest()
        return f"{encoded}.{_b64encode(signature)}"

    def decode(self, token: str) -> ConversationChangesCursor:
        data = _decode_signed_payload(self._secret, token)
        if not isinstance(data, dict) or data.get("kind") != "conversation_changes":
            raise ValueError("invalid_navigation")
        try:
            version = _strict_int(data.get("version"), "version")
            request = parse_request(cast(dict[str, object], data))
            snapshot_seq = _strict_int(data.get("snapshot_seq"), "snapshot_seq")
            after_seq = _strict_int(data.get("after_seq"), "after_seq")
            page_depth = _strict_int(data.get("page_depth"), "page_depth")
        except ValueError as exc:
            raise ValueError("invalid_navigation") from exc
        if version != _TOKEN_VERSION or snapshot_seq < 0 or after_seq <= 0 or page_depth < _MIN_PAGE_DEPTH:
            raise ValueError("invalid_navigation")
        return ConversationChangesCursor(
            request.since_utc,
            request.until_utc,
            request.kinds,
            request.dialog_id,
            request.page_limit,
            snapshot_seq,
            after_seq,
            page_depth,
        )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _b64decode(value: str) -> bytes:
    if not value or len(value) > _TOKEN_MAX_LENGTH or not _B64_RE.fullmatch(value) or len(value) % 4 == 1:
        raise ValueError("invalid_navigation")
    decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise ValueError("invalid_navigation")
    return decoded


def _decode_signed_payload(secret: bytes, token: str) -> object:
    if len(token) > _TOKEN_MAX_LENGTH or token.count(".") != 1:
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


def _effective_request(
    request: ConversationChangesRequest, codec: ConversationChangesTokenCodec
) -> tuple[ConversationChangesRequest, ConversationChangesCursor | None]:
    if request.navigation is None:
        return request, None
    cursor = codec.decode(request.navigation)
    if request.filters_supplied:
        supplied = (request.since_utc, request.until_utc, request.kinds, request.dialog_id)
        expected = (cursor.since_utc, cursor.until_utc, cursor.kinds, cursor.dialog_id)
        if supplied != expected:
            raise ValueError("invalid_navigation")
    page_limit = request.page_limit if request.page_limit_supplied else cursor.page_limit
    return (
        ConversationChangesRequest(
            cursor.since_utc,
            cursor.until_utc,
            cursor.kinds,
            cursor.dialog_id,
            page_limit,
            request.navigation,
            request.filters_supplied,
            request.page_limit_supplied,
        ),
        cursor,
    )


def _query_rows(
    conn: sqlite3.Connection,
    request: ConversationChangesRequest,
    *,
    snapshot_seq: int,
    after_seq: int | None,
) -> list[tuple[object, ...]]:
    placeholders = ",".join("?" for _ in request.kinds)
    query = f"""SELECT e.seq,e.kind,e.occurred_at,e.time_basis,e.dialog_id,d.name,
                       e.message_id,e.version,e.reason_code,e.access_change_cause,e.actor_id,
                       m.text,mv.old_text,next_mv.old_text
                  FROM conversation_history_events e
                  LEFT JOIN dialogs d ON d.dialog_id=e.dialog_id
                  LEFT JOIN messages m ON m.dialog_id=e.dialog_id AND m.message_id=e.message_id
                  LEFT JOIN message_versions mv
                    ON mv.dialog_id=e.dialog_id AND mv.message_id=e.message_id AND mv.version=e.version
                  LEFT JOIN message_versions next_mv
                    ON next_mv.dialog_id=e.dialog_id AND next_mv.message_id=e.message_id
                   AND next_mv.version=e.version+1
                 WHERE e.seq<=? AND e.kind IN ({placeholders})"""
    params: list[object] = [snapshot_seq, *request.kinds]
    if request.since_utc is not None:
        query += " AND e.occurred_at>=?"
        params.append(request.since_utc)
    if request.until_utc is not None:
        query += " AND e.occurred_at<?"
        params.append(request.until_utc)
    if request.dialog_id is not None:
        query += " AND e.dialog_id=?"
        params.append(request.dialog_id)
    if after_seq is not None:
        query += " AND e.seq<?"
        params.append(after_seq)
    query += " ORDER BY e.seq DESC LIMIT ?"
    params.append(request.page_limit + 1)
    return cast(list[tuple[object, ...]], conn.execute(query, params).fetchall())


def _text_evidence(row: tuple[object, ...]) -> dict[str, object] | None:
    kind, current_text, before_text, next_before_text = row[1], row[11], row[12], row[13]
    if kind == "deleted_message":
        return {
            "untrusted_content": True,
            "last_known_text": current_text,
            "before_text": None,
            "after_text": None,
            "provenance": "messages.text[current_candidate]" if current_text is not None else None,
            "confidence": "candidate" if current_text is not None else "unavailable",
        }
    if kind != "edit":
        return None
    after_text = next_before_text if next_before_text is not None else current_text
    return {
        "untrusted_content": True,
        "last_known_text": None,
        "before_text": before_text,
        "after_text": after_text,
        "provenance": (
            "message_versions.old_text+next_version"
            if next_before_text is not None
            else "message_versions.old_text+messages.text[current_candidate]"
        ),
        "confidence": "exact_before_candidate_after" if before_text is not None else "candidate_after_only",
    }


def _summary(kind: object) -> str:
    return {
        "edit": "Message edited",
        "deleted_message": "Message deleted",
        "access_lost": "Access lost",
        "access_restored": "Access restored",
    }[cast(str, kind)]


def _event(row: tuple[object, ...]) -> dict[str, object]:
    return {
        "event_id": row[0],
        "kind": row[1],
        "occurred_at": row[2],
        "time_basis": row[3],
        "dialog_id": row[4],
        "dialog_title": row[5],
        "message_id": row[6],
        "version": row[7],
        "summary": _summary(row[1]),
        "reason_code": row[8],
        "access_change_cause": row[9],
        "actor_id": row[10],
        "text_evidence": _text_evidence(row),
    }


def query_conversation_changes(
    conn: sqlite3.Connection, req: dict[str, object], codec: ConversationChangesTokenCodec
) -> dict[str, object]:
    try:
        parsed = parse_request(req)
        request, cursor = _effective_request(parsed, codec)
        snapshot_seq = (
            cursor.snapshot_seq
            if cursor is not None
            else cast(
                tuple[int],
                conn.execute("SELECT COALESCE(MAX(seq),0) FROM conversation_history_events").fetchone(),
            )[0]
        )
        rows = _query_rows(conn, request, snapshot_seq=snapshot_seq, after_seq=cursor.after_seq if cursor else None)
    except sqlite3.OperationalError:
        return {"ok": False, "error": "backend_error", "message": "conversation changes unavailable"}
    except ValueError as exc:
        message = str(exc)
        return {
            "ok": False,
            "error": "invalid_navigation" if message == "invalid_navigation" else "invalid_input",
            "message": message,
        }
    has_more = len(rows) > request.page_limit
    page_rows = rows[: request.page_limit]
    page_depth = cursor.page_depth if cursor is not None else 1
    next_navigation = None
    if has_more and page_rows:
        next_navigation = codec.encode(
            ConversationChangesCursor(
                request.since_utc,
                request.until_utc,
                request.kinds,
                request.dialog_id,
                request.page_limit,
                snapshot_seq,
                _strict_int(page_rows[-1][0], "event_id"),
                page_depth + 1,
            )
        )
    return {
        "ok": True,
        "data": {
            "events": [_event(row) for row in page_rows],
            "count": len(page_rows),
            "has_more": has_more,
            "next_navigation": next_navigation,
            "coverage": {
                "message_changes": "incoming_human_direct_messages",
                "access_changes": "synced_dialogs",
            },
            "_page_depth": page_depth,
        },
    }


__all__ = [
    "CHANGE_KINDS",
    "ConversationChangesRequest",
    "ConversationChangesTokenCodec",
    "parse_request",
    "query_conversation_changes",
]
