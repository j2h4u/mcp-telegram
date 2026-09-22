"""Low-level peer and linked-chat resolution primitives.

Acyclic low-level module: no imports from higher-level coordinator modules.
Importable by both sweep and API layers without creating import cycles.

This module owns:
  - resolve_input_peer: entity-type-aware dialog_id → InputPeer
  - resolve_linked_chat_id: cache-first linked_chat_id resolver
  - LinkedChatResolution: typed result dataclass
"""

import json
import logging
import sqlite3
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Protocol, cast

from telethon.tl.types import TypeInputChannel, TypeInputPeer

from .activity_substrate import ActivityClient, call_with_timeout
from .entity_store import EntitySnapshot, ensure_entity_stub, upsert_entity_snapshots
from .flood import TelegramRpcThrottled, _raise_if_latched
from .resolver import latinize
from .telegram_demand import RpcAttemptBudgetExhaustedError
from .telegram_rpc_scheduler import RpcAdmissionClosedError

logger = logging.getLogger(__name__)

_MIN_LINKED_CHAT_SCHEMA_VERSION = 24
_PROFILE_REVISION_INDEX = 2


@dataclass
class LinkedChatResolution:
    """Result of a linked-chat resolution attempt.

    linked_chat_id: int | None
        The normalized -100… discussion-group peer id, or None when the
        channel has no discussion group OR when TelegramRpcThrottled was caught
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
    observed_profile_revision: int | None
    observed_fetched_at: int | None
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
    except RpcAttemptBudgetExhaustedError:
        raise
    except RpcAdmissionClosedError:
        raise
    except TelegramRpcThrottled as exc:
        _raise_if_latched(exc)
        logger.debug("activity_peer_resolve_input_peer_throttled dialog_id=%r", dialog_id)
        return None
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
) -> tuple[dict[str, object], tuple[str] | None, int | None, int | None]:
    """Load the current entity_details JSON payload, if any."""
    detail_rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(entity_details)").fetchall())
    detail_columns = {str(row[1]) for row in detail_rows}
    selected = ["detail_json", "fetched_at"]
    if "profile_revision" in detail_columns:
        selected.append("profile_revision")
    row = cast(
        tuple[object, ...] | None,
        conn.execute(f"SELECT {', '.join(selected)} FROM entity_details WHERE entity_id = ?", (channel_id,)).fetchone(),
    )
    if row is None or not isinstance(row[0], str):
        return {}, None, None, None
    existing_detail_row = (row[0],)
    fetched_at = _optional_int(row[1])
    observed_revision = _optional_int(row[2]) if len(row) > _PROFILE_REVISION_INDEX else None
    try:
        decoded = cast(object, json.loads(row[0]))
        detail = cast(dict[str, object], decoded) if isinstance(decoded, dict) else {}
        return detail, existing_detail_row, observed_revision, fetched_at
    except json.JSONDecodeError:
        return {}, existing_detail_row, observed_revision, fetched_at


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


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


def _write_linked_entity_identity(payload: _LinkedChatCacheWrite) -> None:
    ensure_entity_stub(
        payload.conn,
        EntitySnapshot(
            entity_id=payload.channel_id,
            entity_type="channel",
            name=payload.channel_name,
            username=payload.channel_username,
            name_normalized=latinize(payload.channel_name) if isinstance(payload.channel_name, str) else None,
            updated_at=payload.now,
        ),
    )
    current = cast(
        tuple[object, ...] | None,
        payload.conn.execute(
            "SELECT name, username, name_normalized FROM entities WHERE id=?", (payload.channel_id,)
        ).fetchone(),
    )
    current_name, current_username, current_normalized = (current or (None, None, None))[:3]
    current_name = current_name if isinstance(current_name, str) else None
    current_username = current_username if isinstance(current_username, str) else None
    observed_name = _clean_linked_name(payload.channel_name)
    observed_username = _clean_linked_name(payload.channel_username)
    upsert_entity_snapshots(
        payload.conn,
        [
            EntitySnapshot(
                entity_id=payload.channel_id,
                entity_type="channel",
                name=observed_name or current_name,
                username=observed_username or current_username,
                name_normalized=_linked_name_normalized(observed_name, current_name, current_normalized),
                updated_at=payload.now,
            )
        ],
    )


def _clean_linked_name(value: str | None) -> str | None:
    return value.strip() if isinstance(value, str) else None


def _linked_name_normalized(observed_name: str | None, current_name: object, current_normalized: object) -> str | None:
    if observed_name is None and isinstance(current_normalized, str):
        return current_normalized
    next_name = observed_name or current_name
    return latinize(next_name) if isinstance(next_name, str) else None


def _linked_detail_columns(conn: sqlite3.Connection) -> set[str]:
    rows = cast(list[tuple[object, ...]], conn.execute("PRAGMA table_info(entity_details)").fetchall())
    return {str(row[1]) for row in rows}


def _write_linked_detail(payload: _LinkedChatCacheWrite, detail_columns: set[str]) -> int | None:
    if payload.existing_detail_row is None and not _has_linked_sibling_fields(payload.existing_blob):
        return None
    if payload.existing_detail_row is None:
        if not _insert_linked_detail(payload, detail_columns):
            return None
    elif not _update_linked_detail(payload, detail_columns):
        return None
    return _linked_detail_revision(payload, detail_columns)


def _has_linked_sibling_fields(blob: dict[str, object]) -> bool:
    return any(key in blob for key in ("subscribers_count", "pinned_msg_id", "about"))


def _update_linked_detail(payload: _LinkedChatCacheWrite, detail_columns: set[str]) -> bool:
    encoded = json.dumps(payload.existing_blob)
    assignments = "detail_json=?, fetched_at=?"
    if "profile_revision" in detail_columns:
        assignments += ", profile_revision=profile_revision+1"
        cas = " AND profile_revision=?"
        cas_value: tuple[object, ...] = (payload.observed_profile_revision or 0,)
    else:
        cas = " AND fetched_at=?"
        cas_value = (payload.observed_fetched_at,)
    changed = payload.conn.execute(
        "UPDATE entity_details SET " + assignments + " WHERE entity_id=?" + cas,
        (encoded, payload.now, payload.channel_id, *cas_value),
    ).rowcount
    return changed == 1


def _insert_linked_detail(payload: _LinkedChatCacheWrite, detail_columns: set[str]) -> bool:
    columns = ["entity_id", "detail_json", "fetched_at"]
    values: list[object] = [payload.channel_id, json.dumps(payload.existing_blob), payload.now]
    if "profile_revision" in detail_columns:
        columns.append("profile_revision")
        refresh_columns = _table_columns(payload.conn, "entity_profile_refresh_state")
        if {"profile_revision", "status"} <= refresh_columns:
            changed = payload.conn.execute(
                f"INSERT OR IGNORE INTO entity_details({', '.join(columns)}) "
                "SELECT ?, ?, ?, COALESCE((SELECT profile_revision + 1 "
                "FROM entity_profile_refresh_state WHERE entity_id=? "
                "AND status IN ('pending', 'failed')), 1)",
                (payload.channel_id, json.dumps(payload.existing_blob), payload.now, payload.channel_id),
            ).rowcount
            return changed == 1
        values.append(1)
    changed = payload.conn.execute(
        f"INSERT INTO entity_details({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)}) "
        "ON CONFLICT(entity_id) DO NOTHING",
        values,
    ).rowcount
    return changed == 1


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = cast(list[tuple[object, ...]], conn.execute(f"PRAGMA table_info({table})").fetchall())
    return {str(row[1]) for row in rows}


def _linked_detail_revision(payload: _LinkedChatCacheWrite, detail_columns: set[str]) -> int | None:
    if "profile_revision" not in detail_columns:
        return None
    row = cast(
        tuple[object, ...] | None,
        payload.conn.execute(
            "SELECT profile_revision FROM entity_details WHERE entity_id=?", (payload.channel_id,)
        ).fetchone(),
    )
    return _optional_int(row[0]) if row is not None else None


def _carry_linked_refresh_revision(conn: sqlite3.Connection, channel_id: int, revision: int | None) -> None:
    if revision is None:
        return
    refresh_columns = _table_columns(conn, "entity_profile_refresh_state")
    if "profile_revision" in refresh_columns:
        conn.execute(
            "UPDATE entity_profile_refresh_state SET profile_revision=? "
            "WHERE entity_id=? AND status IN ('pending', 'failed') AND profile_revision<=?",
            (revision, channel_id, revision),
        )


def _write_linked_chat_resolution(payload: _LinkedChatCacheWrite) -> None:
    """Persist the live linked-chat answer and sibling fields."""
    try:
        with payload.conn:
            _write_linked_entity_identity(payload)
            payload.conn.execute(
                "INSERT INTO dialogs (dialog_id, linked_chat_id, linked_chat_resolved_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(dialog_id) DO UPDATE SET "
                "    linked_chat_id = excluded.linked_chat_id, "
                "    linked_chat_resolved_at = excluded.linked_chat_resolved_at",
                (payload.channel_id, payload.linked_chat_id, payload.now),
            )
            revision = _write_linked_detail(payload, _linked_detail_columns(payload.conn))
            _carry_linked_refresh_revision(payload.conn, payload.channel_id, revision)
    except sqlite3.Error:
        logger.debug(
            "activity_peer_resolve_linked_cache_write_error channel_id=%r",
            payload.channel_id,
            exc_info=True,
        )


async def _resolve_linked_chat_live(
    client: ActivityClient,
    conn: sqlite3.Connection,
    channel_id: int,
    now: int,
    timeout_s: float | None,
) -> LinkedChatResolution:
    """Run the live Telethon fetch and persist the linked-chat cache."""
    from telethon.tl.functions.channels import GetFullChannelRequest

    input_channel = cast(TypeInputChannel | None, await resolve_input_peer(client, channel_id))
    if input_channel is None:
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)

    request = GetFullChannelRequest(channel=input_channel)
    if timeout_s is None:
        full_result = cast(_FullResultLike, await client(request))
    else:
        full_result = cast(_FullResultLike, await call_with_timeout(client, request, timeout_s=timeout_s))
    full_chat = full_result.full_chat
    linked_chat_id = _normalize_linked_chat_id(full_chat.linked_chat_id)
    existing_blob, existing_detail_row, observed_profile_revision, observed_fetched_at = _load_existing_detail_blob(
        conn, channel_id
    )
    merged_blob = _merge_sibling_linked_chat_fields(full_chat, existing_blob)
    channel_name, channel_username = _extract_channel_identity(full_result, channel_id)
    _write_linked_chat_resolution(
        _LinkedChatCacheWrite(
            conn=conn,
            channel_id=channel_id,
            linked_chat_id=linked_chat_id,
            existing_blob=merged_blob,
            existing_detail_row=existing_detail_row,
            observed_profile_revision=observed_profile_revision,
            observed_fetched_at=observed_fetched_at,
            channel_name=channel_name,
            channel_username=channel_username,
            now=now,
        )
    )
    return LinkedChatResolution(linked_chat_id=linked_chat_id, flood_wait_seconds=None)


async def resolve_linked_chat_id(
    client: ActivityClient,
    conn: sqlite3.Connection,
    channel_id: int,
    *,
    timeout_s: float | None = None,
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
    4. On TelegramRpcThrottled: do NOT touch dialogs. resolved_at stays NULL, which
       IS the retry signal. The next sweep cycle re-attempts naturally.
    5. A channel with no linked chat returns linked_chat_id=None,
       flood_wait_seconds=None (distinct from a flood wait by flood_wait_seconds
       being None).

    No TTL on linked_chat_resolved_at: a definitive answer stays definitive
    until the event handler (plan 03) refreshes it on a real UpdateChannel.

    Never raises into the caller (beyond the schema-floor RuntimeError on
    misconfigured connections).
    """
    _assert_linked_chat_schema(conn)
    cached_resolution = _read_cached_linked_chat(conn, channel_id)
    if cached_resolution is not None:
        return cached_resolution

    now = int(time.time())

    try:
        return await _resolve_linked_chat_live(client, conn, channel_id, now, timeout_s)
    except RpcAttemptBudgetExhaustedError:
        raise
    except RpcAdmissionClosedError:
        raise
    except TelegramRpcThrottled as exc:
        logger.warning(
            "activity_peer_resolve_linked_flood channel_id=%r flood_wait_seconds=%s",
            channel_id,
            exc.retry_after_seconds,
        )
        # Throttling-neutral: do NOT sleep — surface wait to calling tier.
        # D-08: do NOT touch dialogs — resolved_at stays NULL, which IS the retry
        # signal. The next sweep cycle will re-attempt naturally.
        _raise_if_latched(exc)
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=exc.retry_after_seconds)
    except Exception:
        logger.debug("activity_peer_resolve_linked_error channel_id=%r", channel_id, exc_info=True)
        # D-08 (generic error path): do NOT touch dialogs — resolved_at stays NULL
        # so the next sweep pass retries naturally.
        return LinkedChatResolution(linked_chat_id=None, flood_wait_seconds=None)
