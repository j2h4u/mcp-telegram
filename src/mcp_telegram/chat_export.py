"""Finite, archive-read-only Telegram operations for the streaming CLI export."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from telethon.errors import RPCError, UserNotParticipantError
from telethon.tl import functions, types

from .entity_identity import project_entity_identity
from .flood import TelegramRpcThrottled
from .message_composition import normalize_telegram_fact
from .message_history.contracts import MESSAGE_HISTORY_PAGE_SIZE
from .messages.telegram_adapter import extract_message_row
from .telegram_demand import AcquisitionKind, RpcAttemptBudget, RpcAttemptBudgetExhaustedError, acquisition_context
from .telegram_gateway import fetch_group_profile_response
from .telegram_rpc_error import describe_telegram_rpc_error
from .telegram_rpc_scheduler import (
    RpcAdmissionClosedError,
    RpcAdmissionError,
    TelegramRpcAdmissionDeferred,
    rpc_attempt_budget,
)

EXPORT_OPERATION_SECONDS = 60.0
EXPORT_RESPONSE_BYTES = 1_000_000
EXPORT_ATTEMPTS = 6
CHANNEL_ID_OFFSET = 1000000000000
REACTION_CURSOR_BYTES = 4096


class _Client(Protocol):
    async def get_input_entity(self, dialog_id: int | str) -> object: ...

    async def __call__(self, request: object) -> object: ...


def _integer(req: Mapping[str, object], key: str, *, default: int | None = None) -> int:
    value = req.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    if key == "user_id" and not 0 < value < 2**63:
        raise ValueError("user_id must be a positive Telegram user identifier")
    if key not in {"dialog_id", "user_id"} and not 0 <= value < 2**31:
        raise ValueError(f"{key} must be a nonnegative Telegram identifier")
    return value


def _objects(value: object) -> Sequence[object]:
    return cast(Sequence[object], value) if isinstance(value, (list, tuple)) else ()


def _audit_references(value: object) -> object:
    if isinstance(value, list):
        return [_audit_references(item) for item in value]
    if isinstance(value, dict):
        data = cast(dict[str, object], value)
        if data.get("_") in {"Message", "MessageService", "MessageEmpty"}:
            data = {key: data[key] for key in ("_", "id", "peer_id", "date") if key in data}
        return {key: _audit_references(item) for key, item in data.items()}
    return value


def _raw(value: object, *, audit: bool = False) -> object:
    """Reuse ordinary reading's facts; audit cannot resurrect removed text."""
    normalized = normalize_telegram_fact(value)
    return _audit_references(normalized) if audit else normalized


def _peer(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, (types.PeerUser, types.User, types.UserEmpty)):
        peer_id = value.user_id if isinstance(value, types.PeerUser) else value.id
        return {"id": peer_id, "kind": "user"}
    if isinstance(value, (types.PeerChannel, types.Channel, types.ChannelForbidden)):
        peer_id = value.channel_id if isinstance(value, types.PeerChannel) else value.id
        return {"id": -CHANNEL_ID_OFFSET - peer_id, "kind": "channel"}
    if isinstance(value, (types.PeerChat, types.Chat, types.ChatForbidden)):
        peer_id = value.chat_id if isinstance(value, types.PeerChat) else value.id
        return {"id": -peer_id, "kind": "chat"}
    return None


def _identities(response: object) -> dict[int, object]:
    return {
        cast(int, peer["id"]): entity
        for entity in (*_objects(getattr(response, "users", ())), *_objects(getattr(response, "chats", ())))
        if (peer := _peer(entity)) is not None
    }


def _identity(value: object, entities: Mapping[int, object], conn: sqlite3.Connection) -> dict[str, object] | None:
    peer = _peer(value)
    if peer is None:
        return None
    peer_id = cast(int, peer["id"])
    entity = entities.get(peer_id)
    name: str | None = None
    username: str | None = None
    if entity is not None:
        name = getattr(entity, "title", None) or " ".join(
            str(cast(object, part))
            for part in (getattr(entity, "first_name", None), getattr(entity, "last_name", None))
            if part
        )
        username = getattr(entity, "username", None)
        peer["identity_source"] = "telegram_response"
    else:
        # Only stable display facts may be reused. Current messages and roles
        # always come from Telegram; deleted local archive rows are never read.
        row = cast(
            tuple[object, object] | None,
            conn.execute("SELECT name, username FROM entities WHERE id=?", (peer_id,)).fetchone(),
        )
        if row is not None:
            name = row[0] if isinstance(row[0], str) else None
            username = row[1] if isinstance(row[1], str) else None
            peer["identity_source"] = "local_cache"
        else:
            peer["identity_source"] = "id_only"
    peer.update(project_entity_identity(display_name=name, username=username, telegram_id=peer_id))
    return peer


def _action_user_ids(key: str, value: object) -> list[int]:
    if key in {"user_id", "inviter_id", "promoted_by", "kicked_by"} and type(value) is int:
        return [value]
    if key == "users" and isinstance(value, list):
        return [user_id for user_id in value if type(user_id) is int]
    return []


def _related_users(action: object, entities: Mapping[int, object], conn: sqlite3.Connection) -> list[dict[str, object]]:
    """Identify the people referenced by a service/audit action, without lookups."""
    ids: set[int] = set()

    def visit(value: object) -> None:
        if isinstance(value, dict):
            for key, item in cast(dict[str, object], value).items():
                ids.update(_action_user_ids(key, item))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(_raw(action, audit=True))
    return [
        identity
        for user_id in sorted(ids)
        if (identity := _identity(types.PeerUser(user_id), entities, conn)) is not None
    ]


def _selector(req: Mapping[str, object]) -> int | str:
    value = req.get("dialog_id")
    if isinstance(value, str):
        username = value.removeprefix("https://t.me/").removeprefix("@").rstrip("/")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
            raise ValueError("Use a public Telegram group URL or @username")
        return username
    return _integer(req, "dialog_id")


async def _resolve(client: _Client, dialog_id: int | str) -> object:
    with acquisition_context(AcquisitionKind.ENTITY_LOOKUP):
        peer = await client.get_input_entity(dialog_id)
    if not isinstance(peer, (types.InputPeerChannel, types.InputPeerChat)):
        raise ValueError("export-chat requires a group peer, not a user or bot")
    return peer


async def _send(client: _Client, request: object, kind: AcquisitionKind) -> object:
    with acquisition_context(kind):
        return await client(request)


async def _legacy_group(client: _Client, peer: types.InputPeerChat, kind: AcquisitionKind) -> object:
    with acquisition_context(kind):
        return await fetch_group_profile_response(client, -peer.chat_id)


async def _history_response(client: _Client, peer: object, before: int, upper: int, limit: int) -> object:
    return await _send(
        client,
        functions.messages.GetHistoryRequest(
            peer=cast(types.TypeInputPeer, peer),
            offset_id=before,
            offset_date=None,
            add_offset=0,
            limit=limit,
            max_id=upper + 1 if upper else 0,
            min_id=0,
            hash=0,
        ),
        AcquisitionKind.MESSAGE_HISTORY_PAGE,
    )


def _message(
    item: object, dialog_id: int, entities: Mapping[int, object], conn: sqlite3.Connection
) -> dict[str, object]:
    stored = extract_message_row(dialog_id, item).message
    reply = getattr(item, "reply_to", None)
    reply_id = getattr(reply, "reply_to_msg_id", None)
    reactions = getattr(item, "reactions", None)
    aggregate = _raw(reactions)
    counts = _objects(getattr(reactions, "results", ()))
    status = "unknown" if reactions is None else "pending" if counts else "known_empty"
    return {
        "id": stored.message_id,
        "date": _raw(getattr(item, "date", None)),
        "dialog_id": dialog_id,
        "kind": "service" if stored.is_service else "message",
        "author": _identity(getattr(item, "from_id", None), entities, conn),
        "related_users": _related_users(getattr(item, "action", None), entities, conn),
        "raw": _raw(item),
        "reply_to": {
            "message_id": reply_id,
            "peer": _peer(getattr(reply, "reply_to_peer_id", None))
            or {"id": dialog_id, "kind": "channel" if dialog_id < -CHANNEL_ID_OFFSET else "chat"},
        }
        if reply_id is not None
        else None,
        "topic_id": (getattr(reply, "reply_to_top_id", None) or reply_id)
        if getattr(reply, "forum_topic", False)
        else None,
        "reactions": {
            "status": status,
            "aggregate": aggregate,
            "can_view_list": bool(getattr(reactions, "can_see_list", False)),
        },
    }


def _bounded_page(items: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    selected: list[dict[str, object]] = []
    used = 0
    for item in items:
        size = len(json.dumps(item).encode("utf-8")) + 2
        if used + size > EXPORT_RESPONSE_BYTES:
            if not selected:
                raise ValueError("A Telegram item exceeds the export IPC byte limit; text was not truncated")
            break
        selected.append(item)
        used += size
    return selected


async def _open(client: _Client, peer: object, dialog_id: int) -> dict[str, object]:
    response = await _history_response(client, peer, 0, 0, 1)
    ids = [cast(int, getattr(item, "id", 0)) for item in _objects(getattr(response, "messages", ()))]
    if isinstance(peer, types.InputPeerChannel):
        full = await _send(
            client,
            functions.channels.GetFullChannelRequest(types.InputChannel(peer.channel_id, peer.access_hash)),
            AcquisitionKind.ENTITY_LOOKUP,
        )
        predecessor = getattr(getattr(full, "full_chat", None), "migrated_from_chat_id", None)
    else:
        full = await _legacy_group(client, cast(types.InputPeerChat, peer), AcquisitionKind.ENTITY_LOOKUP)
        predecessor = None
    entity = _identities(full).get(dialog_id)
    if isinstance(entity, types.Channel) and not entity.megagroup and not entity.gigagroup:
        raise ValueError("export-chat requires a group, not a broadcast channel")
    total = getattr(response, "count", None)
    return {
        "group": {
            "dialog_id": dialog_id,
            "title": getattr(entity, "title", None),
            "kind": "supergroup" if isinstance(peer, types.InputPeerChannel) else "group",
        },
        "upper_id": max(ids, default=0),
        "total_messages": total if isinstance(total, int) else None,
        "total_kind": "estimated" if isinstance(total, int) else "unknown",
        "migrated_from_dialog_id": -predecessor if isinstance(predecessor, int) else None,
    }


def _history_ids(raw_items: Sequence[object], upper: int, before: int) -> list[int]:
    values = [cast(object, getattr(item, "id", None)) for item in raw_items]
    if any(type(value) is not int or value < 1 for value in values):
        raise ValueError("Telegram history returned invalid message IDs")
    ids = cast(list[int], values)
    if ids and max(ids) > upper:
        raise ValueError("Telegram history exceeded its frozen boundary")
    if ids and before and max(ids) >= before:
        raise ValueError("Telegram history cursor did not advance")
    return ids


async def _history(
    client: _Client, peer: object, req: Mapping[str, object], conn: sqlite3.Connection
) -> dict[str, object]:
    upper = _integer(req, "upper_id")
    before = _integer(req, "before_id", default=0)
    if upper == 0:
        return {"items": [], "next_before_id": before, "done": True}
    response = await _history_response(client, peer, before, upper, MESSAGE_HISTORY_PAGE_SIZE)
    raw_items = _objects(getattr(response, "messages", ()))
    entities = _identities(response)
    ids = _history_ids(raw_items, upper, before)
    valid = [item for item in raw_items if isinstance(item, (types.Message, types.MessageService))]
    items = _bounded_page([_message(item, _integer(req, "dialog_id"), entities, conn) for item in valid])
    next_id = min(ids, default=before)
    if len(items) < len(valid):
        next_id = cast(int, items[-1]["id"])
    return {"items": items, "next_before_id": next_id, "done": not raw_items}


async def _reactions(
    client: _Client, peer: object, req: Mapping[str, object], conn: sqlite3.Connection
) -> dict[str, object]:
    offset = req.get("offset", "")
    if not isinstance(offset, str) or len(offset) > REACTION_CURSOR_BYTES:
        raise ValueError("Invalid reaction cursor")
    response = await _send(
        client,
        functions.messages.GetMessageReactionsListRequest(
            peer=cast(types.TypeInputPeer, peer), id=_integer(req, "message_id"), limit=50, offset=offset
        ),
        AcquisitionKind.REACTION_SNAPSHOT,
    )
    entities = _identities(response)
    items = [
        {
            "peer": _identity(getattr(item, "peer_id", None), entities, conn),
            "reaction": _raw(getattr(item, "reaction", None)),
            "date": _raw(getattr(item, "date", None)),
        }
        for item in _objects(getattr(response, "reactions", ()))
    ]
    # Opaque Telegram reaction offsets cannot be rebuilt after byte trimming.
    if len(_bounded_page(items)) != len(items):
        raise ValueError("Reaction page exceeds export IPC byte limit")
    return {
        "items": items,
        "next_offset": getattr(response, "next_offset", None),
        "total": getattr(response, "count", None),
        "status": "complete",
    }


def _participant_data(participant: object | None, *, former: bool = False) -> dict[str, object]:
    is_owner = isinstance(participant, (types.ChannelParticipantCreator, types.ChatParticipantCreator))
    is_admin = is_owner or isinstance(participant, (types.ChannelParticipantAdmin, types.ChatParticipantAdmin))
    known = participant is not None or former
    rank = getattr(participant, "rank", None)
    return {
        "is_admin": is_admin if known else None,
        "role": "owner"
        if is_owner
        else "admin"
        if is_admin
        else "former_member"
        if former
        else "member"
        if known
        else "unknown",
        "rank": rank,
        "label": rank,
        "status": "complete" if known else "unavailable",
    }


async def _participant(client: _Client, peer: object, req: Mapping[str, object]) -> dict[str, object]:
    user_id = _integer(req, "user_id")
    if isinstance(peer, types.InputPeerChannel):
        try:
            with acquisition_context(AcquisitionKind.PARTICIPANT_LOOKUP):
                user_peer = await client.get_input_entity(user_id)
            response = await _send(
                client,
                functions.channels.GetParticipantRequest(
                    channel=types.InputChannel(peer.channel_id, peer.access_hash),
                    participant=cast(types.TypeInputPeer, user_peer),
                ),
                AcquisitionKind.PARTICIPANT_LOOKUP,
            )
        except UserNotParticipantError:
            return {"status": "complete", "participant": _participant_data(None, former=True)}
        participant = getattr(response, "participant", None)
    else:
        response = await _legacy_group(client, cast(types.InputPeerChat, peer), AcquisitionKind.PARTICIPANT_LOOKUP)
        participants = getattr(getattr(response, "full_chat", None), "participants", None)
        participant = next(
            (
                item
                for item in _objects(getattr(participants, "participants", ()))
                if getattr(item, "user_id", None) == user_id
            ),
            None,
        )
        if participant is None and isinstance(participants, types.ChatParticipants):
            return {"status": "complete", "participant": _participant_data(None, former=True)}
    data = _participant_data(participant)
    return {"status": data["status"], "participant": data}


async def _topic(client: _Client, peer: object, req: Mapping[str, object]) -> dict[str, object]:
    response = await _send(
        client,
        functions.messages.GetForumTopicsByIDRequest(
            peer=cast(types.TypeInputPeer, peer), topics=[_integer(req, "topic_id")]
        ),
        AcquisitionKind.TOPIC_LOOKUP,
    )
    topics = _objects(getattr(response, "topics", ()))
    return {"status": "complete" if topics else "unavailable", "topic": _raw(topics[0]) if topics else None}


async def _admin_log(
    client: _Client, peer: object, req: Mapping[str, object], conn: sqlite3.Connection
) -> dict[str, object]:
    if not isinstance(peer, types.InputPeerChannel):
        return {"items": [], "done": True, "status": "unavailable", "reason": "legacy_group_has_no_admin_log"}
    # Admin event IDs are int64, unlike message IDs.
    before = req.get("before_id", 0)
    if isinstance(before, bool) or not isinstance(before, int) or not 0 <= before < 2**63:
        raise ValueError("Invalid admin event cursor")
    response = await _send(
        client,
        functions.channels.GetAdminLogRequest(
            channel=types.InputChannel(peer.channel_id, peer.access_hash), q="", max_id=before, min_id=0, limit=50
        ),
        AcquisitionKind.ADMIN_LOG_PAGE,
    )
    entities = _identities(response)
    events = _objects(getattr(response, "events", ()))
    items = _bounded_page(
        [
            {
                "id": getattr(event, "id", 0),
                "date": _raw(getattr(event, "date", None)),
                "actor": _identity(types.PeerUser(getattr(event, "user_id", 0)), entities, conn),
                "action": _raw(getattr(event, "action", None), audit=True),
                "related_users": _related_users(getattr(event, "action", None), entities, conn),
                "source": "telegram_admin_log",
            }
            for event in events
        ]
    )
    next_id = cast(int, items[-1]["id"]) if items else before
    if items and before and next_id >= before:
        raise ValueError("Telegram admin log cursor did not advance")
    return {
        "items": items,
        "next_before_id": next_id,
        "done": not events,
        "status": "complete",
        "scope": "recent_available_admin_log; not all-time history",
    }


async def _perform(client: _Client, req: Mapping[str, object], conn: sqlite3.Connection) -> dict[str, object]:
    operation = req.get("operation")
    if operation not in {"open", "history", "reactions", "participant", "topic", "admin_log"}:
        raise ValueError("Unknown export operation")
    peer = await _resolve(client, _selector(req) if operation == "open" else _integer(req, "dialog_id"))
    if operation == "open":
        dialog_id = (
            -CHANNEL_ID_OFFSET - peer.channel_id
            if isinstance(peer, types.InputPeerChannel)
            else -cast(types.InputPeerChat, peer).chat_id
        )
        return await _open(client, peer, dialog_id)
    if operation == "history":
        return await _history(client, peer, req, conn)
    if operation == "reactions":
        return await _reactions(client, peer, req, conn)
    if operation == "participant":
        return await _participant(client, peer, req)
    if operation == "topic":
        return await _topic(client, peer, req)
    return await _admin_log(client, peer, req, conn)


async def export_operation(  # noqa: PLR0911 - explicit transport outcomes keep retry policy visible
    client: object, req: Mapping[str, object], conn: sqlite3.Connection
) -> dict[str, object]:
    """One bounded operation; deferrals are returned to the CLI, never slept here."""
    operation = req.get("operation")
    try:
        async with asyncio.timeout(EXPORT_OPERATION_SECONDS):
            with rpc_attempt_budget(RpcAttemptBudget(EXPORT_ATTEMPTS)):
                data = await _perform(cast(_Client, client), req, conn)
        response: dict[str, object] = {"ok": True, "data": data}
        if len(json.dumps(response).encode("utf-8")) > EXPORT_RESPONSE_BYTES + 100_000:
            raise ValueError("Export response exceeds its IPC byte budget")
        return response
    except TelegramRpcAdmissionDeferred as exc:
        return {"ok": False, "error": "export_deferred", "reason": "admission", "retry_after": exc.retry_after_seconds}
    except TelegramRpcThrottled as exc:
        if exc.latched:
            return {"ok": False, "error": "flood_wait_kill_switch_open", "message": "Account protection is open"}
        return {"ok": False, "error": "export_deferred", "reason": "flood_wait", "retry_after": exc.retry_after_seconds}
    except RpcAdmissionClosedError:
        return {"ok": False, "error": "export_failed", "reason": "admission_closed"}
    except RpcAdmissionError:
        return {"ok": False, "error": "export_deferred", "reason": "admission", "retry_after": 5.0}
    except (RPCError, TimeoutError, OSError, ValueError, RpcAttemptBudgetExhaustedError) as exc:
        reason = describe_telegram_rpc_error(exc).error_type
        if operation in {"reactions", "participant", "topic", "admin_log"}:
            return {
                "ok": True,
                "data": {
                    "status": "unavailable",
                    "reason": reason,
                    "items": [],
                    "done": True,
                    "participant": _participant_data(None),
                    "topic": None,
                },
            }
        return {
            "ok": False,
            "error": "export_failed",
            "reason": reason,
            "message": "History export could not complete; no final file was published",
        }
