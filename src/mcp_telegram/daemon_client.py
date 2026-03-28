"""DaemonClient — async context manager for MCP tool calls to the sync daemon.

MCP tools use daemon_connection() to send requests over the Unix socket to
DaemonAPIServer and receive JSON responses.

Protocol: newline-delimited JSON over Unix socket.  Each DaemonConnection
opens a fresh socket, supports multiple sequential request() calls within
the same async-with block, then closes the socket on exit.

Error handling:
- FileNotFoundError: daemon is not running (socket file absent)
- ConnectionRefusedError: socket file exists but daemon is not listening
- Both raise DaemonNotRunningError with an actionable "mcp-telegram sync" message.
- EOF on read (daemon closed connection unexpectedly): DaemonNotRunningError.

DaemonConnection provides convenience methods for all fourteen daemon API methods.
list_messages and search_messages accept an optional dialog: str | None
parameter to support name-based resolution by the daemon.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from .daemon_api import get_daemon_socket_path

logger = logging.getLogger(__name__)

# ContextVar for collecting request_ids during a tool call.
# server.py sets a fresh list before running the tool; request() appends to it.
# This enables cross-process log correlation without passing rid through tool signatures.
_request_ids: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "_request_ids", default=None
)

__all__ = [
    "DaemonNotRunningError",
    "DaemonConnection",
    "daemon_connection",
    "get_daemon_socket_path",
]


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class DaemonNotRunningError(Exception):
    """Raised when the sync daemon is not reachable via its Unix socket.

    The message is user-facing and includes the command to start the daemon.
    """


# ---------------------------------------------------------------------------
# Connection class
# ---------------------------------------------------------------------------


class DaemonConnection:
    """Wraps a asyncio stream pair for JSON-line request/response exchanges."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def request(self, payload: dict) -> dict:
        """Send *payload* as a JSON line, read one JSON response line, return dict.

        A request_id (8 hex chars) is added to every outgoing payload for
        cross-process log correlation. The daemon echoes it back in the response.

        Raises DaemonNotRunningError if the daemon closes the connection
        without sending a response (empty read = EOF).
        """
        rid = uuid.uuid4().hex[:8]
        rids = _request_ids.get(None)
        if rids is not None:
            rids.append(rid)
        payload = {**payload, "request_id": rid}
        encoded = json.dumps(payload).encode() + b"\n"
        logger.debug("daemon_request method=%s request_id=%s", payload.get("method"), rid)
        self._writer.write(encoded)
        await self._writer.drain()

        try:
            line = await self._reader.readline()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            raise DaemonNotRunningError(
                "Sync daemon closed the connection unexpectedly. "
                "Restart it with: mcp-telegram sync"
            ) from exc

        if not line:
            raise DaemonNotRunningError(
                "Sync daemon closed the connection unexpectedly. "
                "Restart it with: mcp-telegram sync"
            )
        try:
            response = json.loads(line.decode())
        except json.JSONDecodeError as exc:
            raise DaemonNotRunningError(
                f"Daemon returned malformed JSON: {exc}"
            ) from exc
        logger.debug(
            "daemon_response method=%s request_id=%s ok=%s",
            payload.get("method"),
            response.get("request_id", rid),
            response.get("ok"),
        )
        return response

    # ------------------------------------------------------------------
    # Convenience wrappers for the daemon API methods
    # ------------------------------------------------------------------

    async def list_messages(
        self,
        *,
        dialog_id: int = 0,
        dialog: str | None = None,
        limit: int = 50,
        navigation: str | None = None,
    ) -> dict:
        """Send list_messages request. Accepts dialog name or numeric id."""
        return await self.request(
            {
                "method": "list_messages",
                "dialog_id": dialog_id,
                "dialog": dialog,
                "limit": limit,
                "navigation": navigation,
            }
        )

    async def search_messages(
        self,
        *,
        dialog_id: int = 0,
        dialog: str | None = None,
        query: str,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Send search_messages request. Accepts dialog name or numeric id."""
        return await self.request(
            {
                "method": "search_messages",
                "dialog_id": dialog_id,
                "dialog": dialog,
                "query": query,
                "limit": limit,
                "offset": offset,
            }
        )

    async def list_dialogs(
        self,
        *,
        exclude_archived: bool = False,
        ignore_pinned: bool = False,
    ) -> dict:
        """Send list_dialogs request."""
        return await self.request(
            {
                "method": "list_dialogs",
                "exclude_archived": exclude_archived,
                "ignore_pinned": ignore_pinned,
            }
        )

    async def list_topics(
        self,
        *,
        dialog_id: int = 0,
        dialog: str | None = None,
    ) -> dict:
        """Send list_topics request. Accepts dialog name or numeric id."""
        return await self.request(
            {
                "method": "list_topics",
                "dialog_id": dialog_id,
                "dialog": dialog,
            }
        )

    async def get_me(self) -> dict:
        """Send get_me request."""
        return await self.request({"method": "get_me"})

    async def mark_dialog_for_sync(self, *, dialog_id: int, enable: bool = True) -> dict:
        """Send mark_dialog_for_sync request."""
        return await self.request(
            {
                "method": "mark_dialog_for_sync",
                "dialog_id": dialog_id,
                "enable": enable,
            }
        )

    async def get_sync_status(self, *, dialog_id: int) -> dict:
        """Send get_sync_status request."""
        return await self.request({"method": "get_sync_status", "dialog_id": dialog_id})

    async def get_sync_alerts(self, *, since: int = 0, limit: int = 50) -> dict:
        """Send get_sync_alerts request."""
        return await self.request({"method": "get_sync_alerts", "since": since, "limit": limit})

    async def get_user_info(self, *, user_id: int) -> dict:
        """Send get_user_info request."""
        return await self.request({"method": "get_user_info", "user_id": user_id})

    async def list_unread_messages(
        self,
        *,
        scope: str = "personal",
        limit: int = 100,
        group_size_threshold: int = 100,
    ) -> dict:
        """Send list_unread_messages request."""
        return await self.request({
            "method": "list_unread_messages",
            "scope": scope,
            "limit": limit,
            "group_size_threshold": group_size_threshold,
        })

    async def record_telemetry(self, *, event: dict) -> dict:
        """Send record_telemetry request."""
        return await self.request({"method": "record_telemetry", "event": event})

    async def get_usage_stats(self, *, since: int | None = None) -> dict:
        """Send get_usage_stats request."""
        payload: dict = {"method": "get_usage_stats"}
        if since is not None:
            payload["since"] = since
        return await self.request(payload)

    async def upsert_entities(self, *, entities: list[dict]) -> dict:
        """Send upsert_entities request."""
        return await self.request({"method": "upsert_entities", "entities": entities})

    async def resolve_entity(self, *, query: str) -> dict:
        """Send resolve_entity request."""
        return await self.request({"method": "resolve_entity", "query": query})


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def daemon_connection() -> AsyncIterator[DaemonConnection]:
    """Open a Unix socket connection to the sync daemon.

    Yields a DaemonConnection ready for request/response exchanges.

    Raises DaemonNotRunningError with an actionable message when:
    - The socket file is absent (daemon not started)
    - The connection is refused (socket exists but daemon crashed)
    """
    socket_path = get_daemon_socket_path()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise DaemonNotRunningError(
            "Sync daemon is not running. "
            "Start it with: mcp-telegram sync"
        ) from exc

    try:
        yield DaemonConnection(reader, writer)
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                logger.debug("daemon_client wait_closed error", exc_info=True)
