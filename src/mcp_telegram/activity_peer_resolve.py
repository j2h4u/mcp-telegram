"""Low-level peer and linked-chat resolution primitives.

Acyclic low-level module: no imports from higher-level coordinator modules.
Importable by both sweep and API layers without creating import cycles.

This module owns:
  - resolve_input_peer: entity-type-aware dialog_id → InputPeer
  - resolve_linked_chat_id: cache-first linked_chat_id resolver
  - LinkedChatResolution: typed result dataclass
  - _ENTITY_DETAIL_TTL_SECONDS: canonical TTL constant
"""

import json
import logging
import sqlite3
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Protocol, cast

from telethon.tl.types import TypeInputChannel, TypeInputPeer

from .activity_sync import _ActivityClient

logger = logging.getLogger(__name__)

# Canonical TTL for entity_details cache reads.
# Owned here as a single source of truth; importable by higher-level layers.
_ENTITY_DETAIL_TTL_SECONDS: int = 300
_MIN_LINKED_CHAT_SCHEMA_VERSION = 24


@dataclass
class LinkedChatResolution:
    """Result of a linked-chat resolution attempt.

    linked_chat_id: int | None
        The normalized -100… discussion-group peer id, or None when the
        channel has no discussion group OR when a FloodWait was caught
        (check flood_wait_seconds to distinguish).

    flood_wait_seconds: int | None
        Set to the flood wait duration when GetFullChannelRequest was rate-
        limited. None on a clean resolution (with or without a linked chat).
    """

    linked_chat_id: int | None
    flood_wait_seconds: int | None


@dataclass
class _LinkedChatCacheWrite:
    """Context for persisting a live linked-chat resolution."""

    conn: sqlite3.Connection
    channel_id: int
    linked_chat_id: int | None
    existing_blob: dict[str, object]
    existing_detail_row: tuple[str] | None
    channel_name: str | None
    channel_username: str | None
    now: int


class _InputEntityResolverClient(Protocol):
    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]: ...


async def resolve_input_peer(client: _InputEntityResolverClient, dialog_id: int) -> TypeInputPeer | None:
    """Resolve a bare dialog_id to a concrete InputPeer via the Telethon session.

    Uses client.get_input_entity() which is entity-type-aware: it resolves
    channel vs chat vs user and supplies the cached access_hash from the
    session, so we do NOT hand-build InputPeerChannel from the entities table
    (which has no access_hash column).

    Returns None on access-loss, cache miss, or any other lookup failure so
    the caller can skip-and-retry rather than crash.  Never raises.
    """
    try:
        return cast(TypeInputPeer, await client.get_input_entity(dialog_id))
    except Exception:
        logger.debug("activity_peer_resolve_input_peer_miss dialog_id=%r", dialog_id, exc_info=True)
        return None


def _assert_linked_chat_schema(conn: sqlite3.Connection) -> None:
    """Raise when the connection is older than the linked-chat schema floor."""
    try:
        version_row = cast(
            tuple[int | None] | None,
            conn.execute("SELECT MAX(version) FROM schema_version").fetchone(),
        )
        schema_version = version_row[0] if version_row is not None and version_row[0] is not None else 0
    except sqlite3.OperationalError:
        schema_version = 0
    if schema_version < _MIN_LINKED_CHAT_SCHEMA_VERSION:
        raise RuntimeError(
            f"activity_peer_resolve.resolve_linked_chat_id requires schema v24+ "
            f"(dialogs.linked_chat_id, dialogs.linked_chat_resolved_at). "
            f"Connection reports schema_version={schema_version}. "
            f"Phase 54 cache-substrate flip: a half-migrated daemon must NOT fall "
            f"through to live GetFullChannelRequest on every call — that re-creates "
            f"the exact ban-trigger pattern Phase 54 exists to eliminate. "
            f"Run ensure_sync_schema() on this connection before calling the resolver."
        )


def _read_cached_linked_chat(conn: sqlite3.Connection, channel_id: int) -> LinkedChatResolution | None:
    """Return the cached linked-chat answer when dialogs already has one."""
    row = cast(
        tuple[int | None, int | None] | None,
        conn.execute(
            "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id = ?",
            (channel_id,),
        ).fetchone(),
    )
    if row is None:
        return None
    linked_chat_id, linked_chat_resolved_at = cast(tuple[int | None, int | None], row)
    if linked_chat_resolved_at is None:
        return None
    return LinkedChatResolution(linked_chat_id=linked_chat_id, flood_wait_seconds=None)


def _normalize_linked_chat_id(linked_chat_id_raw: int | None) -> int | None:
    """Normalize Telethon's linked-chat id into the canonical peer id form."""
    if linked_chat_id_raw is None:
        return None
    if linked_chat_id_raw > 0:
        from telethon.tl.types import PeerChannel
        from telethon.utils import get_peer_id

        return int(get_peer_id(PeerChannel(linked_chat_id_raw)))
    return int(linked_chat_id_raw)


def _load_existing_detail_blob(
    conn: sqlite3.Connection, channel_id: int
) -> tuple[dict[str, object], tuple[str] | None]:
    """Load the current entity_details JSON payload, if any."""
    existing_detail_row = cast(
        tuple[str] | None,
        conn.execute(
            "SELECT detail_json FROM entity_details WHERE entity_id = ?",
            (channel_id,),
        ).fetchone(),
    )
    if existing_detail_row is None:
        return {}, None
    try:
        detail_row = cast(tuple[str], existing_detail_row)
        return cast(dict[str, object], json.loads(detail_row[0])), detail_row
    except json.JSONDecodeError:
        return {}, cast(tuple[str], existing_detail_row)


class _FullChatLike(Protocol):
    participants_count: int | None
    pinned_msg_id: int | None
    about: str | None
    linked_chat_id: int | None


class _FullResultChatLike(Protocol):
    id: int
    title: str | None
    username: str | None


class _FullResultLike(Protocol):
    full_chat: _FullChatLike
    chats: list[_FullResultChatLike] | None


def _merge_sibling_linked_chat_fields(full_chat: _FullChatLike, existing_blob: dict[str, object]) -> dict[str, object]:
    """Overlay sibling fields from GetFullChannel into the cached detail blob."""
    subscribers_count = getattr(full_chat, "participants_count", None)
    if subscribers_count is not None:
        existing_blob["subscribers_count"] = subscribers_count
    pinned_msg_id = getattr(full_chat, "pinned_msg_id", None)
    if pinned_msg_id is not None:
        existing_blob["pinned_msg_id"] = pinned_msg_id
    about = getattr(full_chat, "about", None)
    if about is not None:
        existing_blob["about"] = about
    return existing_blob


def _extract_channel_identity(full_result: _FullResultLike, channel_id: int) -> tuple[str | None, str | None]:
    """Extract the channel title and username from the live result, if present."""
    from telethon.utils import get_peer_id

    for chat in full_result.chats or []:
        try:
            if int(cast(int | str, get_peer_id(chat))) == int(channel_id):
                return chat.title, chat.username
        except TypeError, ValueError:
            continue
    return None, None


def _write_linked_chat_resolution(payload: _LinkedChatCacheWrite) -> None:
    """Persist the live linked-chat answer and sibling fields."""
    has_sibling_fields = any(k in payload.existing_blob for k in ("subscribers_count", "pinned_msg_id", "about"))
    try:
        with payload.conn:
            payload.conn.execute(
                "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) VALUES (?, 'channel', ?, ?, ?)",
                (payload.channel_id, payload.channel_name, payload.channel_username, payload.now),
            )
            payload.conn.execute(
                "INSERT INTO dialogs (dialog_id, linked_chat_id, linked_chat_resolved_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(dialog_id) DO UPDATE SET "
                "    linked_chat_id = excluded.linked_chat_id, "
                "    linked_chat_resolved_at = excluded.linked_chat_resolved_at",
                (payload.channel_id, payload.linked_chat_id, payload.now),
            )
            if payload.existing_detail_row is not None or has_sibling_fields:
                payload.conn.execute(
                    "INSERT OR REPLACE INTO entity_details (entity_id, detail_json, fetched_at) VALUES (?, ?, ?)",
                    (payload.channel_id, json.dumps(payload.existing_blob), payload.now),
                )
    except sqlite3.Error:
        logger.debug(
            "activity_peer_resolve_linked_cache_write_error channel_id=%r",
            payload.channel_id,
            exc_info=True,
        )


async def _resolve_linked_chat_live(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    channel_id: int,
    now: int,
) -> LinkedChatResolution:
    """Run the live Telethon fetch and persist the linked-chat cache."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    input_channel = cast(TypeInputChannel | None, await resolve_input_peer(client, channel_id))
    if input_channel is None:
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)

    full_result = cast(_FullResultLike, await client(GetFullChannelRequest(channel=input_channel)))
    full_chat = full_result.full_chat
    linked_chat_id = _normalize_linked_chat_id(full_chat.linked_chat_id)
    existing_blob, existing_detail_row = _load_existing_detail_blob(conn, channel_id)
    merged_blob = _merge_sibling_linked_chat_fields(full_chat, existing_blob)
    channel_name, channel_username = _extract_channel_identity(full_result, channel_id)
    _write_linked_chat_resolution(
        _LinkedChatCacheWrite(
            conn=conn,
            channel_id=channel_id,
            linked_chat_id=linked_chat_id,
            existing_blob=merged_blob,
            existing_detail_row=existing_detail_row,
            channel_name=channel_name,
            channel_username=channel_username,
            now=now,
        )
    )
    return LinkedChatResolution(linked_chat_id=linked_chat_id, flood_wait_seconds=None)


async def resolve_linked_chat_id(
    client: _ActivityClient,
    conn: sqlite3.Connection,
    channel_id: int,
) -> LinkedChatResolution:
    """Dialogs-first linked-chat resolver for a broadcast channel.

    Resolution order:
    1. Assert schema v24+ (dialogs.linked_chat_id + linked_chat_resolved_at
       columns must exist). A half-migrated connection raises RuntimeError —
       never silently degrades to live fetch (which would re-create the
       ban-trigger pattern Phase 54 eliminates).
    2. Read dialogs.linked_chat_resolved_at for channel_id. NOT NULL = we have
       a definitive answer — return it immediately with no Telethon call.
       NULL (or no row) = fall through to live GetFullChannelRequest.
    3. On live fetch success: UPSERT dialogs(dialog_id, linked_chat_id,
       linked_chat_resolved_at = now). Only the two linked-chat columns are
       updated — name/type/hidden/members etc. are never touched here.
       Sibling fields (subscribers_count, about, pinned_msg_id) still flow
       into entity_details.detail_json as before.
    4. On FloodWaitError: do NOT touch dialogs. resolved_at stays NULL, which
       IS the retry signal. The next sweep cycle re-attempts naturally.
    5. A channel with no linked chat returns linked_chat_id=None,
       flood_wait_seconds=None (distinct from a flood wait by flood_wait_seconds
       being None).

    No TTL on linked_chat_resolved_at: a definitive answer stays definitive
    until the event handler (plan 03) refreshes it on a real UpdateChannel.

    Never raises into the caller (beyond the schema-floor RuntimeError on
    misconfigured connections).
    """
    from telethon.errors import FloodWaitError

    _assert_linked_chat_schema(conn)
    cached_resolution = _read_cached_linked_chat(conn, channel_id)
    if cached_resolution is not None:
        return cached_resolution

    now = int(time.time())

    try:
        return await _resolve_linked_chat_live(client, conn, channel_id, now)
    except FloodWaitError as exc:
        logger.warning(
            "activity_peer_resolve_linked_flood channel_id=%r flood_wait_seconds=%d",
            channel_id,
            exc.seconds,
        )
        # FloodWait-NEUTRAL: do NOT sleep — surface wait to calling tier.
        # D-08: do NOT touch dialogs — resolved_at stays NULL, which IS the retry
        # signal. The next sweep cycle will re-attempt naturally.
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=int(exc.seconds))
    except Exception:
        logger.debug("activity_peer_resolve_linked_error channel_id=%r", channel_id, exc_info=True)
        # D-08 (generic error path): do NOT touch dialogs — resolved_at stays NULL
        # so the next sweep pass retries naturally.
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
