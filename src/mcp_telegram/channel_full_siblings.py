"""Project accepted GetFullChannel sibling fields without owning linkage."""

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, cast

from telethon.tl import types

from .entity_store import EntitySnapshot, ensure_entity_stub, upsert_entity_snapshots
from .resolver import latinize

_DETAIL_BASE_COLUMN_COUNT = 2


class _FullChatSiblings(Protocol):
    participants_count: object
    pinned_msg_id: object
    about: object


@dataclass(frozen=True, slots=True)
class ChannelFullSiblingsToken:
    """Pre-acquisition snapshot used to fence stale detail projection."""

    profile_revision: int | None
    fetched_at: int | None
    detail_json: str | None


def capture_channel_full_siblings_token(conn: sqlite3.Connection, channel_id: int) -> ChannelFullSiblingsToken:
    columns = _table_columns(conn, "entity_details")
    selected = ["detail_json", "fetched_at"]
    if "profile_revision" in columns:
        selected.append("profile_revision")
    row = cast(
        tuple[object, ...] | None,
        conn.execute(f"SELECT {', '.join(selected)} FROM entity_details WHERE entity_id=?", (channel_id,)).fetchone(),
    )
    if row is None:
        return ChannelFullSiblingsToken(_retained_refresh_revision(conn, channel_id), None, None)
    return ChannelFullSiblingsToken(
        profile_revision=_optional_int(row[_DETAIL_BASE_COLUMN_COUNT])
        if len(row) > _DETAIL_BASE_COLUMN_COUNT
        else None,
        fetched_at=_optional_int(row[1]),
        detail_json=row[0] if isinstance(row[0], str) else None,
    )


def write_channel_full_siblings(
    conn: sqlite3.Connection,
    channel_id: int,
    full_result: object,
    token: ChannelFullSiblingsToken,
    *,
    observed_at: int,
) -> bool:
    """CAS-merge profile siblings and identity under the caller's transaction.

    The caller validates and publishes the linked-chat fact in the same outer
    transaction. This function never reads or writes linked_chat_id.
    """
    sibling_values = _sibling_values(full_result)
    if sibling_values:
        if not _write_sibling_detail(conn, channel_id, sibling_values, token, observed_at):
            return False
    elif not _token_is_current(conn, channel_id, token):
        # Fence identity updates even when the response has no sibling fields.
        return False

    identity = _matching_channel_identity(full_result, channel_id)
    _write_channel_identity(conn, channel_id, identity, observed_at)
    return True


def _sibling_values(full_result: object) -> dict[str, object]:
    full_chat_value = getattr(full_result, "full_chat", None)
    if full_chat_value is None:
        raise ValueError("full-channel result is missing full_chat")
    full_chat = cast(_FullChatSiblings, full_chat_value)
    values = {
        "subscribers_count": getattr(full_chat, "participants_count", None),
        "pinned_msg_id": getattr(full_chat, "pinned_msg_id", None),
        "about": getattr(full_chat, "about", None),
    }
    return {key: value for key, value in values.items() if value is not None}


def _matching_channel_identity(full_result: object, channel_id: int) -> tuple[str | None, str | None]:
    chats = cast(Sequence[object] | None, getattr(full_result, "chats", None)) or ()
    for chat in chats:
        if not isinstance(chat, types.Channel):
            continue
        if -1_000_000_000_000 - chat.id == int(channel_id):
            return chat.title.strip() if isinstance(chat.title, str) else None, (
                chat.username.strip() if isinstance(chat.username, str) else None
            )
    return None, None


def _write_sibling_detail(
    conn: sqlite3.Connection,
    channel_id: int,
    sibling_values: dict[str, object],
    token: ChannelFullSiblingsToken,
    observed_at: int,
) -> bool:
    detail = _decode_detail(token.detail_json)
    detail.update(sibling_values)
    encoded = json.dumps(detail)
    columns = _table_columns(conn, "entity_details")
    if _token_is_empty(token):
        written = _insert_sibling_detail(conn, channel_id, encoded, observed_at, columns)
    else:
        written = _update_sibling_detail(conn, channel_id, encoded, observed_at, token)
    if written:
        _carry_refresh_revision(conn, channel_id)
    return written


def _token_is_empty(token: ChannelFullSiblingsToken) -> bool:
    return token.detail_json is None and token.fetched_at is None


def _insert_sibling_detail(
    conn: sqlite3.Connection,
    channel_id: int,
    encoded: str,
    observed_at: int,
    columns: set[str],
) -> bool:
    ensure_entity_stub(
        conn,
        EntitySnapshot(
            entity_id=channel_id,
            entity_type="channel",
            name=None,
            username=None,
            name_normalized=None,
            updated_at=observed_at,
        ),
    )
    insert_columns = ["entity_id", "detail_json", "fetched_at"]
    values: list[object] = [channel_id, encoded, observed_at]
    if "profile_revision" in columns:
        insert_columns.append("profile_revision")
        values.append(_next_detail_profile_revision(conn, channel_id))
    result = conn.execute(
        f"INSERT INTO entity_details({', '.join(insert_columns)}) VALUES ({', '.join('?' for _ in values)}) "
        "ON CONFLICT(entity_id) DO NOTHING",
        values,
    )
    return result.rowcount == 1


def _update_sibling_detail(
    conn: sqlite3.Connection,
    channel_id: int,
    encoded: str,
    observed_at: int,
    token: ChannelFullSiblingsToken,
) -> bool:
    columns = _table_columns(conn, "entity_details")
    assignments = "detail_json=?, fetched_at=?"
    values: list[object] = [encoded, observed_at]
    if "profile_revision" in columns:
        assignments += ", profile_revision=profile_revision+1"
        guard = "profile_revision=?"
        values.append(token.profile_revision)
    else:
        guard = "fetched_at=? AND detail_json=?"
        values.extend((token.fetched_at, token.detail_json))
    values.append(channel_id)
    result = conn.execute(
        f"UPDATE entity_details SET {assignments} WHERE {guard} AND entity_id=?",
        values,
    )
    return result.rowcount == 1


def _write_channel_identity(
    conn: sqlite3.Connection,
    channel_id: int,
    identity: tuple[str | None, str | None],
    observed_at: int,
) -> None:
    chat_name, chat_username = identity
    if chat_name is None and chat_username is None:
        return
    ensure_entity_stub(
        conn,
        EntitySnapshot(
            entity_id=channel_id,
            entity_type="channel",
            name=chat_name,
            username=chat_username,
            name_normalized=latinize(chat_name) if chat_name is not None else None,
            updated_at=observed_at,
        ),
    )
    name, username, normalized = _merged_identity_values(conn, channel_id, identity)
    upsert_entity_snapshots(
        conn,
        [
            EntitySnapshot(
                entity_id=channel_id,
                entity_type="channel",
                name=name,
                username=username,
                name_normalized=normalized,
                updated_at=observed_at,
            )
        ],
    )


def _merged_identity_values(
    conn: sqlite3.Connection,
    channel_id: int,
    identity: tuple[str | None, str | None],
) -> tuple[str | None, str | None, str | None]:
    current = cast(
        tuple[object, object, object] | None,
        conn.execute("SELECT name, username, name_normalized FROM entities WHERE id=?", (channel_id,)).fetchone(),
    )
    old_name, old_username, old_normalized = current or (None, None, None)
    chat_name, chat_username = identity
    name = chat_name or (old_name if isinstance(old_name, str) else None)
    username = chat_username or (old_username if isinstance(old_username, str) else None)
    normalized = latinize(name) if name is not None else (old_normalized if isinstance(old_normalized, str) else None)
    return name, username, normalized


def _token_is_current(conn: sqlite3.Connection, channel_id: int, token: ChannelFullSiblingsToken) -> bool:
    current = capture_channel_full_siblings_token(conn, channel_id)
    return current == token


def _decode_detail(encoded: str | None) -> dict[str, object]:
    if encoded is None:
        return {}
    try:
        decoded = cast(object, json.loads(encoded))
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _carry_refresh_revision(conn: sqlite3.Connection, channel_id: int) -> None:
    columns = _table_columns(conn, "entity_details")
    refresh_columns = _table_columns(conn, "entity_profile_refresh_state")
    if "profile_revision" not in columns or not {"profile_revision", "status"} <= refresh_columns:
        return
    row = cast(
        tuple[object, ...] | None,
        conn.execute("SELECT profile_revision FROM entity_details WHERE entity_id=?", (channel_id,)).fetchone(),
    )
    revision = _optional_int(row[0]) if row is not None else None
    if revision is not None:
        conn.execute(
            "UPDATE entity_profile_refresh_state SET profile_revision=? "
            "WHERE entity_id=? AND status IN ('pending','failed') AND profile_revision<=?",
            (revision, channel_id, revision),
        )


def _retained_refresh_revision(conn: sqlite3.Connection, channel_id: int) -> int | None:
    refresh_columns = _table_columns(conn, "entity_profile_refresh_state")
    if "profile_revision" not in refresh_columns:
        return None
    row = cast(
        tuple[object, ...] | None,
        conn.execute(
            "SELECT profile_revision FROM entity_profile_refresh_state WHERE entity_id=?",
            (channel_id,),
        ).fetchone(),
    )
    return _optional_int(row[0]) if row is not None else None


def _next_detail_profile_revision(conn: sqlite3.Connection, channel_id: int) -> int:
    retained_revision = _retained_refresh_revision(conn, channel_id)
    return max(1, retained_revision + 1) if retained_revision is not None else 1


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = cast(list[tuple[object, ...]], conn.execute(f"PRAGMA table_info({table})").fetchall())
    return {str(row[1]) for row in rows}


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
