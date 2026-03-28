"""Daemon API server — Unix socket request dispatcher (Plan 29-01, Task 2).

DaemonAPIServer listens on a Unix domain socket and handles fourteen methods:
  - list_messages: read from sync.db (synced dialogs) or Telegram (on-demand)
  - search_messages: FTS5 stemmed full-text search against messages_fts
  - list_dialogs: live dialog list from Telegram enriched with sync_status
  - list_topics: forum topic list via Telegram API
  - get_me: current user info via Telegram API
  - mark_dialog_for_sync: add/remove dialog from sync scope
  - get_sync_status: sync status and message statistics for a dialog
  - get_sync_alerts: deleted messages, edit history, access-lost dialogs
  - get_user_info: user profile and common chats (Plan 32-01)
  - list_unread_messages: prioritized unread messages across dialogs (Plan 32-01)
  - record_telemetry: write telemetry event to sync.db (Plan 33-01)
  - get_usage_stats: read usage statistics from sync.db (Plan 33-01)
  - upsert_entities: batch upsert entities into sync.db (Plan 33-01)
  - resolve_entity: fuzzy entity resolution from sync.db (Plan 33-01)

Protocol: newline-delimited JSON (one request line → one response line).

Dialog name resolution: when dialog_id is absent or 0 and a "dialog" string
is present, _resolve_dialog_name() resolves it to a numeric id via
client.get_entity() with fallback to iter_dialogs() fuzzy match.

Architecture:
- One DaemonAPIServer instance is created per daemon run; it holds a
  reference to the long-lived sqlite3.Connection and TelegramClient.
- handle_client() is passed directly to asyncio.start_unix_server().
- Formatting (format_messages) stays on the MCP server side — the daemon
  returns raw row dicts that the MCP tools format.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

try:
    from telethon.tl.functions.channels import (  # type: ignore[import-untyped]
        GetForumTopicsRequest,
    )
    from telethon.tl.functions.messages import (  # type: ignore[import-untyped]
        GetCommonChatsRequest,
    )
    from telethon.tl.types import Channel, Chat  # type: ignore[import-untyped]
    from telethon import utils as telethon_utils  # type: ignore[import-untyped]
    _TELETHON_AVAILABLE = True
except ImportError:
    _TELETHON_AVAILABLE = False
    GetForumTopicsRequest = None  # type: ignore[assignment,misc]
    GetCommonChatsRequest = None  # type: ignore[assignment,misc]
    Channel = None  # type: ignore[assignment,misc]
    Chat = None  # type: ignore[assignment,misc]
    telethon_utils = None  # type: ignore[assignment]

USER_TTL: int = 2_592_000   # 30 days
GROUP_TTL: int = 604_800    # 7 days

from .budget import allocate_message_budget_proportional, unread_chat_tier
from .fts import stem_query
from .resolver import (
    Candidates,
    NotFound,
    Resolved,
    latinize,
    resolve as resolve_entity_sync,
)

logger = logging.getLogger(__name__)


def _clamp(value: int, low: int, high: int) -> int:
    """Clamp *value* to the inclusive range [low, high]."""
    return max(low, min(value, high))


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def get_daemon_socket_path() -> Path:
    """Return the canonical path for the daemon Unix socket."""
    return xdg_state_home() / "mcp-telegram" / "daemon.sock"


# ---------------------------------------------------------------------------
# SQL constants
# ---------------------------------------------------------------------------

_SELECT_SYNC_STATUS_SQL = "SELECT status FROM synced_dialogs WHERE dialog_id = ?"

_SELECT_MESSAGES_SQL = (
    "SELECT message_id, sent_at, text, sender_id, sender_first_name, "
    "media_description, reply_to_msg_id, forum_topic_id, reactions, is_deleted, deleted_at "
    "FROM messages WHERE dialog_id = ? AND is_deleted = 0 ORDER BY sent_at DESC LIMIT ?"
)

_SELECT_FTS_SQL = (
    "SELECT f.message_id, m.text, m.sender_first_name, m.sent_at, "
    "m.media_description, m.reply_to_msg_id "
    "FROM messages_fts f "
    "JOIN messages m ON m.dialog_id = f.dialog_id AND m.message_id = f.message_id "
    "WHERE messages_fts MATCH ? AND f.dialog_id = ? "
    "ORDER BY rank LIMIT ? OFFSET ?"
)

_SELECT_SYNCED_STATUSES_SQL = "SELECT dialog_id, status FROM synced_dialogs"

_MARK_FOR_SYNC_SQL = "INSERT OR IGNORE INTO synced_dialogs (dialog_id, status) VALUES (?, 'not_synced')"
_UNMARK_SYNC_SQL = "UPDATE synced_dialogs SET status = 'not_synced' WHERE dialog_id = ?"

_GET_SYNC_STATUS_SQL = (
    "SELECT status, last_synced_at, last_event_at, sync_progress, total_messages, access_lost_at "
    "FROM synced_dialogs WHERE dialog_id = ?"
)
_COUNT_SYNCED_MESSAGES_SQL = "SELECT COUNT(*) FROM messages WHERE dialog_id = ? AND is_deleted = 0"

_GET_DELETED_ALERTS_SQL = (
    "SELECT dialog_id, message_id, text, deleted_at "
    "FROM messages WHERE is_deleted = 1 AND deleted_at > ? "
    "ORDER BY deleted_at DESC LIMIT ?"
)
_GET_EDIT_ALERTS_SQL = (
    "SELECT dialog_id, message_id, version, old_text, edit_date "
    "FROM message_versions WHERE edit_date > ? "
    "ORDER BY edit_date DESC LIMIT ?"
)
_GET_ACCESS_LOST_ALERTS_SQL = (
    "SELECT dialog_id, access_lost_at "
    "FROM synced_dialogs WHERE status = 'access_lost' AND access_lost_at > ?"
)

# Entity / telemetry SQL (Plan 33-01)
_UPSERT_ENTITY_SQL = (
    "INSERT OR REPLACE INTO entities "
    "(id, type, name, username, name_normalized, updated_at) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_ALL_ENTITY_NAMES_SQL = (
    "SELECT id, name FROM entities "
    "WHERE (type = 'user' AND updated_at >= ?) "
    "OR (type != 'user' AND updated_at >= ?)"
)
_ALL_ENTITY_NAMES_NORMALIZED_SQL = (
    "SELECT id, name_normalized FROM entities "
    "WHERE name_normalized IS NOT NULL "
    "AND ((type = 'user' AND updated_at >= ?) "
    "OR (type != 'user' AND updated_at >= ?))"
)
_ENTITY_BY_USERNAME_SQL = "SELECT id, name FROM entities WHERE username = ?"


# ---------------------------------------------------------------------------
# Dialog category helper (used by _list_unread_messages)
# ---------------------------------------------------------------------------


def _classify_dialog_for_unread(dialog: object) -> str:
    """Classify a Telethon Dialog object into category string.

    Returns one of: "user", "bot", "group", "channel".
    """
    if getattr(dialog, "is_user", False):
        entity = getattr(dialog, "entity", None)
        if entity is not None and getattr(entity, "bot", False):
            return "bot"
        return "user"
    if getattr(dialog, "is_group", False):
        return "group"
    if getattr(dialog, "is_channel", False):
        return "channel"
    return "group"


# ---------------------------------------------------------------------------
# Usage stats query (moved from tools/stats.py in Plan 33-01)
# ---------------------------------------------------------------------------


def _query_usage_stats(cursor: sqlite3.Cursor, since: int) -> dict:
    """Run all analytics queries and return the raw stats dict."""
    tool_dist = dict(
        cursor.execute(
            "SELECT tool_name, COUNT(*) FROM telemetry_events "
            "WHERE timestamp >= ? GROUP BY tool_name ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    )

    error_dist = dict(
        cursor.execute(
            "SELECT error_type, COUNT(*) FROM telemetry_events "
            "WHERE timestamp >= ? AND error_type IS NOT NULL "
            "GROUP BY error_type ORDER BY COUNT(*) DESC",
            (since,),
        ).fetchall()
    )

    max_depth_result = cursor.execute(
        "SELECT MAX(page_depth) FROM telemetry_events WHERE timestamp >= ?",
        (since,),
    ).fetchone()
    max_depth = (
        max_depth_result[0]
        if max_depth_result and max_depth_result[0] is not None
        else 0
    )

    filter_count_result = cursor.execute(
        "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ? AND has_filter = 1",
        (since,),
    ).fetchone()
    filter_count = filter_count_result[0] if filter_count_result else 0

    total_calls_result = cursor.execute(
        "SELECT COUNT(*) FROM telemetry_events WHERE timestamp >= ?",
        (since,),
    ).fetchone()
    total_calls = total_calls_result[0] if total_calls_result else 0

    latencies = cursor.execute(
        "SELECT duration_ms FROM telemetry_events WHERE timestamp >= ? ORDER BY duration_ms",
        (since,),
    ).fetchall()

    latency_median_ms = 0
    latency_p95_ms = 0
    if latencies:
        latency_values_ms = [lat[0] for lat in latencies]
        latency_median_ms = latency_values_ms[len(latency_values_ms) // 2]
        p95_idx = int(len(latency_values_ms) * 0.95)
        latency_p95_ms = (
            latency_values_ms[p95_idx]
            if p95_idx < len(latency_values_ms)
            else latency_values_ms[-1]
        )

    return {
        "tool_distribution": tool_dist,
        "error_distribution": error_dist,
        "max_page_depth": max_depth,
        "dialogs_with_deep_scroll": 0,
        "total_calls": total_calls,
        "filter_count": filter_count,
        "latency_median_ms": latency_median_ms,
        "latency_p95_ms": latency_p95_ms,
    }


# ---------------------------------------------------------------------------
# DaemonAPIServer
# ---------------------------------------------------------------------------


class DaemonAPIServer:
    """Unix socket server that dispatches JSON requests to Telegram/sync.db.

    Instantiated once per daemon run by sync_main() (wired in Plan 29-02).
    handle_client() is passed to asyncio.start_unix_server() as the client
    connected callback.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: Any,
        shutdown_event: asyncio.Event,
    ) -> None:
        self._conn = conn
        self._client = client
        self._shutdown_event = shutdown_event

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle one client connection: read one request, write one response.

        One request per connection — client opens a new Unix socket connection
        for each call. The request_id field (if present) is echoed back in the
        response for cross-process log correlation.
        """
        method = ""
        request_id: str | None = None
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError as exc:
                logger.warning("daemon_api invalid JSON: %s", exc)
                response = {"ok": False, "error": "invalid_json", "message": "invalid JSON"}
            else:
                request_id = req.get("request_id")
                method = req.get("method", "")
                if request_id:
                    logger.debug(
                        "daemon_api_request method=%s request_id=%s", method, request_id
                    )
                try:
                    response = await self._dispatch(req)
                except Exception:
                    logger.exception(
                        "daemon_api_dispatch_error method=%s request_id=%s",
                        method,
                        request_id,
                    )
                    response = {"ok": False, "error": "internal", "message": "internal error"}
                if request_id:
                    response = {**response, "request_id": request_id}

            encoded = json.dumps(response).encode() + b"\n"
            writer.write(encoded)
            await writer.drain()
        except Exception:
            logger.exception(
                "daemon_api handle_client_write_error method=%s request_id=%s",
                method, request_id,
            )
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Dispatcher
    # ------------------------------------------------------------------

    async def _dispatch(self, req: dict) -> dict:
        """Route request to the appropriate handler by method name."""
        method = req.get("method", "")
        if method == "list_messages":
            return await self._list_messages(req)
        if method == "search_messages":
            return await self._search_messages(req)
        if method == "list_dialogs":
            return await self._list_dialogs(req)
        if method == "list_topics":
            return await self._list_topics(req)
        if method == "get_me":
            return await self._get_me(req)
        if method == "mark_dialog_for_sync":
            return await self._mark_dialog_for_sync(req)
        if method == "get_sync_status":
            return await self._get_sync_status(req)
        if method == "get_sync_alerts":
            return await self._get_sync_alerts(req)
        if method == "get_user_info":
            return await self._get_user_info(req)
        if method == "list_unread_messages":
            return await self._list_unread_messages(req)
        if method == "record_telemetry":
            return await self._record_telemetry(req)
        if method == "get_usage_stats":
            return await self._get_usage_stats(req)
        if method == "upsert_entities":
            return await self._upsert_entities(req)
        if method == "resolve_entity":
            return await self._resolve_entity(req)
        return {"ok": False, "error": "unknown_method"}

    # ------------------------------------------------------------------
    # Dialog name resolution
    # ------------------------------------------------------------------

    async def _resolve_dialog_name(self, dialog: str) -> int:
        """Resolve a dialog name string to a numeric dialog_id.

        Tries client.get_entity(dialog) first (handles @username, phone,
        invite link).  Falls back to fuzzy-matching iter_dialogs() by name
        if get_entity raises ValueError.

        Returns telethon peer id (negative for channels/groups).
        Raises ValueError with descriptive message on failure.
        """
        try:
            entity = await self._client.get_entity(dialog)
            if _TELETHON_AVAILABLE and telethon_utils is not None:
                return int(telethon_utils.get_peer_id(entity))
            return int(entity.id)
        except (ValueError, KeyError):
            pass

        # Fallback: iterate dialogs and fuzzy-match by name
        logger.debug("resolve_dialog_fallback_iter_dialogs query=%r", dialog)
        matched_dialog: Any | None = None
        matched_dialog_name: str = ""
        async for d in self._client.iter_dialogs():
            name = getattr(d, "name", "") or ""
            if name.lower() == dialog.lower():
                matched_dialog = d
                matched_dialog_name = name
                break
            if dialog.lower() in name.lower() and matched_dialog is None:
                matched_dialog = d
                matched_dialog_name = name

        if matched_dialog is not None:
            if _TELETHON_AVAILABLE and telethon_utils is not None:
                return int(telethon_utils.get_peer_id(matched_dialog.entity))
            return int(matched_dialog.id)

        raise ValueError(
            f"Dialog {dialog!r} not found. "
            "Check the dialog name or use dialog_id from ListDialogs."
        )

    # ------------------------------------------------------------------
    # Shared message helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _msg_to_dict(msg: Any) -> dict:
        """Convert a live Telethon message object to the standard message dict.

        Used by the on-demand path in _list_messages and by _list_unread_messages
        to avoid duplicating the same field-extraction logic.
        """
        sender_first_name: str | None = None
        if getattr(msg, "sender", None) is not None:
            sender_first_name = getattr(msg.sender, "first_name", None)
        sent_at = 0
        if getattr(msg, "date", None) is not None:
            try:
                sent_at = int(msg.date.timestamp())
            except Exception:
                sent_at = 0
        return {
            "message_id": msg.id,
            "sent_at": sent_at,
            "text": getattr(msg, "message", None),
            "sender_id": getattr(msg, "sender_id", None),
            "sender_first_name": sender_first_name,
            "media_description": None,
            "reply_to_msg_id": getattr(msg, "reply_to_msg_id", None),
            "forum_topic_id": getattr(msg, "forum_topic_id", None),
            "reactions": None,
            "is_deleted": 0,
        }

    # ------------------------------------------------------------------
    # list_messages
    # ------------------------------------------------------------------

    async def _list_messages(self, req: dict) -> dict:
        """Return messages from sync.db (if synced) or Telegram (on-demand)."""
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        limit: int = _clamp(req.get("limit", 50), 1, 500)
        navigation: str | None = req.get("navigation")

        # Dialog resolution
        if not dialog_id and dialog:
            try:
                dialog_id = await self._resolve_dialog_name(dialog)
            except ValueError as exc:
                return {"ok": False, "error": "dialog_not_found", "message": str(exc)}

        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required",
            }

        # Check sync status
        row = self._conn.execute(_SELECT_SYNC_STATUS_SQL, (dialog_id,)).fetchone()
        status = row[0] if row is not None else None

        if status in ("synced", "syncing"):
            # Read from sync.db
            rows = self._conn.execute(_SELECT_MESSAGES_SQL, (dialog_id, limit)).fetchall()
            messages = [
                {
                    "message_id": r[0],
                    "sent_at": r[1],
                    "text": r[2],
                    "sender_id": r[3],
                    "sender_first_name": r[4],
                    "media_description": r[5],
                    "reply_to_msg_id": r[6],
                    "forum_topic_id": r[7],
                    "reactions": r[8],
                    "is_deleted": r[9],
                    "deleted_at": r[10],
                }
                for r in rows
            ]
            return {"ok": True, "data": {"messages": messages, "source": "sync_db"}}

        # On-demand fetch from Telegram
        logger.debug("list_messages_fallback_telegram dialog_id=%d", dialog_id)
        messages = []
        try:
            async for msg in self._client.iter_messages(dialog_id, limit=limit):
                messages.append(self._msg_to_dict(msg))
        except Exception as exc:
            logger.warning(
                "list_messages_telegram_error dialog_id=%d error=%s",
                dialog_id,
                exc,
                exc_info=True,
            )
            return {"ok": False, "error": "telegram_error", "message": "failed to fetch messages"}

        return {"ok": True, "data": {"messages": messages, "source": "telegram"}}

    # ------------------------------------------------------------------
    # search_messages
    # ------------------------------------------------------------------

    async def _search_messages(self, req: dict) -> dict:
        """FTS5 stemmed full-text search against messages_fts."""
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        query: str = req.get("query", "")
        limit: int = _clamp(req.get("limit", 20), 1, 200)
        offset: int = max(0, req.get("offset", 0))

        # Dialog resolution
        if not dialog_id and dialog:
            try:
                dialog_id = await self._resolve_dialog_name(dialog)
            except ValueError as exc:
                return {"ok": False, "error": "dialog_not_found", "message": str(exc)}

        # Stem the query
        stemmed = stem_query(query)
        if not stemmed:
            return {"ok": True, "data": {"messages": [], "total": 0}}

        rows = self._conn.execute(
            _SELECT_FTS_SQL,
            (stemmed, dialog_id, limit, offset),
        ).fetchall()

        messages = [
            {
                "message_id": r[0],
                "text": r[1],
                "sender_first_name": r[2],
                "sent_at": r[3],
                "media_description": r[4],
                "reply_to_msg_id": r[5],
            }
            for r in rows
        ]

        return {"ok": True, "data": {"messages": messages, "total": len(messages)}}

    # ------------------------------------------------------------------
    # list_dialogs
    # ------------------------------------------------------------------

    async def _list_dialogs(self, req: dict) -> dict:
        """Return live dialog list from Telegram enriched with sync_status."""
        # Load current sync statuses for O(1) lookup
        synced_statuses: dict[int, str] = {
            row[0]: row[1]
            for row in self._conn.execute(_SELECT_SYNCED_STATUSES_SQL).fetchall()
        }

        exclude_archived: bool = req.get("exclude_archived", False)
        ignore_pinned: bool = req.get("ignore_pinned", False)

        archived_flag = False if exclude_archived else None

        dialogs = []
        try:
            async for d in self._client.iter_dialogs(
                archived=archived_flag,
                ignore_pinned=ignore_pinned,
            ):
                entity = getattr(d, "entity", None)
                entity_type = type(entity).__name__ if entity is not None else "Unknown"
                last_msg_at: int | None = None
                if getattr(d, "date", None) is not None:
                    try:
                        last_msg_at = int(d.date.timestamp())
                    except Exception:
                        last_msg_at = None
                dialogs.append(
                    {
                        "id": d.id,
                        "name": getattr(d, "name", None),
                        "type": entity_type,
                        "last_message_at": last_msg_at,
                        "unread_count": getattr(d, "unread_count", 0),
                        "sync_status": synced_statuses.get(d.id, "not_synced"),
                    }
                )
        except Exception as exc:
            logger.warning("list_dialogs_telegram_error error=%s", exc, exc_info=True)
            return {"ok": False, "error": "telegram_error", "message": "failed to list dialogs"}

        return {"ok": True, "data": {"dialogs": dialogs}}

    # ------------------------------------------------------------------
    # list_topics
    # ------------------------------------------------------------------

    async def _list_topics(self, req: dict) -> dict:
        """Return forum topics for a dialog via Telegram API."""
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")

        # Dialog resolution
        if not dialog_id and dialog:
            try:
                dialog_id = await self._resolve_dialog_name(dialog)
            except ValueError as exc:
                return {"ok": False, "error": "dialog_not_found", "message": str(exc)}

        if not dialog_id:
            return {
                "ok": False,
                "error": "missing_dialog",
                "message": "Either dialog_id or dialog name is required for list_topics",
            }

        try:
            entity = await self._client.get_entity(dialog_id)
        except Exception as exc:
            logger.warning("get_entity failed for dialog_id=%s: %s", dialog_id, exc)
            return {
                "ok": False,
                "error": "entity_not_found",
                "message": "telegram API error",
            }

        try:
            if _TELETHON_AVAILABLE and GetForumTopicsRequest is not None:
                result = await self._client(
                    GetForumTopicsRequest(
                        channel=entity,
                        offset_date=0,
                        offset_id=0,
                        offset_topic=0,
                        limit=100,
                        q="",
                    )
                )
                topics = [
                    {
                        "id": t.id,
                        "title": getattr(t, "title", None),
                        "icon_emoji_id": getattr(t, "icon_emoji_id", None),
                        "date": getattr(t, "date", None),
                    }
                    for t in getattr(result, "topics", [])
                ]
            else:
                topics = []
        except Exception as exc:
            logger.warning("topics fetch failed for dialog_id=%s: %s", dialog_id, exc)
            return {
                "ok": False,
                "error": "topics_fetch_failed",
                "message": "telegram API error",
            }

        return {"ok": True, "data": {"topics": topics, "dialog_id": dialog_id}}

    # ------------------------------------------------------------------
    # get_me
    # ------------------------------------------------------------------

    async def _get_me(self, req: dict) -> dict:
        """Return current user info from Telegram."""
        try:
            me = await self._client.get_me()
        except Exception as exc:
            logger.warning("get_me_failed error=%s", exc, exc_info=True)
            return {"ok": False, "error": "telegram_error", "message": "failed to retrieve account info"}
        if me is None:
            return {"ok": False, "error": "not_found", "message": "account info unavailable"}
        return {
            "ok": True,
            "data": {
                "id": me.id,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "username": me.username,
            },
        }

    # ------------------------------------------------------------------
    # mark_dialog_for_sync
    # ------------------------------------------------------------------

    async def _mark_dialog_for_sync(self, req: dict) -> dict:
        """Add or remove a dialog from sync scope in synced_dialogs.

        enable=True: INSERT OR IGNORE with status='not_synced' (daemon picks
        up the new dialog within one heartbeat interval).
        enable=False: UPDATE status back to 'not_synced' (pauses syncing,
        preserves history).
        """
        dialog_id: int = req.get("dialog_id", 0)
        enable: bool = req.get("enable", True)
        if enable:
            self._conn.execute(_MARK_FOR_SYNC_SQL, (dialog_id,))
        else:
            self._conn.execute(_UNMARK_SYNC_SQL, (dialog_id,))
        self._conn.commit()
        return {"ok": True}

    # ------------------------------------------------------------------
    # get_sync_status
    # ------------------------------------------------------------------

    async def _get_sync_status(self, req: dict) -> dict:
        """Return sync status and message statistics for a dialog.

        delete_detection is derived from dialog_id sign:
        - Negative → channel/supergroup → "reliable (channel)"
        - Positive → DM/small group → "best-effort weekly (DM)"
        """
        dialog_id: int = req.get("dialog_id", 0)
        row = self._conn.execute(_GET_SYNC_STATUS_SQL, (dialog_id,)).fetchone()

        if row is not None:
            status: str = row[0]
            last_synced_at: int | None = row[1]
            last_event_at: int | None = row[2]
            sync_progress: int | None = row[3]
            total_messages: int | None = row[4]
        else:
            status = "not_synced"
            last_synced_at = None
            last_event_at = None
            sync_progress = None
            total_messages = None

        count_row = self._conn.execute(_COUNT_SYNCED_MESSAGES_SQL, (dialog_id,)).fetchone()
        message_count: int = count_row[0] if count_row is not None else 0

        delete_detection = "reliable (channel)" if dialog_id < 0 else "best-effort weekly (DM)"

        return {
            "ok": True,
            "data": {
                "dialog_id": dialog_id,
                "status": status,
                "message_count": message_count,
                "last_synced_at": last_synced_at,
                "last_event_at": last_event_at,
                "sync_progress": sync_progress,
                "total_messages": total_messages,
                "delete_detection": delete_detection,
            },
        }

    # ------------------------------------------------------------------
    # get_sync_alerts
    # ------------------------------------------------------------------

    async def _get_sync_alerts(self, req: dict) -> dict:
        """Return sync alerts: deleted messages, edit history, access-lost dialogs.

        since: unix timestamp — only return alerts newer than this value (default 0).
        limit: max items per category (default 50).
        """
        since: int = req.get("since", 0)
        limit: int = _clamp(req.get("limit", 50), 1, 500)

        deleted_rows = self._conn.execute(_GET_DELETED_ALERTS_SQL, (since, limit)).fetchall()
        deleted_messages = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "text": r[2],
                "deleted_at": r[3],
            }
            for r in deleted_rows
        ]

        edit_rows = self._conn.execute(_GET_EDIT_ALERTS_SQL, (since, limit)).fetchall()
        edits = [
            {
                "dialog_id": r[0],
                "message_id": r[1],
                "version": r[2],
                "old_text": r[3],
                "edit_date": r[4],
            }
            for r in edit_rows
        ]

        access_lost_rows = self._conn.execute(_GET_ACCESS_LOST_ALERTS_SQL, (since,)).fetchall()
        access_lost = [
            {
                "dialog_id": r[0],
                "access_lost_at": r[1],
            }
            for r in access_lost_rows
        ]

        return {
            "ok": True,
            "data": {
                "deleted_messages": deleted_messages,
                "edits": edits,
                "access_lost": access_lost,
            },
        }

    # ------------------------------------------------------------------
    # get_user_info
    # ------------------------------------------------------------------

    async def _get_user_info(self, req: dict) -> dict:
        """Return user profile and list of common chats.

        Calls client.get_entity(user_id) and GetCommonChatsRequest to build
        a complete user profile dict with typed common_chats entries.
        """
        user_id: int = req.get("user_id", 0)
        try:
            user = await self._client.get_entity(user_id)
        except Exception as exc:
            logger.warning("get_entity failed for user_id=%s: %s", user_id, exc)
            return {"ok": False, "error": "user_not_found", "message": "telegram API error"}

        # Fetch common chats (only available for user entities)
        common_chats: list[dict] = []
        if _TELETHON_AVAILABLE and GetCommonChatsRequest is not None:
            try:
                common_result = await self._client(
                    GetCommonChatsRequest(user_id=user_id, max_id=0, limit=100)
                )
                for chat in getattr(common_result, "chats", []):
                    # Determine chat type from Telethon type system
                    if Channel is not None and isinstance(chat, Channel):
                        chat_type = "supergroup" if getattr(chat, "megagroup", False) else "channel"
                    elif Chat is not None and isinstance(chat, Chat):
                        chat_type = "group"
                    else:
                        chat_type = "user"

                    if _TELETHON_AVAILABLE and telethon_utils is not None:
                        full_id = int(telethon_utils.get_peer_id(chat))
                    else:
                        full_id = int(chat.id)

                    common_chats.append({
                        "id": full_id,
                        "name": getattr(chat, "title", None) or str(chat.id),
                        "type": chat_type,
                    })
            except Exception as exc:
                logger.warning("get_user_info common_chats_failed user_id=%r error=%s", user_id, exc)

        return {
            "ok": True,
            "data": {
                "id": user.id,
                "first_name": getattr(user, "first_name", None),
                "last_name": getattr(user, "last_name", None),
                "username": getattr(user, "username", None),
                "common_chats": common_chats,
            },
        }

    # ------------------------------------------------------------------
    # list_unread_messages
    # ------------------------------------------------------------------

    async def _list_unread_messages(self, req: dict) -> dict:
        """Return prioritized unread messages across dialogs."""
        scope: str = req.get("scope", "personal")
        limit: int = _clamp(req.get("limit", 100), 1, 500)
        group_size_threshold: int = req.get("group_size_threshold", 100)

        entries, counts = await self._collect_unread_dialogs(scope, group_size_threshold)
        self._rank_unread_entries(entries)
        allocation = allocate_message_budget_proportional(counts, limit)
        groups = await self._fetch_unread_groups(entries, allocation)

        return {"ok": True, "data": {"groups": groups}}

    async def _collect_unread_dialogs(
        self, scope: str, group_size_threshold: int
    ) -> tuple[list[dict], dict[int, int]]:
        """Iterate Telegram dialogs, return those with unread_count > 0."""
        entries: list[dict] = []
        counts: dict[int, int] = {}

        async for dialog in self._client.iter_dialogs(archived=None, ignore_pinned=False):
            unread_count = getattr(dialog, "unread_count", 0)
            if unread_count == 0:
                continue
            chat_id = getattr(dialog, "id", None)
            if not isinstance(chat_id, int):
                continue

            category = _classify_dialog_for_unread(dialog)

            if scope == "personal":
                if category == "channel":
                    continue
                entity = getattr(dialog, "entity", None)
                participants_count = (
                    getattr(entity, "participants_count", None) if entity is not None else None
                )
                if (
                    category == "group"
                    and participants_count is not None
                    and participants_count > group_size_threshold
                ):
                    continue

            raw_dialog = getattr(dialog, "dialog", None)
            entries.append({
                "chat_id": chat_id,
                "display_name": getattr(dialog, "name", f"Chat {chat_id}"),
                "unread_count": unread_count,
                "unread_mentions_count": getattr(dialog, "unread_mentions_count", 0),
                "category": category,
                "date": getattr(dialog, "date", None),
                "read_inbox_max_id": getattr(raw_dialog, "read_inbox_max_id", 0) if raw_dialog else 0,
            })
            counts[chat_id] = unread_count

        return entries, counts

    @staticmethod
    def _rank_unread_entries(entries: list[dict]) -> None:
        """Assign priority tiers and sort in place (lower tier = higher priority)."""
        for entry in entries:
            entry["tier"] = unread_chat_tier({
                "unread_mentions_count": entry["unread_mentions_count"],
                "category": entry["category"],
            })
        entries.sort(
            key=lambda e: (e["tier"], -(e["date"].timestamp() if e["date"] else 0))
        )

    async def _fetch_unread_groups(
        self, entries: list[dict], allocation: dict[int, int]
    ) -> list[dict]:
        """Fetch messages for each unread dialog up to its budget allocation."""
        groups: list[dict] = []
        for entry in entries:
            budget = allocation.get(entry["chat_id"], 0)
            group: dict = {
                "dialog_id": entry["chat_id"],
                "display_name": entry["display_name"],
                "tier": entry["tier"],
                "category": entry["category"],
                "unread_count": entry["unread_count"],
                "unread_mentions_count": entry["unread_mentions_count"],
                "messages": [],
            }
            if budget == 0:
                if entry["category"] == "channel":
                    groups.append(group)
                continue
            try:
                async for msg in self._client.iter_messages(
                    entry["chat_id"],
                    min_id=entry["read_inbox_max_id"],
                    limit=budget,
                ):
                    d = self._msg_to_dict(msg)
                    group["messages"].append({
                        "message_id": d["message_id"],
                        "sent_at": d["sent_at"],
                        "text": d["text"],
                        "sender_id": d["sender_id"],
                        "sender_first_name": d["sender_first_name"],
                    })
            except Exception as exc:
                fetched = len(group["messages"])
                logger.warning(
                    "unread_fetch_failed chat_id=%r fetched=%d expected=%d error=%s",
                    entry["chat_id"],
                    fetched,
                    entry["unread_count"],
                    exc,
                    exc_info=True,
                )
                group["is_truncated"] = True
            groups.append(group)

        return groups

    # ------------------------------------------------------------------
    # record_telemetry (Plan 33-01)
    # ------------------------------------------------------------------

    async def _record_telemetry(self, req: dict) -> dict:
        """Write a telemetry event row to sync.db telemetry_events table."""
        event = req.get("event", {})
        tool_name = event.get("tool_name", "")
        if not isinstance(tool_name, str) or len(tool_name) > 200:
            return {"ok": False, "error": "invalid_input", "message": "tool_name must be a string (max 200 chars)"}
        try:
            self._conn.execute(
                "INSERT INTO telemetry_events "
                "(tool_name, timestamp, duration_ms, result_count, "
                "has_cursor, page_depth, has_filter, error_type) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.get("tool_name"),
                    event.get("timestamp"),
                    event.get("duration_ms"),
                    event.get("result_count"),
                    event.get("has_cursor"),
                    event.get("page_depth"),
                    event.get("has_filter"),
                    event.get("error_type"),
                ),
            )
            self._conn.commit()
            return {"ok": True}
        except Exception as exc:
            logger.error("record_telemetry failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # get_usage_stats (Plan 33-01)
    # ------------------------------------------------------------------

    async def _get_usage_stats(self, req: dict) -> dict:
        """Return usage statistics from sync.db telemetry_events."""
        since: int = req.get("since", int(time.time()) - 30 * 86400)
        try:
            stats = _query_usage_stats(self._conn.cursor(), since)
            return {"ok": True, "data": stats}
        except Exception as exc:
            logger.error("get_usage_stats failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # upsert_entities (Plan 33-01)
    # ------------------------------------------------------------------

    async def _upsert_entities(self, req: dict) -> dict:
        """Batch upsert entity rows into sync.db entities table."""
        entities = req.get("entities", [])
        if not isinstance(entities, list) or len(entities) > 10000:
            return {"ok": False, "error": "invalid_input", "message": "entities must be a list (max 10000)"}
        if not entities:
            return {"ok": True, "upserted": 0}
        now = int(time.time())
        try:
            self._conn.executemany(
                _UPSERT_ENTITY_SQL,
                [
                    (
                        e["id"],
                        e["type"],
                        e["name"],
                        e.get("username"),
                        latinize(e["name"]),
                        now,
                    )
                    for e in entities
                ],
            )
            self._conn.commit()
            return {"ok": True, "upserted": len(entities)}
        except Exception as exc:
            logger.error("upsert_entities failed: %s", exc, exc_info=True)
            return {"ok": False, "error": "internal", "message": "internal error"}

    # ------------------------------------------------------------------
    # resolve_entity (Plan 33-01)
    # ------------------------------------------------------------------

    async def _resolve_entity(self, req: dict) -> dict:
        """Fuzzy entity resolution from sync.db entities table."""
        query: str = req.get("query", "")
        if not query:
            return {"ok": False, "error": "missing_query"}

        # @username lookup
        if query.startswith("@"):
            username_query = query[1:]
            row = self._conn.execute(
                _ENTITY_BY_USERNAME_SQL, (username_query,)
            ).fetchone()
            if row:
                return {
                    "ok": True,
                    "data": {
                        "result": "resolved",
                        "entity_id": row[0],
                        "display_name": row[1],
                    },
                }
            return {"ok": True, "data": {"result": "not_found", "query": query}}

        now = int(time.time())
        choices = dict(
            self._conn.execute(
                _ALL_ENTITY_NAMES_SQL, (now - USER_TTL, now - GROUP_TTL)
            ).fetchall()
        )
        normalized = dict(
            self._conn.execute(
                _ALL_ENTITY_NAMES_NORMALIZED_SQL, (now - USER_TTL, now - GROUP_TTL)
            ).fetchall()
        )

        result = resolve_entity_sync(
            query, choices, None, normalized_choices=normalized
        )

        if isinstance(result, Resolved):
            return {
                "ok": True,
                "data": {
                    "result": "resolved",
                    "entity_id": result.entity_id,
                    "display_name": result.display_name,
                },
            }
        if isinstance(result, Candidates):
            return {
                "ok": True,
                "data": {"result": "candidates", "matches": result.matches},
            }
        return {"ok": True, "data": {"result": "not_found", "query": query}}
