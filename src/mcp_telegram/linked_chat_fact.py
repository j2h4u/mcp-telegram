"""Canonical owner for broadcast-channel discussion-group facts and demand."""

from __future__ import annotations

import sqlite3
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, cast, runtime_checkable

from telethon.tl.types import TypeInputChannel, TypeInputPeer, TypePeer

from .access_lifecycle import not_access_lost_sql
from .activity_substrate import call_with_timeout

_LINKED_CHAT_FACT_STATE_TABLE = "linked_chat_fact_state"


@dataclass(frozen=True, slots=True)
class LinkedChatRetryPolicy:
    base_delay_seconds: int = 300
    max_delay_seconds: int = 86_400

    def delay_seconds(self, failure_count: int, flood_wait_seconds: int | None = None) -> int:
        exponent = min(max(0, int(failure_count)), 9)
        exponential_delay = min(self.max_delay_seconds, self.base_delay_seconds * (1 << exponent))
        if flood_wait_seconds is None:
            return exponential_delay
        flood_delay = max(1, min(self.max_delay_seconds, int(flood_wait_seconds)))
        return max(exponential_delay, flood_delay)

    def effective_retry_at(self, retry_at: int | None, requested_at: int | None, now: int) -> int:
        if retry_at is not None:
            return int(retry_at)
        anchor = int(now) if requested_at is None else int(requested_at)
        return anchor + self.base_delay_seconds


_LINKED_CHAT_RETRY_POLICY = LinkedChatRetryPolicy()


class LinkedChatState(Enum):
    UNKNOWN = "unknown"
    KNOWN_NONE = "known_none"
    KNOWN_LINK = "known_link"


@dataclass(frozen=True)
class LinkedChatFact:
    state: LinkedChatState
    linked_chat_id: int | None
    resolved_at: int | None
    refresh_pending: bool
    requested_at: int | None
    retry_at: int | None
    suspended: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.refresh_pending, bool):
            raise TypeError("refresh_pending must be a bool")


@dataclass(frozen=True)
class LinkedChatWork:
    channel_id: int
    generation: int
    retry_at: int


class _TelethonSession(Protocol):
    def get_input_entity(self, entity: TypePeer) -> TypeInputPeer: ...


@runtime_checkable
class _ClientWithSession(Protocol):
    session: _TelethonSession | None


class _ChatPayload(Protocol):
    id: object


class _FullChatPayload(Protocol):
    id: object
    linked_chat_id: object


class _FullChannelPayload(Protocol):
    full_chat: object
    chats: Sequence[object]


def _canonical_link(raw: object) -> int | None:
    if raw is None or (type(raw) is int and raw == 0):
        return None
    if type(raw) is not int or raw <= 0:
        raise ValueError("linked_chat_id must be a positive integer, zero, or None")
    from telethon.tl.types import PeerChannel
    from telethon.utils import get_peer_id

    return int(get_peer_id(PeerChannel(raw)))


def normalize_linked_chat_id(raw: int | None) -> int | None:
    """Normalize a validated positive Telegram channel id to a peer id."""
    return _canonical_link(raw)


def _ensure_ledger(conn: sqlite3.Connection, channel_id: int) -> None:
    conn.execute(
        f"INSERT OR IGNORE INTO {_LINKED_CHAT_FACT_STATE_TABLE}(channel_id) VALUES (?)",
        (int(channel_id),),
    )


def _read_dialog_fact(conn: sqlite3.Connection, channel_id: int) -> tuple[int | None, int | None]:
    row = cast(
        tuple[int | None, int | None] | None,
        conn.execute(
            "SELECT linked_chat_id, linked_chat_resolved_at FROM dialogs WHERE dialog_id=?",
            (int(channel_id),),
        ).fetchone(),
    )
    if row is None:
        return None, None
    return (None if row[1] is None else int(row[1]), None if row[0] is None else int(row[0]))


def _read_ledger_demand(conn: sqlite3.Connection, channel_id: int) -> tuple[bool, int | None, int | None]:
    ledger = cast(
        tuple[int, int | None, int | None, int | None] | None,
        conn.execute(
            f"SELECT generation, pending_generation, requested_at, retry_at "
            f"FROM {_LINKED_CHAT_FACT_STATE_TABLE} WHERE channel_id=?",
            (int(channel_id),),
        ).fetchone(),
    )
    if ledger is None:
        return False, None, None
    pending = ledger[1] is not None
    requested_at = None if ledger is None or ledger[2] is None else int(ledger[2])
    retry_at = None if ledger is None or ledger[3] is None else int(ledger[3])
    return pending, requested_at, retry_at


def _linked_chat_state(resolved_at: int | None, linked_chat_id: int | None) -> LinkedChatState:
    return (
        LinkedChatState.UNKNOWN
        if resolved_at is None
        else LinkedChatState.KNOWN_NONE
        if linked_chat_id is None
        else LinkedChatState.KNOWN_LINK
    )


def _is_suspended(conn: sqlite3.Connection, channel_id: int, pending: bool, resolved_at: int | None) -> bool:
    if not pending or resolved_at is not None:
        return False
    row = cast(
        tuple[int],
        conn.execute(f"SELECT NOT ({not_access_lost_sql('?')})", (int(channel_id),)).fetchone(),
    )
    return bool(row[0])


def read_fact(conn: sqlite3.Connection, channel_id: int) -> LinkedChatFact:
    resolved_at, linked_chat_id = _read_dialog_fact(conn, channel_id)
    pending, requested_at, retry_at = _read_ledger_demand(conn, channel_id)
    return LinkedChatFact(
        _linked_chat_state(resolved_at, linked_chat_id),
        linked_chat_id,
        resolved_at,
        pending,
        requested_at,
        retry_at,
        _is_suspended(conn, channel_id, pending, resolved_at),
    )


def capture_generation(conn: sqlite3.Connection, channel_id: int) -> int:
    """Return the retained per-channel generation before starting an RPC."""
    _ensure_ledger(conn, channel_id)
    row = cast(
        tuple[int],
        conn.execute(
            f"SELECT generation FROM {_LINKED_CHAT_FACT_STATE_TABLE} WHERE channel_id=?",
            (int(channel_id),),
        ).fetchone(),
    )
    return int(row[0])


def ensure_cold_demand(conn: sqlite3.Connection, channel_id: int, now: int) -> bool:
    """Create a retryable cold demand once; repeated callers leave it unchanged."""
    _ensure_ledger(conn, channel_id)
    cursor = conn.execute(
        f"UPDATE {_LINKED_CHAT_FACT_STATE_TABLE} SET pending_generation=generation, "
        "requested_at=?, retry_at=?, failure_count=0 "
        "WHERE channel_id=? AND pending_generation IS NULL",
        (int(now), int(now), int(channel_id)),
    )
    return cursor.rowcount == 1


def invalidate_from_update(conn: sqlite3.Connection, channel_id: int, now: int) -> bool:
    """Advance generation and make an eligible UpdateChannel immediately due."""
    _ensure_ledger(conn, channel_id)
    conn.execute(
        f"UPDATE {_LINKED_CHAT_FACT_STATE_TABLE} SET generation=generation+1, "
        "pending_generation=generation+1, requested_at=?, retry_at=?, failure_count=0 WHERE channel_id=?",
        (int(now), int(now), int(channel_id)),
    )
    return True


def next_release_at(conn: sqlite3.Connection) -> int | None:
    row = cast(
        tuple[int | None] | None,
        conn.execute(
            f"SELECT MIN(COALESCE(s.retry_at, s.requested_at+{_LINKED_CHAT_RETRY_POLICY.base_delay_seconds})) "
            f"FROM {_LINKED_CHAT_FACT_STATE_TABLE} AS s "
            "WHERE s.pending_generation IS NOT NULL AND " + not_access_lost_sql("s.channel_id")
        ).fetchone(),
    )
    return None if row is None or row[0] is None else int(row[0])


def next_due(conn: sqlite3.Connection, now: int) -> LinkedChatWork | None:
    # Repair pending rows missing retry_at once, using a bounded delay.
    conn.execute(
        f"UPDATE {_LINKED_CHAT_FACT_STATE_TABLE} SET retry_at=COALESCE(requested_at, ?)+? "
        "WHERE pending_generation IS NOT NULL AND retry_at IS NULL "
        "AND " + not_access_lost_sql("linked_chat_fact_state.channel_id"),
        (int(now), _LINKED_CHAT_RETRY_POLICY.base_delay_seconds),
    )
    row = cast(
        tuple[int, int, int] | None,
        conn.execute(
            f"SELECT s.channel_id, s.pending_generation, COALESCE(s.retry_at, "
            f"s.requested_at+{_LINKED_CHAT_RETRY_POLICY.base_delay_seconds}) "
            f"FROM {_LINKED_CHAT_FACT_STATE_TABLE} AS s "
            "WHERE s.pending_generation IS NOT NULL AND s.retry_at<=? "
            "AND " + not_access_lost_sql("s.channel_id") + " "
            "ORDER BY s.retry_at, s.channel_id LIMIT 1",
            (int(now),),
        ).fetchone(),
    )
    return None if row is None else LinkedChatWork(int(row[0]), int(row[1]), int(row[2]))


def defer(conn: sqlite3.Connection, work: LinkedChatWork, retry_at: int) -> bool:
    """Record bounded backoff only while the captured pending generation is current."""
    cursor = conn.execute(
        f"UPDATE {_LINKED_CHAT_FACT_STATE_TABLE} SET failure_count=failure_count+1, retry_at=? "
        "WHERE channel_id=? AND pending_generation=? AND generation=?",
        (int(retry_at), int(work.channel_id), int(work.generation), int(work.generation)),
    )
    return cursor.rowcount == 1


def _retry_at_for_failure(
    conn: sqlite3.Connection,
    work: LinkedChatWork,
    now: int,
    policy: LinkedChatRetryPolicy,
    flood_wait_seconds: int | None,
) -> int:
    row = cast(
        tuple[int] | None,
        conn.execute(
            f"SELECT failure_count FROM {_LINKED_CHAT_FACT_STATE_TABLE} WHERE channel_id=? "
            "AND pending_generation=? AND generation=?",
            (work.channel_id, work.generation, work.generation),
        ).fetchone(),
    )
    failures = 0 if row is None else int(row[0])
    return int(now) + policy.delay_seconds(failures, flood_wait_seconds)


def publish(
    conn: sqlite3.Connection,
    channel_id: int,
    generation: int,
    validated_link: int | None,
    observed_at: int,
) -> bool:
    """Publish only if no newer event/publication advanced this channel generation."""
    canonical_link = _canonical_link(validated_link)
    _ensure_ledger(conn, channel_id)
    cursor = conn.execute(
        f"UPDATE {_LINKED_CHAT_FACT_STATE_TABLE} SET generation=generation+1, pending_generation=NULL, "
        "requested_at=NULL, retry_at=NULL, failure_count=0 WHERE channel_id=? AND generation=?",
        (int(channel_id), int(generation)),
    )
    if cursor.rowcount != 1:
        return False
    conn.execute(
        "INSERT INTO dialogs(dialog_id, linked_chat_id, linked_chat_resolved_at) VALUES (?, ?, ?) "
        "ON CONFLICT(dialog_id) DO UPDATE SET linked_chat_id=excluded.linked_chat_id, "
        "linked_chat_resolved_at=excluded.linked_chat_resolved_at",
        (int(channel_id), canonical_link, int(observed_at)),
    )
    return True


def _full_chat_fields(full_result: object) -> tuple[int, int | None, Sequence[object]]:
    """Extract the required FullChannel fields, rejecting incomplete payloads."""
    result = cast(_FullChannelPayload, full_result)
    full_chat_value = cast(object | None, getattr(result, "full_chat", None))
    chats_value = cast(object | None, getattr(result, "chats", None))
    if full_chat_value is None or not isinstance(chats_value, Sequence):
        raise ValueError("GetFullChannel response is missing full_chat or chats")
    if not hasattr(full_chat_value, "id") or not hasattr(full_chat_value, "linked_chat_id"):
        raise ValueError("GetFullChannel response has malformed channel or link fields")
    full_chat = cast(_FullChatPayload, full_chat_value)
    full_id = full_chat.id
    raw_link = full_chat.linked_chat_id
    if type(full_id) is not int or type(raw_link) not in (int, type(None)):
        raise ValueError("GetFullChannel response has malformed channel or link fields")
    return full_id, cast(int | None, raw_link), cast(Sequence[object], chats_value)


def _canonical_peer_channel_id(channel_id: int) -> int:
    """Convert a raw Telegram channel id to its canonical peer id."""
    from telethon.tl.types import PeerChannel
    from telethon.utils import get_peer_id

    try:
        return int(get_peer_id(PeerChannel(channel_id)))
    except (TypeError, ValueError) as error:
        raise ValueError("GetFullChannel response contains malformed peer ids") from error


def _response_contains_channel(chats: Sequence[object], channel_id: int) -> bool:
    for chat in chats:
        if not hasattr(chat, "id"):
            continue
        chat_id = cast(_ChatPayload, chat).id
        if type(chat_id) is int and _canonical_peer_channel_id(chat_id) == channel_id:
            return True
    return False


def validate_observation(full_result: object, channel_id: int) -> int | None:
    """Validate the matching FullChannel payload and return raw linked_chat_id."""
    full_id, linked_chat_id, chats = _full_chat_fields(full_result)
    if _canonical_peer_channel_id(full_id) != int(channel_id):
        raise ValueError("GetFullChannel response belongs to a different channel")
    if not _response_contains_channel(chats, int(channel_id)):
        raise ValueError("GetFullChannel response does not include the requested channel")
    if linked_chat_id is not None and linked_chat_id < 0:
        raise ValueError("GetFullChannel response has a negative linked_chat_id")
    return linked_chat_id


class _RpcOnlyActivityClient:
    def __init__(self, rpc: Callable[[object], Awaitable[object]]) -> None:
        self._rpc = rpc

    def __call__(self, request: object) -> Coroutine[object, object, object]:
        async def run() -> object:
            return await self._rpc(request)

        return run()

    def get_input_entity(self, dialog_id: int) -> Coroutine[object, object, object]:
        del dialog_id

        async def unavailable() -> object:
            raise RuntimeError("input-peer lookup is not part of this owner adapter")

        return unavailable()


class LinkedChatFactOwner:
    """Named application owner for the linked-chat fact and durable demand."""

    retry_policy = _LINKED_CHAT_RETRY_POLICY
    read_fact = staticmethod(read_fact)
    capture_generation = staticmethod(capture_generation)
    ensure_cold_demand = staticmethod(ensure_cold_demand)
    invalidate_from_update = staticmethod(invalidate_from_update)
    next_release_at = staticmethod(next_release_at)
    next_due = staticmethod(next_due)
    defer = staticmethod(defer)
    publish = staticmethod(publish)
    validate_observation = staticmethod(validate_observation)

    def retry_at_for_failure(
        self,
        conn: sqlite3.Connection,
        work: LinkedChatWork,
        now: int,
        *,
        flood_wait_seconds: int | None = None,
    ) -> int:
        return _retry_at_for_failure(conn, work, now, self.retry_policy, flood_wait_seconds)

    def effective_retry_at(self, retry_at: int | None, requested_at: int | None, now: int) -> int:
        return self.retry_policy.effective_retry_at(retry_at, requested_at, now)

    @staticmethod
    def resolve_cached_input_peer(client: object, channel_id: int) -> TypeInputChannel | None:
        from telethon.tl import types

        if not isinstance(client, _ClientWithSession) or client.session is None:
            return None
        raw_channel_id = -int(channel_id) - 1_000_000_000_000
        if raw_channel_id <= 0:
            return None
        try:
            input_peer = client.session.get_input_entity(types.PeerChannel(raw_channel_id))
        except TypeError, ValueError:
            return None
        if isinstance(input_peer, types.InputPeerChannel):
            return types.InputChannel(input_peer.channel_id, input_peer.access_hash)
        return None

    @staticmethod
    async def acquire(
        client: Callable[[object], Awaitable[object]],
        input_channel: TypeInputChannel,
        *,
        timeout_s: float | None = None,
    ) -> object:
        from telethon.tl.functions.channels import GetFullChannelRequest

        request = GetFullChannelRequest(channel=input_channel)
        if timeout_s is None:
            return await client(request)
        return await call_with_timeout(_RpcOnlyActivityClient(client), request, timeout_s=timeout_s)


linked_chat_fact_owner = LinkedChatFactOwner()


__all__ = [
    "LinkedChatFact",
    "LinkedChatFactOwner",
    "LinkedChatRetryPolicy",
    "LinkedChatState",
    "LinkedChatWork",
    "capture_generation",
    "defer",
    "ensure_cold_demand",
    "invalidate_from_update",
    "linked_chat_fact_owner",
    "next_due",
    "next_release_at",
    "normalize_linked_chat_id",
    "publish",
    "read_fact",
    "validate_observation",
]
