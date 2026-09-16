"""Telethon-only gateway helpers for reading adapters."""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol, cast

from telethon.tl import types  # type: ignore[import-untyped]
from telethon.tl.functions.messages import (
    GetFullChatRequest,  # type: ignore[import-untyped]
    GetScheduledHistoryRequest,  # type: ignore[import-untyped]
)
from telethon.tl.types import TypeInputPeer  # type: ignore[import-untyped]
from telethon.utils import get_peer_id  # type: ignore[import-untyped]

from .entity_profile.contracts import GroupCurrentPhoto, GroupProfileObservation
from .entity_profile.ports import GroupProfilePort
from .flood import TelegramRpcThrottled
from .telegram_access import ACCESS_LOST_ERRORS
from .telegram_reading import GatewayFailure, GatewayFailureKind
from .telegram_rpc_scheduler import RpcAdmissionClosedError, RpcAdmissionError, UnclassifiedTelegramRpcError

CATCHABLE_GATEWAY_FAILURES = (Exception,)


class ScheduledHistoryClient(Protocol):
    async def get_input_entity(self, _dialog_id: int) -> object: ...

    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class GroupProfileClient(Protocol):
    async def __call__(self, _request: object, **_kwargs: object) -> object: ...


class TelethonGroupProfileGateway(GroupProfilePort):
    """Normalize ``messages.GetFullChat`` at the Telethon boundary."""

    def __init__(self, client: object, *, now_provider: Callable[[], float] | None = None) -> None:
        self._client = cast(GroupProfileClient, client)
        self._now_provider = now_provider or time.time

    async def fetch_group_profile(self, group_id: int) -> GroupProfileObservation:
        raw_group_id = _raw_group_id(group_id)
        started_at = int(self._now_provider())
        result = await self._client(GetFullChatRequest(chat_id=raw_group_id))
        completed_at = int(self._now_provider())
        full_chat = _validated_full_chat(result, raw_group_id)
        participant_ids, participants_reason = _normalize_participants(
            getattr(full_chat, "participants", None), raw_group_id
        )
        current_photo = _normalize_current_photo(getattr(full_chat, "chat_photo", None))
        return GroupProfileObservation(
            group_id=group_id,
            about=_optional_string(getattr(full_chat, "about", None)),
            invite_link=_normalize_invite_link(getattr(full_chat, "exported_invite", None)),
            participant_ids=participant_ids,
            participants_unavailable_reason=participants_reason,
            current_photo=current_photo,
            observation_started_at=started_at,
            observation_completed_at=completed_at,
        )


def _raw_group_id(group_id: int) -> int:
    if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id == 0:
        raise ValueError("legacy group target id is invalid")
    return -group_id if group_id < 0 else group_id


def _validated_full_chat(result: object, raw_group_id: int) -> types.ChatFull:
    if not isinstance(result, types.messages.ChatFull):
        raise ValueError("full chat envelope is invalid")
    full_chat = getattr(result, "full_chat", None)
    if not isinstance(full_chat, types.ChatFull):
        raise ValueError("legacy chat full payload is invalid")
    if _positive_id(getattr(full_chat, "id", None)) != raw_group_id:
        raise ValueError("legacy chat full target does not match")
    return full_chat


def _normalize_invite_link(invite: object) -> str | None:
    link = getattr(cast(object, invite), "link", None) if invite is not None else None
    return link if isinstance(link, str) else None


def _positive_id(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _normalize_participants(  # noqa: PLR0911
    participants: object,
    raw_group_id: int,
) -> tuple[tuple[int, ...] | None, str | None]:
    if isinstance(participants, types.ChatParticipantsForbidden):
        return None, "participants_forbidden"
    if participants is None:
        return None, "participants_missing"
    if not isinstance(participants, types.ChatParticipants):
        return None, "participants_malformed"
    if _positive_id(getattr(participants, "chat_id", None)) != raw_group_id:
        return None, "participants_target_mismatch"
    raw_items = getattr(participants, "participants", None)
    if not isinstance(raw_items, Sequence) or isinstance(raw_items, str | bytes | bytearray):
        return None, "participants_not_sequence"
    participant_types = (types.ChatParticipant, types.ChatParticipantAdmin, types.ChatParticipantCreator)
    ids: list[int] = []
    for item in raw_items:
        if not isinstance(item, participant_types):
            return None, "participants_malformed"
        user_id = _positive_id(getattr(item, "user_id", None))
        if user_id is None:
            return None, "participants_malformed"
        ids.append(user_id)
    return tuple(ids), None


def _normalize_current_photo(photo: object) -> GroupCurrentPhoto | None:
    if photo is None:
        return None
    if not isinstance(photo, (types.Photo, types.PhotoEmpty)):
        raise ValueError("legacy group current photo is invalid")
    if isinstance(photo, types.PhotoEmpty) and getattr(photo, "id", None) == 0:
        return None
    photo_id = _positive_id(getattr(photo, "id", None))
    if photo_id is None:
        raise ValueError("legacy group current photo id is invalid")
    raw_date = getattr(photo, "date", None)
    date = raw_date.isoformat() if isinstance(raw_date, datetime) else None
    return GroupCurrentPhoto(photo_id=photo_id, date=date)


def translate_gateway_failure(exc: BaseException) -> GatewayFailure:
    """Translate Telegram exceptions at the integration boundary."""
    if isinstance(exc, RpcAdmissionClosedError):
        raise exc
    if isinstance(exc, (RpcAdmissionError, UnclassifiedTelegramRpcError)):
        return GatewayFailure(
            GatewayFailureKind.TRANSIENT,
            "TelegramUnavailable",
            "Telegram is temporarily unavailable; retry later",
            True,
        )
    if isinstance(exc, TelegramRpcThrottled):
        return GatewayFailure(
            GatewayFailureKind.FLOOD_WAIT,
            TelegramRpcThrottled.__name__,
            "Telegram RPC throttled",
            not exc.latched,
            exc.retry_after_seconds,
        )
    message = str(exc).replace("\n", "\\n") or type(exc).__name__
    if isinstance(exc, ACCESS_LOST_ERRORS):
        return GatewayFailure(GatewayFailureKind.ACCESS_LOST, type(exc).__name__, message, False)
    if isinstance(exc, ValueError):
        return GatewayFailure(GatewayFailureKind.INVALID_TARGET, type(exc).__name__, message, False)
    return GatewayFailure(GatewayFailureKind.TRANSIENT, type(exc).__name__, message, True)


async def fetch_scheduled_history_snapshot(
    client: ScheduledHistoryClient,
    dialog_id: int,
) -> list[object]:
    """Fetch one scheduled queue snapshot through Telethon.

    The daemon-owned TelegramRpcGate configures Telethon's threshold to zero,
    so the gate owns account-wide flood admission and observation.
    The scheduled reconciliation caller owns the ``SCHEDULED_MESSAGES`` scope;
    this shared adapter deliberately inherits it.
    """
    input_entity = cast(TypeInputPeer, await client.get_input_entity(dialog_id))
    result = await client(GetScheduledHistoryRequest(peer=input_entity, hash=0))
    messages = list(cast(Sequence[object], getattr(result, "messages", ()) or ()))
    entities = {
        get_peer_id(entity): entity
        for entity in [
            *cast(Sequence[object], getattr(result, "users", ()) or ()),
            *cast(Sequence[object], getattr(result, "chats", ()) or ()),
        ]
    }
    for message in messages:
        finish_init = getattr(message, "_finish_init", None)
        if callable(finish_init):
            finish_init(client, entities, input_entity)
    return messages
