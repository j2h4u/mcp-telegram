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

DaemonConnection provides convenience methods for all fifteen daemon API methods.
list_messages and search_messages accept an optional dialog: str | None
parameter to support name-based resolution by the daemon.
"""

import asyncio
import contextvars
import json
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from .daemon_api import get_daemon_socket_path

logger = logging.getLogger(__name__)

# ContextVar for collecting request_ids during a tool call.
# server.py sets a fresh list before running the tool; request() appends to it.
# This enables cross-process log correlation without passing rid through tool signatures.
_request_ids: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("_request_ids", default=None)

__all__ = [
    "DaemonConnection",
    "DaemonNotRunningError",
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
                "Sync daemon closed the connection unexpectedly. Restart it with: mcp-telegram sync"
            ) from exc

        if not line:
            raise DaemonNotRunningError(
                "Sync daemon closed the connection unexpectedly. Restart it with: mcp-telegram sync"
            )
        try:
            response = json.loads(line.decode())
        except json.JSONDecodeError as exc:
            raise DaemonNotRunningError(f"Daemon returned malformed JSON: {exc}") from exc
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
        direction: str | None = None,
        sender_id: int | None = None,
        sender_name: str | None = None,
        topic_id: int | None = None,
        unread_after_id: int | None = None,
        unread: bool | None = None,
        context_message_id: int | None = None,
        context_size: int | None = None,
    ) -> dict:
        """Send list_messages request to the daemon.

        Args:
            dialog_id: Numeric dialog id (preferred over dialog name).
            dialog: Fuzzy dialog name — daemon resolves via get_entity/iter_dialogs.
            limit: Max messages to return (daemon clamps to 1..500).
            navigation: Opaque cursor token from a previous next_navigation response.
            direction: "newest" (default) or "oldest" sort order.
            sender_id: Filter messages by sender id (sync.db: AND clause, on-demand: from_user=).
            sender_name: Filter by sender name (case-insensitive LIKE, sync.db only).
            topic_id: Filter by forum topic id.
            unread_after_id: Return only messages with message_id > this value.
            unread: If True, daemon resolves read_inbox_max_id as unread_after_id.

        Optional params are omitted from the payload when None (backward compat).
        """
        payload: dict = {
            "method": "list_messages",
            "dialog_id": dialog_id,
            "dialog": dialog,
            "limit": limit,
            "navigation": navigation,
        }
        if direction is not None:
            payload["direction"] = direction
        if sender_id is not None:
            payload["sender_id"] = sender_id
        if sender_name is not None:
            payload["sender_name"] = sender_name
        if topic_id is not None:
            payload["topic_id"] = topic_id
        if unread_after_id is not None:
            payload["unread_after_id"] = unread_after_id
        if unread is not None:
            payload["unread"] = unread
        if context_message_id is not None:
            payload["context_message_id"] = context_message_id
        if context_size is not None:
            payload["context_size"] = context_size
        return await self.request(payload)

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
        """List dialogs with optional archive/pin filtering."""
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
        """List forum topics. Accepts dialog name or numeric id."""
        return await self.request(
            {
                "method": "list_topics",
                "dialog_id": dialog_id,
                "dialog": dialog,
            }
        )

    async def get_me(self) -> dict:
        """Return current authenticated user info."""
        return await self.request({"method": "get_me"})

    async def mark_dialog_for_sync(self, *, dialog_id: int, enable: bool = True) -> dict:
        """Mark or unmark a dialog for persistent sync."""
        return await self.request(
            {
                "method": "mark_dialog_for_sync",
                "dialog_id": dialog_id,
                "enable": enable,
            }
        )

    async def get_sync_status(self, *, dialog_id: int) -> dict:
        """Return sync status and message stats for a dialog."""
        return await self.request({"method": "get_sync_status", "dialog_id": dialog_id})

    async def get_sync_alerts(self, *, since: int = 0, limit: int = 50) -> dict:
        """Return deleted messages, edit history, and access-lost alerts."""
        return await self.request({"method": "get_sync_alerts", "since": since, "limit": limit})

    async def get_user_info(self, *, user_id: int) -> dict:
        """Return user profile and common chats."""
        return await self.request({"method": "get_user_info", "user_id": user_id})

    async def list_unread_messages(
        self,
        *,
        scope: str = "personal",
        limit: int = 100,
        group_size_threshold: int = 100,
    ) -> dict:
        """Return prioritized unread messages across dialogs."""
        return await self.request(
            {
                "method": "list_unread_messages",
                "scope": scope,
                "limit": limit,
                "group_size_threshold": group_size_threshold,
            }
        )

    async def record_telemetry(self, *, event: dict) -> dict:
        """Write a telemetry event to sync.db."""
        return await self.request({"method": "record_telemetry", "event": event})

    async def get_usage_stats(self, *, since: int | None = None) -> dict:
        """Return usage statistics from sync.db."""
        payload: dict = {"method": "get_usage_stats"}
        if since is not None:
            payload["since"] = since
        return await self.request(payload)

    async def upsert_entities(self, *, entities: list[dict]) -> dict:
        """Batch upsert entities into sync.db."""
        return await self.request({"method": "upsert_entities", "entities": entities})

    async def resolve_entity(self, *, query: str) -> dict:
        """Fuzzy entity resolution from sync.db."""
        return await self.request({"method": "resolve_entity", "query": query})

    async def get_dialog_stats(
        self,
        *,
        dialog_id: int = 0,
        dialog: str | None = None,
        limit: int = 5,
    ) -> dict:
        """Return aggregated stats (reactions, mentions, hashtags, forwards) for a dialog."""
        return await self.request(
            {
                "method": "get_dialog_stats",
                "dialog_id": dialog_id,
                "dialog": dialog,
                "limit": limit,
            }
        )


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
        reader, writer = await asyncio.open_unix_connection(str(socket_path), limit=2 * 1024 * 1024)
    except OSError as exc:
        raise DaemonNotRunningError("Sync daemon is not running. Start it with: mcp-telegram sync") from exc

    try:
        yield DaemonConnection(reader, writer)
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                logger.debug("daemon_client wait_closed error", exc_info=True)
