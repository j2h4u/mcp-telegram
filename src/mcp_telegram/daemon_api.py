"""Daemon API server — Unix socket request dispatcher (Plan 29-01, Task 2).

DaemonAPIServer listens on a Unix domain socket and handles five methods:
  - list_messages: read from sync.db (synced dialogs) or Telegram (on-demand)
  - search_messages: FTS5 stemmed full-text search against messages_fts
  - list_dialogs: live dialog list from Telegram enriched with sync_status
  - list_topics: forum topic list via Telegram API
  - get_me: current user info via Telegram API

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
from pathlib import Path
from typing import Any

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

try:
    from telethon.tl.functions.channels import (  # type: ignore[import-untyped]
        GetForumTopicsRequest,
    )
    from telethon import utils as telethon_utils  # type: ignore[import-untyped]
    _TELETHON_AVAILABLE = True
except ImportError:
    _TELETHON_AVAILABLE = False
    GetForumTopicsRequest = None  # type: ignore[assignment,misc]
    telethon_utils = None  # type: ignore[assignment]

from .fts import stem_query

logger = logging.getLogger(__name__)

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
        """Handle one client connection: read one request, write one response."""
        try:
            line = await reader.readline()
            if not line:
                return
            try:
                req = json.loads(line.decode())
            except json.JSONDecodeError as exc:
                response = {"ok": False, "error": "invalid_json", "message": str(exc)}
            else:
                try:
                    response = await self._dispatch(req)
                except Exception as exc:
                    logger.exception("daemon_api dispatch error")
                    response = {"ok": False, "error": "internal", "message": str(exc)}

            encoded = json.dumps(response).encode() + b"\n"
            writer.write(encoded)
            await writer.drain()
        except Exception:
            logger.exception("daemon_api handle_client error")
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
        best: Any | None = None
        best_name: str = ""
        async for d in self._client.iter_dialogs():
            name = getattr(d, "name", "") or ""
            if name.lower() == dialog.lower():
                best = d
                best_name = name
                break
            if dialog.lower() in name.lower() and best is None:
                best = d
                best_name = name

        if best is not None:
            if _TELETHON_AVAILABLE and telethon_utils is not None:
                return int(telethon_utils.get_peer_id(best.entity))
            return int(best.id)

        raise ValueError(
            f"Dialog {dialog!r} not found. "
            "Check the dialog name or use dialog_id from ListDialogs."
        )

    # ------------------------------------------------------------------
    # list_messages
    # ------------------------------------------------------------------

    async def _list_messages(self, req: dict) -> dict:
        """Return messages from sync.db (if synced) or Telegram (on-demand)."""
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        limit: int = req.get("limit", 50)
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
        messages = []
        async for msg in self._client.iter_messages(dialog_id, limit=limit):
            sender_first_name: str | None = None
            if getattr(msg, "sender", None) is not None:
                sender_first_name = getattr(msg.sender, "first_name", None)
            sent_at = 0
            if getattr(msg, "date", None) is not None:
                try:
                    sent_at = int(msg.date.timestamp())
                except Exception:
                    sent_at = 0
            messages.append(
                {
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
            )

        return {"ok": True, "data": {"messages": messages, "source": "telegram"}}

    # ------------------------------------------------------------------
    # search_messages
    # ------------------------------------------------------------------

    async def _search_messages(self, req: dict) -> dict:
        """FTS5 stemmed full-text search against messages_fts."""
        dialog_id: int = req.get("dialog_id", 0) or 0
        dialog: str | None = req.get("dialog")
        query: str = req.get("query", "")
        limit: int = req.get("limit", 20)
        offset: int = req.get("offset", 0)

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
            return {
                "ok": False,
                "error": "entity_not_found",
                "message": str(exc),
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
            return {
                "ok": False,
                "error": "topics_fetch_failed",
                "message": str(exc),
            }

        return {"ok": True, "data": {"topics": topics, "dialog_id": dialog_id}}

    # ------------------------------------------------------------------
    # get_me
    # ------------------------------------------------------------------

    async def _get_me(self, req: dict) -> dict:
        """Return current user info from Telegram."""
        me = await self._client.get_me()
        return {
            "ok": True,
            "data": {
                "id": me.id,
                "first_name": me.first_name,
                "last_name": me.last_name,
                "username": me.username,
                "phone": me.phone,
            },
        }
