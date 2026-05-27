"""Low-level peer and linked-chat resolution primitives.

Acyclic low-level module: no imports from higher-level coordinator modules.
Importable by both sweep and API layers without creating import cycles.

This module owns:
  - resolve_input_peer: entity-type-aware dialog_id → InputPeer
  - resolve_linked_chat_id: cache-first linked_chat_id resolver
  - LinkedChatResolution: typed result dataclass
  - _ENTITY_DETAIL_TTL_SECONDS: canonical TTL constant
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Canonical TTL for entity_details cache reads.
# Owned here as a single source of truth; importable by higher-level layers.
_ENTITY_DETAIL_TTL_SECONDS: int = 300


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


async def resolve_input_peer(client: Any, dialog_id: int) -> Any:
    """Resolve a bare dialog_id to a concrete InputPeer via the Telethon session.

    Uses client.get_input_entity() which is entity-type-aware: it resolves
    channel vs chat vs user and supplies the cached access_hash from the
    session, so we do NOT hand-build InputPeerChannel from the entities table
    (which has no access_hash column).

    Returns None on access-loss, cache miss, or any other lookup failure so
    the caller can skip-and-retry rather than crash.  Never raises.
    """
    try:
        return await client.get_input_entity(dialog_id)
    except Exception:
        logger.debug(
            "activity_peer_resolve_input_peer_miss dialog_id=%r", dialog_id, exc_info=True
        )
        return None


async def resolve_linked_chat_id(
    client: Any,
    conn: sqlite3.Connection,
    channel_id: int,
) -> LinkedChatResolution:
    """Cache-first linked-chat resolver for a broadcast channel.

    Resolution order:
    1. Read entity_details.detail_json for channel_id; if present and fresh
       (within _ENTITY_DETAIL_TTL_SECONDS), return cached linked_chat_id with
       flood_wait_seconds=None — no live call.
    2. On miss / expired / absent, call GetFullChannelRequest exactly once.
       The result is MERGED into the existing detail_json blob (read-modify-
       write) so pre-existing keys like subscribers_count/about/pinned_msg_id
       are preserved.  linked_chat_id is normalized to -100… form.
    3. On FloodWaitError: return LinkedChatResolution(linked_chat_id=None,
       flood_wait_seconds=<n>) immediately — do NOT sleep.  The calling tier
       owns the durable backoff write.
    4. A channel with no linked chat returns linked_chat_id=None,
       flood_wait_seconds=None (distinct from a flood wait by flood_wait_seconds
       being None).

    Never raises into the caller.
    """
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.utils import get_peer_id

    now = int(time.time())

    # --- Cache read ---
    try:
        row = conn.execute(
            "SELECT detail_json, fetched_at FROM entity_details WHERE entity_id = ?",
            (channel_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        logger.debug(
            "activity_peer_resolve_linked_cache_read_error channel_id=%r", channel_id,
            exc_info=True,
        )
        row = None

    if row is not None:
        detail_json_str, fetched_at = row
        if now - fetched_at < _ENTITY_DETAIL_TTL_SECONDS:
            try:
                blob = json.loads(detail_json_str)
                cached_raw = blob.get("linked_chat_id")
                if cached_raw is not None:
                    return LinkedChatResolution(
                        linked_chat_id=int(cached_raw), flood_wait_seconds=None
                    )
                # Explicitly cached as None (no discussion group)
                if "linked_chat_id" in blob:
                    return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
                # Key absent from blob — treat as miss and re-fetch
            except (json.JSONDecodeError, ValueError):
                logger.debug(
                    "activity_peer_resolve_linked_cache_corrupt channel_id=%r", channel_id
                )
    else:
        row = None  # not in cache at all
        detail_json_str = None

    # --- Live fetch ---
    try:
        input_channel = await resolve_input_peer(client, channel_id)
        if input_channel is None:
            # Access-loss / cache miss — cannot resolve, return clean None
            return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)

        full_result = await client(GetFullChannelRequest(channel=input_channel))
        full_chat = full_result.full_chat

        linked_chat_id_raw = getattr(full_chat, "linked_chat_id", None)
        linked_chat_id: int | None = None
        if linked_chat_id_raw is not None:
            # Normalize to -100… canonical form via Telethon's get_peer_id helper
            from telethon.tl.types import PeerChannel
            if linked_chat_id_raw > 0:
                linked_chat_id = int(get_peer_id(PeerChannel(linked_chat_id_raw)))
            else:
                linked_chat_id = int(linked_chat_id_raw)

        # --- Merge into existing blob (read-modify-write, preserve other keys) ---
        existing_blob: dict = {}
        if detail_json_str is not None:
            try:
                existing_blob = json.loads(detail_json_str)
            except json.JSONDecodeError:
                existing_blob = {}

        # Merge: overlay only the fields we freshly fetched
        existing_blob["linked_chat_id"] = linked_chat_id
        # Also update other full_channel fields if present
        subscribers_count = getattr(full_chat, "participants_count", None)
        if subscribers_count is not None:
            existing_blob["subscribers_count"] = subscribers_count
        pinned_msg_id = getattr(full_chat, "pinned_msg_id", None)
        if pinned_msg_id is not None:
            existing_blob["pinned_msg_id"] = pinned_msg_id
        about = getattr(full_chat, "about", None)
        if about is not None:
            existing_blob["about"] = about

        # entity_details.entity_id has a FK to entities(id) with foreign_keys=ON,
        # so the parent row MUST exist first or the write raises sqlite3.IntegrityError
        # (NOT OperationalError). Mirror _get_entity_info: INSERT OR IGNORE the entity
        # before the detail write. Pull name/username from the GetFullChannel result's
        # chats list when available; otherwise a minimal channel row (refreshed later by
        # entity_info). Catch sqlite3.Error so a cache-write failure can never bubble to
        # the outer handler and masquerade as "no linked chat".
        channel_name: str | None = None
        channel_username: str | None = None
        for chat in getattr(full_result, "chats", None) or []:
            try:
                if int(get_peer_id(chat)) == int(channel_id):
                    channel_name = getattr(chat, "title", None)
                    channel_username = getattr(chat, "username", None)
                    break
            except (TypeError, ValueError):
                continue
        try:
            with conn:
                conn.execute(
                    "INSERT OR IGNORE INTO entities (id, type, name, username, updated_at) "
                    "VALUES (?, 'channel', ?, ?, ?)",
                    (channel_id, channel_name, channel_username, now),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO entity_details (entity_id, detail_json, fetched_at) "
                    "VALUES (?, ?, ?)",
                    (channel_id, json.dumps(existing_blob), now),
                )
        except sqlite3.Error:
            logger.debug(
                "activity_peer_resolve_linked_cache_write_error channel_id=%r", channel_id,
                exc_info=True,
            )
            # Continue: still return the live data

        return LinkedChatResolution(linked_chat_id=linked_chat_id, flood_wait_seconds=None)

    except FloodWaitError as exc:
        logger.warning(
            "activity_peer_resolve_linked_flood channel_id=%r flood_wait_seconds=%d",
            channel_id, exc.seconds,
        )
        # FloodWait-NEUTRAL: do NOT sleep — surface wait to calling tier
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=int(exc.seconds))
    except Exception:
        logger.debug(
            "activity_peer_resolve_linked_error channel_id=%r", channel_id, exc_info=True
        )
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
